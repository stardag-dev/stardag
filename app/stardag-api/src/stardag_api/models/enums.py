"""Enumeration types for database models.

Every enum below is stored as a **native Postgres enum** whose labels are
the members' *values* (lowercase), the same spelling the API puts on the
wire — so a status reads the same in a query, a log line and a response.
See :func:`stardag_api.models.base.pg_enum`.
"""

import enum


class WorkspaceRole(str, enum.Enum):
    """Role of a user within a workspace."""

    OWNER = "owner"  # Full control, cannot be removed, can transfer ownership
    ADMIN = "admin"  # Can manage members and environments
    MEMBER = "member"  # Read/write access to environments


class InviteStatus(str, enum.Enum):
    """Status of a workspace invite."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    CANCELLED = "cancelled"


class TaskStatus(str, enum.Enum):
    """Global status of a completion (``task.status``).

    v1's ``UNREGISTERED`` (a phantom, known only as a dependency) is gone:
    every ``task`` row is registered with a body on some instance.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
    # Waiting for its dynamic (yielded) children; holds no claim, and is
    # re-run from scratch once they are COMPLETED.
    SUSPENDED = "suspended"
    # The platform took the execution away (function timeout, container
    # reclaimed) — not a failure and not terminal; holds no claim.
    INTERRUPTED = "interrupted"


class BuildStatus(str, enum.Enum):
    """Stored status of a build, driven by build events."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXIT_EARLY = "exit_early"  # All remaining tasks running in other builds


class DeploymentKind(str, enum.Enum):
    """Where a deployment's code runs. Part of every deployment lookup, so a
    local ``code_id`` can never collide with a Modal one."""

    MODAL = "modal"
    LOCAL = "local"


class AdmittedBy(str, enum.Enum):
    """How an instance became a member of a plan (``plan_member.admitted_by``)."""

    ROOT = "root"  # the build's request
    STATIC = "static"  # registered by the static phase (discovery walk)
    DYNAMIC = "dynamic"  # yielded by a running parent
    CLOSURE = "closure"  # reachable over edges from a member, admitted to close


class ExclusionReason(str, enum.Enum):
    """Why a plan member was given up on (``plan_member.excluded_reason``)."""

    OPERATOR = "operator"  # an operator excluded it (STA-104)
    DISCOVERY_FAILED = "discovery_failed"  # class not importable / requires() raised
    UPSTREAM_EXCLUDED = "upstream_excluded"  # cascaded from an excluded upstream


class ClaimOutcome(str, enum.Enum):
    """How an execution's claim ended (``execution.claim_outcome``).

    Written by the **server**, in ``transition_task()``, whenever the task
    leaves RUNNING or the claim changes hands.
    """

    # The report that moved the task off RUNNING.
    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"
    INTERRUPTED = "interrupted"
    CANCELLED = "cancelled"
    # A later claiming start took over the lapsed claim.
    TAKEN_OVER = "taken_over"
    # A lapsed claim closed by something other than a claiming start (e.g.
    # an observed completion).
    LAPSED = "lapsed"
    # Released by a build's terminal transition (complete / fail / cancel).
    RELEASED = "released"


class ExecutionOutcome(str, enum.Enum):
    """How an execution itself ended (``execution.outcome``).

    Written only by the execution's own terminal report, or by an operator
    stop — never inferred by the server.
    """

    COMPLETED = "completed"
    FAILED = "failed"
    SUSPENDED = "suspended"
    INTERRUPTED = "interrupted"
    PREEMPTED = "preempted"
    STOPPED = "stopped"


class EventType(str, enum.Enum):
    """Event types for the append-only event log."""

    # Build events
    BUILD_STARTED = "build_started"
    BUILD_RESUMED = "build_resumed"
    BUILD_COMPLETED = "build_completed"
    BUILD_FAILED = "build_failed"
    BUILD_CANCELLED = "build_cancelled"
    BUILD_EXIT_EARLY = "build_exit_early"  # All remaining tasks running elsewhere

    # Task events
    TASK_PENDING = "task_pending"  # a new completion was registered
    TASK_REFERENCED = "task_referenced"  # an existing completion joined a plan
    TASK_STARTED = "task_started"
    TASK_SUSPENDED = "task_suspended"
    TASK_RESUMED = "task_resumed"
    TASK_RETRIED = "task_retried"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_INTERRUPTED = "task_interrupted"
    # The platform is restarting the same execution itself; no status of
    # its own — the task stays RUNNING and keeps its claim.
    TASK_PREEMPTED = "task_preempted"
    TASK_SKIPPED = "task_skipped"
    TASK_CANCELLED = "task_cancelled"
    # New in v2.
    TASK_INVALIDATED = "task_invalidated"  # COMPLETED -> PENDING, target missing
    TASK_EXCLUDED = "task_excluded"  # a plan member was given up on
    TASK_OBSERVED_COMPLETE = "task_observed_complete"  # target found at discovery
    TASK_STRUCTURE_DIVERGED = "task_structure_diverged"  # edges grew in-scope
    TASK_YIELDED = "task_yielded"  # one applied /yield batch (carries batch_id)


#: Build-level event types: they carry no ``plan_id`` (CHECK on ``event``).
BUILD_EVENT_TYPES: tuple[EventType, ...] = (
    EventType.BUILD_STARTED,
    EventType.BUILD_RESUMED,
    EventType.BUILD_COMPLETED,
    EventType.BUILD_FAILED,
    EventType.BUILD_CANCELLED,
    EventType.BUILD_EXIT_EARLY,
)
