"""State and helpers of :class:`~stardag.testing.InMemoryRegistry`.

The rows mirror the v2 schema (design.md, "Entities"): ``task`` (the
completion and its claim), ``deployment``, ``settings``, ``task_instance``
with its edges, ``plan``, ``plan_member`` and the ``execution`` ledger.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from stardag.exceptions import APIError, NotFoundError

RESERVED_SETTINGS_PREFIXES = ("STARDAG_", "MODAL_")
ACTIONABLE = ("pending", "suspended", "interrupted", "cancelled", "skipped")
DEFAULT_CLAIM_TTL = 3600
MAX_CLAIM_TTL = 24 * 3600
CLOCK_SKEW_TOLERANCE = timedelta(seconds=5)
# How long a preempted execution's claim stays live for the restart.
PREEMPT_GRACE = timedelta(seconds=120)
WAKE_HANDOUT_WINDOW = timedelta(seconds=120)


def refuse(
    code: str, message: str = "", *, status: int = 409, **detail: Any
) -> APIError:
    """The registry's refusal, as :class:`~stardag.registry.APIRegistry`
    raises it (``detail.code``)."""
    payload = {"code": code, "message": message or code, **detail}
    if status == 404:
        return NotFoundError(
            f"{code}: resource not found", detail=message, payload=payload
        )
    return APIError(
        f"{code} failed", status_code=status, detail=message, payload=payload
    )


def settings_hash(body: Mapping[str, str]) -> str:
    canonical = json.dumps(
        dict(body), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass
class BuildRow:
    id: UUID
    name: str
    root_task_ids: list[str]
    description: str | None = None
    status: str = "running"
    executor_metadata: dict[str, Any] | None = None
    reactive_app_name: str | None = None
    reactive_tick_kwargs: dict[str, Any] | None = None
    needs_tick: bool = False
    handed_out_at: datetime | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    is_resumed: bool = False
    error_message: str | None = None
    created_at: datetime | None = None
    #: Bumped on build-level lifecycle events only (created, resumed,
    #: completed/failed/cancelled/exit-early) — mirrors the server's
    #: ``Build.last_active_at`` (models/build.py), which "GET /builds"
    #: orders by, most recently active first.
    last_active_at: datetime | None = None


@dataclass
class DeploymentRow:
    id: UUID
    kind: str
    app_name: str
    code_id: str
    generation: int
    deployed_at: datetime
    activated_at: datetime | None = None
    image_id: str | None = None
    modal_app_id: str | None = None


@dataclass
class TaskRow:
    task_id: str
    task_namespace: str
    task_name: str
    version: str | None
    output_uri: str | None
    status: str = "pending"
    status_at: datetime | None = None
    completed_at: datetime | None = None
    error_message: str | None = None
    claim_expires_at: datetime | None = None
    claim_plan_id: UUID | None = None
    preempted_at: datetime | None = None
    execution_id: UUID | None = None
    limit_keys: set[str] = field(default_factory=set)


@dataclass
class InstanceRow:
    id: UUID
    deployment_id: UUID
    settings_hash: str
    instance_hash: str
    task_id: str
    body: dict[str, Any]
    expanded: bool = False
    # upstream instance id -> is_dynamic
    upstreams: dict[UUID, bool] = field(default_factory=dict)


@dataclass
class PlanRow:
    id: UUID
    build_id: UUID
    deployment_id: UUID
    settings_hash: str
    generation: int
    activated_at: datetime | None = None
    sealed_at: datetime | None = None
    superseded_at: datetime | None = None
    created_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.activated_at is not None and self.superseded_at is None


@dataclass
class MemberRow:
    task_id: str
    instance_id: UUID
    is_root: bool
    admitted_by: str
    excluded_reason: str | None = None


@dataclass
class ExecutionRow:
    id: UUID
    task_id: str
    plan_id: UUID
    instance_id: UUID
    started_at: datetime
    executor: str | None = None
    executor_ref: str | None = None
    executor_metadata: dict[str, Any] | None = None
    claim_released_at: datetime | None = None
    claim_outcome: str | None = None
    ended_at: datetime | None = None
    outcome: str | None = None


@dataclass
class ArtifactRow:
    """One stored artifact, minted like the server's ``TaskArtifact`` row
    (``services/artifacts.py``): ``id``/``created_at`` are assigned once,
    on first upload of a ``(task, type, name)``, and survive a re-upload
    that only replaces ``body`` -- the server's upsert only touches
    ``body_json`` on conflict."""

    id: UUID
    artifact_type: str
    name: str
    body: Any
    created_at: datetime


@dataclass
class Event:
    type: str
    task_id: str | None = None
    build_id: UUID | None = None
    plan_id: UUID | None = None
    execution_id: UUID | None = None
    applied: bool = True
    detail: dict[str, Any] = field(default_factory=dict)
    error_message: str | None = None
    id: UUID = field(default_factory=uuid4)
    #: Stamped by :meth:`RegistryState.log` from the registry clock.
    created_at: datetime | None = None


class RegistryState:
    """The rows, the clock, and the transaction/refusal helpers."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.builds: dict[UUID, BuildRow] = {}
        self.deployments: dict[UUID, DeploymentRow] = {}
        self.settings: dict[str, dict[str, str]] = {}
        self.tasks: dict[str, TaskRow] = {}
        self.instances: dict[UUID, InstanceRow] = {}
        self.instance_index: dict[tuple[UUID, str, str], UUID] = {}
        self.plans: dict[UUID, PlanRow] = {}
        self.members: dict[UUID, dict[str, MemberRow]] = {}
        self.executions: dict[UUID, ExecutionRow] = {}
        self.yields: dict[tuple[UUID, UUID], Any] = {}
        self.limits: dict[str, int] = {}
        self.events: list[Event] = []
        self.tick_summaries: dict[UUID, list[dict[str, Any]]] = {}
        self.artifacts: dict[str, list[ArtifactRow]] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def now(self) -> datetime:
        return self.clock()

    def log(self, event: Event) -> None:
        """Append to the event log, stamped with the registry clock (the
        server's ``Event.created_at``)."""
        if event.created_at is None:
            event.created_at = self.now()
        self.events.append(event)

    def _record(self, method: str, **kwargs: Any) -> None:
        self.calls.append((method, kwargs))

    def calls_to(self, method: str, **match: Any) -> list[dict[str, Any]]:
        """The keyword arguments of every call to ``method`` whose arguments
        include ``match`` (compared as strings), in order."""
        return [
            kwargs
            for name, kwargs in self.calls
            if name == method
            and all(str(kwargs.get(k)) == str(v) for k, v in match.items())
        ]

    def called(self, method: str, **match: Any) -> bool:
        return bool(self.calls_to(method, **match))

    def methods_called(self) -> list[str]:
        return [name for name, _ in self.calls]

    _TRANSACTIONAL = (
        "builds",
        "deployments",
        "settings",
        "tasks",
        "instances",
        "instance_index",
        "plans",
        "members",
        "executions",
        "yields",
        "events",
    )

    @contextmanager
    def transaction(
        self, *, keep: Callable[[BaseException], bool] | None = None
    ) -> Iterator[None]:
        """All-or-nothing, like the server's transaction: on an error the
        rows are restored — unless ``keep(error)`` says the refusal is a
        recorded one (a late report's ledger end is committed first)."""
        snapshot = {
            name: copy.deepcopy(getattr(self, name)) for name in self._TRANSACTIONAL
        }
        try:
            yield
        except BaseException as e:
            if keep is None or not keep(e):
                for name, value in snapshot.items():
                    setattr(self, name, value)
            raise

    # -- lookups --------------------------------------------------------------------

    def build(self, build_id: UUID) -> BuildRow:
        build = self.builds.get(build_id)
        if build is None:
            raise refuse("unknown_build", f"no build {build_id}", status=404)
        return build

    def plan(self, plan_id: UUID) -> PlanRow:
        plan = self.plans.get(plan_id)
        if plan is None:
            raise refuse("unknown_plan", f"no plan {plan_id}", status=404)
        return plan

    def task(self, task_id: str) -> TaskRow:
        task = self.tasks.get(task_id)
        if task is None:
            raise refuse("unknown_task", f"no task {task_id}", status=404)
        return task

    def member(self, plan_id: UUID, task_id: str) -> MemberRow:
        member = self.members.get(plan_id, {}).get(task_id)
        if member is None:
            raise refuse(
                "unknown_member", f"task {task_id} is not a member", status=404
            )
        return member

    def active_plan(self, build_id: UUID) -> PlanRow | None:
        for plan in self.plans.values():
            if plan.build_id == build_id and plan.active:
                return plan
        return None

    def live(self, task: TaskRow) -> bool:
        return (
            task.status == "running"
            and task.claim_expires_at is not None
            and task.claim_expires_at > self.now()
        )

    def current_deployment(self, kind: str, app_name: str) -> DeploymentRow | None:
        """Mirrors the server's ``current_deployment_id``: a local
        deployment is never current, no matter its activation state."""
        if kind == "local":
            return None
        rows = [
            d
            for d in self.deployments.values()
            if d.kind == kind and d.app_name == app_name and d.activated_at is not None
        ]
        return max(rows, key=lambda d: d.generation) if rows else None

    def verify_deployment_current(self, deployment: DeploymentRow) -> None:
        """Mirrors the server's ``verify_deployment_current``: a no-op for
        a local deployment, which is authoritative for its own plans;
        otherwise 409 ``deployment_not_current`` unless it is its app's
        current (highest-generation, activated) row."""
        if deployment.kind == "local":
            return
        current = self.current_deployment(deployment.kind, deployment.app_name)
        if current is None or current.id != deployment.id:
            raise refuse("deployment_not_current")

    # -- the ledger and wake-ups ----------------------------------------------------

    def close_claim(self, task: TaskRow, outcome: str) -> None:
        """Whatever moves a task off RUNNING closes its execution's claim."""
        if task.execution_id is not None:
            execution = self.executions[task.execution_id]
            if execution.claim_released_at is None:
                execution.claim_released_at = self.now()
                execution.claim_outcome = outcome
        task.claim_plan_id = None
        task.preempted_at = None
        task.claim_expires_at = None

    def move(
        self, task: TaskRow, status: str, *, flag_except: UUID | None = None
    ) -> None:
        task.status = status
        task.status_at = self.now()
        self.flag_holders(task.task_id, except_build=flag_except)

    def flag_holders(self, task_id: str, *, except_build: UUID | None = None) -> None:
        """Flag the RUNNING reactive builds whose active plan holds the task
        (a status change may have moved their frontier)."""
        for build in self.builds.values():
            if build.id == except_build or build.status != "running":
                continue
            if build.reactive_app_name is None:
                continue
            plan = self.active_plan(build.id)
            if plan is None:
                continue
            member = self.members.get(plan.id, {}).get(task_id)
            if member is not None and member.excluded_reason is None:
                build.needs_tick = True

    def release_build_claims(self, build: BuildRow) -> None:
        """A build's terminal transition releases the claims of all its
        plans: the tasks go CANCELLED (actionable for any other build)."""
        plan_ids = {p.id for p in self.plans.values() if p.build_id == build.id}
        for task in self.tasks.values():
            if task.status == "running" and task.claim_plan_id in plan_ids:
                self.close_claim(task, "released")
                self.move(task, "cancelled", flag_except=build.id)
                self.log(Event("TASK_CANCELLED", task.task_id, build.id))
