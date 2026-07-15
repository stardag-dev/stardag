"""Per-build task object persistence for reactive scheduling.

A reactive scheduler tick is a short-lived process with no memory of the
build: it learns *which* tasks are actionable from the registry frontier,
but needs the actual task *objects* (with parameters) to spawn workers.
``BuildTaskStore`` persists pickled tasks per build under a target root —
the same durable storage the build's outputs live on:

    <target-root>/_stardag_builds/<build_id>/tasks/<task_id>.pkl

The store holds *only* task objects. The build's orchestration metadata
(the "this build is reactively scheduled" marker, the owning app name, and
the tick configuration) lives in the registry, not here — target roots may
be configured immutable/append-only (S3 object-lock, Modal volumes refuse
overwrites), and the registry is already the source of truth for build
roots/status/frontier. See ``registry.build_set_reactive_meta`` and the
``reactive_app_name`` field on the build frontier.

Pickles are written by the trigger (initial discovery) and by workers
(dynamically yielded deps). They are write-once (a task id maps to one
immutable object), so the store is compatible with immutable/append-only
target roots. Same-deployment guarantee applies: a pickle is only loaded by
containers of the same deployed app version that wrote it — the same
constraint as passing tasks to workers by value.
"""

from __future__ import annotations

import logging
import pickle
import typing
from uuid import UUID

from stardag import BaseTask
from stardag.target._factory import DEFAULT_TARGET_ROOT_KEY, target_factory_provider

logger = logging.getLogger(__name__)

_STORE_PREFIX = "_stardag_builds"


class BuildTaskStore:
    """Persists task objects for a build under a target root (see module docs)."""

    def __init__(
        self,
        build_id: UUID,
        target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
    ) -> None:
        self.build_id = build_id
        self.target_root_key = target_root_key

    def _target(self, relpath: str):
        return target_factory_provider.get().get_file_target(
            f"{_STORE_PREFIX}/{self.build_id}/{relpath}",
            target_root_key=self.target_root_key,
        )

    # --- task pickles ---

    def save_task(self, task: BaseTask) -> None:
        # Write-once: a task id maps to one immutable object, so an existing
        # pickle is already correct — skip it. This keeps the store
        # compatible with immutable/append-only target roots and lets
        # re-triggers re-persist a DAG without overwriting (a stale pickle
        # from a redeployed app is handled by the registry-rehydration
        # fallback, not by overwriting here).
        target = self._target(f"tasks/{task.id}.pkl")
        if target.exists():
            return
        with target.open("wb") as handle:
            handle.write(pickle.dumps(task))

    def save_tasks(self, tasks: typing.Iterable[BaseTask]) -> None:
        for task in tasks:
            self.save_task(task)

    def load_task(self, task_id: UUID | str) -> BaseTask | None:
        """Load a task by id; None if not persisted (logged as error)."""
        target = self._target(f"tasks/{task_id}.pkl")
        if not target.exists():
            logger.error(
                f"Task {task_id} of build {self.build_id} not found in the "
                f"build task store — cannot (re)schedule it."
            )
            return None
        with target.open("rb") as handle:
            task = pickle.loads(handle.read())
        if not isinstance(task, BaseTask):
            logger.error(
                f"Task store entry for {task_id} of build {self.build_id} "
                f"is not a BaseTask (got {type(task).__name__})."
            )
            return None
        return task
