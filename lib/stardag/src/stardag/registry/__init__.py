"""The task registry (v2).

- :class:`RegistryABC`: the interface every engine and integration uses.
- :class:`APIRegistry`: its implementation over the ``/api/v2`` HTTP API.
- :class:`NoOpRegistry`: the default when no registry is configured; the
  engines make no registry call against it.
- :data:`registry_provider`: the configured registry for this process.

The response and registration models are re-exported here; see
:mod:`stardag.registry._models` for the vocabulary (an *instance* is a
registry row under a scope; the Python object is a *task object*).
"""

from stardag.registry._api_registry import APIRegistry
from stardag.registry._auth import StardagAPIKeyAuth, StardagTokenAuth
from stardag.registry._base import (
    NoOpRegistry,
    RegistryABC,
    get_git_commit_hash,
    init_registry,
    is_noop_registry,
    registry_provider,
)
from stardag.registry._models import (
    BuildFrontier,
    BuildInfo,
    BuildNotifyResult,
    ConcurrencyLimitHolderInfo,
    ConcurrencyLimitInfo,
    DeploymentInfo,
    ExclusionResult,
    ExecutionInfo,
    FrontierMember,
    MembersResult,
    PlanInfo,
    PlanRoots,
    RegistrationItem,
    ResumeResult,
    SchedulerLeaseResult,
    SettingsInfo,
    TaskArtifactInfo,
    TaskInfo,
    TaskInstanceInfo,
    TickSummaryRecord,
    TransitionResult,
    WakeCandidate,
    YieldResult,
)

__all__ = [
    "APIRegistry",
    "BuildFrontier",
    "BuildInfo",
    "BuildNotifyResult",
    "ConcurrencyLimitHolderInfo",
    "ConcurrencyLimitInfo",
    "DeploymentInfo",
    "ExclusionResult",
    "ExecutionInfo",
    "FrontierMember",
    "MembersResult",
    "NoOpRegistry",
    "PlanInfo",
    "PlanRoots",
    "RegistrationItem",
    "RegistryABC",
    "ResumeResult",
    "SchedulerLeaseResult",
    "SettingsInfo",
    "StardagAPIKeyAuth",
    "StardagTokenAuth",
    "TaskArtifactInfo",
    "TaskInfo",
    "TaskInstanceInfo",
    "TickSummaryRecord",
    "TransitionResult",
    "WakeCandidate",
    "YieldResult",
    "get_git_commit_hash",
    "init_registry",
    "is_noop_registry",
    "registry_provider",
]
