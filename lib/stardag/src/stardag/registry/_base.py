"""Base registry classes and utilities."""

import abc
import datetime
import getpass
import os
import subprocess
from functools import lru_cache
from typing import TYPE_CHECKING
from uuid import UUID

from pydantic import BaseModel

from stardag.polymorphic import SubClass
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from stardag import BaseTask
    from stardag.registry_asset import RegistryAsset


class RegisterdTaskEnvelope(BaseModel):
    task: SubClass["BaseTask"]
    task_id: UUID
    user: str
    created_at: datetime.datetime
    commit_hash: str

    @classmethod
    def new(cls, task: "BaseTask"):
        return cls(
            task=task,
            task_id=task.id,
            user=getpass.getuser(),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            commit_hash=get_git_commit_hash(),
        )


class RegistryABC(metaclass=abc.ABCMeta):
    """Abstract base class for task registries.

    A registry tracks task execution within builds. Implementations must
    provide at least the `task_register` method. All other methods have default
    no-op implementations for backwards compatibility.

    The registry is stateless with respect to build_id - the build_id is passed
    explicitly to all methods that need it. This allows a single registry instance
    to be reused across multiple builds.

    Method naming convention:
    - Build methods: build_<action> (e.g., build_start, build_complete)
    - Task methods: task_<action> (e.g., task_register, task_start)
    - Async versions: <method>_aio suffix (e.g., build_start_aio, task_register_aio)
    """

    # -------------------------------------------------------------------------
    # Build lifecycle methods
    # -------------------------------------------------------------------------

    def build_start(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> str:
        """Start a new build session.

        Called at the beginning of a build. Returns a build ID.

        Args:
            root_tasks: The root tasks being built
            description: Optional description of the build

        Returns:
            Build ID for the new build session.
        """
        return "default-build"

    def build_complete(self, build_id: str) -> None:
        """Mark a build as completed successfully.

        Args:
            build_id: The build ID returned by build_start.
        """
        pass

    def build_fail(self, build_id: str, error_message: str | None = None) -> None:
        """Mark a build as failed.

        Args:
            build_id: The build ID returned by build_start.
            error_message: Optional error message describing the failure.
        """
        pass

    def build_cancel(self, build_id: str) -> None:
        """Cancel a build.

        Called when a build is explicitly cancelled by the user.

        Args:
            build_id: The build ID returned by build_start.
        """
        pass

    def build_exit_early(self, build_id: str, reason: str | None = None) -> None:
        """Mark a build as exited early.

        Called when all remaining tasks are running in other builds
        and this build should stop waiting.

        Args:
            build_id: The build ID returned by build_start.
            reason: Optional reason for exiting early.
        """
        pass

    # -------------------------------------------------------------------------
    # Task lifecycle methods
    # -------------------------------------------------------------------------

    @abc.abstractmethod
    def task_register(self, build_id: str, task: "BaseTask") -> None:
        """Register a task as pending/scheduled.

        This is called when a task is about to be executed.

        Args:
            build_id: The build ID returned by build_start.
            task: The task to register.
        """
        pass

    def task_start(self, build_id: str, task: "BaseTask") -> None:
        """Mark a task as started/running.

        Called immediately before a task begins execution.

        Args:
            build_id: The build ID returned by build_start.
            task: The task that is starting.
        """
        pass

    def task_complete(self, build_id: str, task: "BaseTask") -> None:
        """Mark a task as completed successfully.

        Called after a task finishes execution without errors.

        Args:
            build_id: The build ID returned by build_start.
            task: The task that completed.
        """
        pass

    def task_fail(
        self, build_id: str, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Mark a task as failed.

        Called when a task raises an exception during execution.

        Args:
            build_id: The build ID returned by build_start.
            task: The task that failed.
            error_message: Optional error message describing the failure.
        """
        pass

    def task_suspend(self, build_id: str, task: "BaseTask") -> None:
        """Mark a task as suspended waiting for dynamic dependencies.

        Called when a task yields dynamic deps that are not yet complete.
        The task will remain suspended until its dynamic deps are built.

        Args:
            build_id: The build ID returned by build_start.
            task: The task that is suspended.
        """
        pass

    def task_resume(self, build_id: str, task: "BaseTask") -> None:
        """Mark a task as resumed after dynamic dependencies completed.

        Called when a task's dynamic dependencies are complete and
        the task is ready to continue execution (either by resuming
        a suspended generator or by re-executing the task).

        Args:
            build_id: The build ID returned by build_start.
            task: The task that is resuming.
        """
        pass

    def task_cancel(self, build_id: str, task: "BaseTask") -> None:
        """Cancel a task.

        Called when a task is explicitly cancelled by the user.

        Args:
            build_id: The build ID returned by build_start.
            task: The task to cancel.
        """
        pass

    def task_waiting_for_lock(
        self, build_id: str, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Record that a task is waiting for a global lock.

        Called when a task cannot acquire its lock because another
        build is holding it.

        Args:
            build_id: The build ID returned by build_start.
            task: The task waiting for the lock.
            lock_owner: Optional identifier of who holds the lock.
        """
        pass

    def task_upload_assets(
        self, build_id: str, task: "BaseTask", assets: list["RegistryAsset"]
    ) -> None:
        """Upload assets for a completed task.

        Called after a task completes successfully if it has registry assets.

        Args:
            build_id: The build ID returned by build_start.
            task: The completed task.
            assets: List of assets to upload.
        """
        pass

    # -------------------------------------------------------------------------
    # Async versions - default implementations delegate to sync methods
    # -------------------------------------------------------------------------

    async def build_start_aio(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> str:
        """Async version of build_start."""
        return self.build_start(root_tasks, description)

    async def build_complete_aio(self, build_id: str) -> None:
        """Async version of build_complete."""
        self.build_complete(build_id)

    async def build_fail_aio(
        self, build_id: str, error_message: str | None = None
    ) -> None:
        """Async version of build_fail."""
        self.build_fail(build_id, error_message)

    async def build_cancel_aio(self, build_id: str) -> None:
        """Async version of build_cancel."""
        self.build_cancel(build_id)

    async def build_exit_early_aio(
        self, build_id: str, reason: str | None = None
    ) -> None:
        """Async version of build_exit_early."""
        self.build_exit_early(build_id, reason)

    async def task_register_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_register."""
        self.task_register(build_id, task)

    async def task_start_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_start."""
        self.task_start(build_id, task)

    async def task_complete_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_complete."""
        self.task_complete(build_id, task)

    async def task_fail_aio(
        self, build_id: str, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Async version of task_fail."""
        self.task_fail(build_id, task, error_message)

    async def task_suspend_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_suspend."""
        self.task_suspend(build_id, task)

    async def task_resume_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_resume."""
        self.task_resume(build_id, task)

    async def task_cancel_aio(self, build_id: str, task: "BaseTask") -> None:
        """Async version of task_cancel."""
        self.task_cancel(build_id, task)

    async def task_waiting_for_lock_aio(
        self, build_id: str, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Async version of task_waiting_for_lock."""
        self.task_waiting_for_lock(build_id, task, lock_owner)

    async def task_upload_assets_aio(
        self, build_id: str, task: "BaseTask", assets: list["RegistryAsset"]
    ) -> None:
        """Async version of task_upload_assets."""
        self.task_upload_assets(build_id, task, assets)


class NoOpRegistry(RegistryABC):
    """A registry that does nothing.

    Used as a default when no registry is configured.
    """

    def build_start(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> str:
        """Return a placeholder build ID."""
        return "no-op-registry-build"

    def task_register(self, build_id: str, task: "BaseTask") -> None:
        pass


def init_registry() -> RegistryABC:
    """Initialize the default registry based on configuration.

    Returns APIRegistry if authentication is configured, otherwise NoOpRegistry.
    """
    from stardag.config import config_provider
    from stardag.registry._api_registry import APIRegistry

    config = config_provider.get()

    # Use API registry if we have authentication or explicit API URL set
    if (
        config.api_key
        or config.access_token
        or config.api.url != "http://localhost:8000"
    ):
        return APIRegistry()

    return NoOpRegistry()


registry_provider = resource_provider(RegistryABC, init_registry)


@lru_cache
def get_git_commit_hash() -> str:
    """Get the short SHA of the current Git commit."""

    supported_env_vars = ["SHORT_SHA", "COMMIT_HASH"]

    for env_var in supported_env_vars:
        short_sha = os.environ.get(env_var)
        if short_sha:
            return short_sha

    try:
        short_sha = (
            subprocess.check_output(
                ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL
            )
            .strip()
            .decode("utf-8")
        )
        # Check if there are uncommitted changes
        dirty_flag = subprocess.check_output(
            ["git", "status", "--porcelain"], stderr=subprocess.DEVNULL
        ).strip()

        if dirty_flag:
            short_sha += "-dirty"

        return short_sha

    except subprocess.CalledProcessError:
        raise RuntimeError(
            "Unable to get Git commit short SHA, you need to either run in an "
            "environment where git is available or set one of the env vars SHORT_SHA "
            "or COMMIT_HASH."
        )
