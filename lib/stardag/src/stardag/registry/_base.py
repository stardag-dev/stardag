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
    from stardag._registry_asset import RegistryAsset
    from stardag._task import BaseTask


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
    provide at least the `register_task` method. All other methods have default
    no-op implementations for backwards compatibility.
    """

    @abc.abstractmethod
    def register_task(self, task: "BaseTask") -> None:
        """Register a task as pending/scheduled.

        This is called when a task is about to be executed.
        """
        pass

    def start_build(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> str | None:
        """Start a new build session.

        Called at the beginning of a build. Returns a build ID if the
        registry supports build tracking, None otherwise.

        Args:
            root_tasks: The root tasks being built
            description: Optional description of the build
        """
        return None

    def complete_build(self) -> None:
        """Mark the current build as completed successfully."""
        pass

    def fail_build(self, error_message: str | None = None) -> None:
        """Mark the current build as failed.

        Args:
            error_message: Optional error message describing the failure
        """
        pass

    def start_task(self, task: "BaseTask") -> None:
        """Mark a task as started/running.

        Called immediately before a task begins execution.
        """
        pass

    def complete_task(self, task: "BaseTask") -> None:
        """Mark a task as completed successfully.

        Called after a task finishes execution without errors.
        """
        pass

    def fail_task(self, task: "BaseTask", error_message: str | None = None) -> None:
        """Mark a task as failed.

        Called when a task raises an exception during execution.

        Args:
            task: The task that failed
            error_message: Optional error message describing the failure
        """
        pass

    def upload_task_assets(
        self, task: "BaseTask", assets: list["RegistryAsset"]
    ) -> None:
        """Upload assets for a completed task.

        Called after a task completes successfully if it has registry assets.

        Args:
            task: The completed task
            assets: List of assets to upload
        """
        pass

    def suspend_task(self, task: "BaseTask") -> None:
        """Mark a task as suspended waiting for dynamic dependencies.

        Called when a task yields dynamic deps that are not yet complete.
        The task will remain suspended until its dynamic deps are built.

        Args:
            task: The task that is suspended
        """
        pass

    def resume_task(self, task: "BaseTask") -> None:
        """Mark a task as resumed after dynamic dependencies completed.

        Called when a task's dynamic dependencies are complete and
        the task is ready to continue execution (either by resuming
        a suspended generator or by re-executing the task).

        Args:
            task: The task that is resuming
        """
        pass

    def cancel_build(self) -> None:
        """Cancel the current build.

        Called when a build is explicitly cancelled by the user.
        """
        pass

    def exit_early(self, reason: str | None = None) -> None:
        """Mark the build as exited early.

        Called when all remaining tasks are running in other builds
        and this build should stop waiting.

        Args:
            reason: Optional reason for exiting early
        """
        pass

    def cancel_task(self, task: "BaseTask") -> None:
        """Cancel a task.

        Called when a task is explicitly cancelled by the user.

        Args:
            task: The task to cancel
        """
        pass

    def task_waiting_for_lock(
        self, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Record that a task is waiting for a global lock.

        Called when a task cannot acquire its lock because another
        build is holding it.

        Args:
            task: The task waiting for the lock
            lock_owner: Optional identifier of who holds the lock
        """
        pass

    # Async versions - default implementations delegate to sync methods

    async def register_task_aio(self, task: "BaseTask") -> None:
        """Async version of register_task."""
        self.register_task(task)

    async def start_build_aio(
        self,
        root_tasks: list["BaseTask"] | None = None,
        description: str | None = None,
    ) -> str | None:
        """Async version of start_build."""
        return self.start_build(root_tasks, description)

    async def complete_build_aio(self) -> None:
        """Async version of complete_build."""
        self.complete_build()

    async def fail_build_aio(self, error_message: str | None = None) -> None:
        """Async version of fail_build."""
        self.fail_build(error_message)

    async def start_task_aio(self, task: "BaseTask") -> None:
        """Async version of start_task."""
        self.start_task(task)

    async def complete_task_aio(self, task: "BaseTask") -> None:
        """Async version of complete_task."""
        self.complete_task(task)

    async def fail_task_aio(
        self, task: "BaseTask", error_message: str | None = None
    ) -> None:
        """Async version of fail_task."""
        self.fail_task(task, error_message)

    async def upload_task_assets_aio(
        self, task: "BaseTask", assets: list["RegistryAsset"]
    ) -> None:
        """Async version of upload_task_assets."""
        self.upload_task_assets(task, assets)

    async def suspend_task_aio(self, task: "BaseTask") -> None:
        """Async version of suspend_task."""
        self.suspend_task(task)

    async def resume_task_aio(self, task: "BaseTask") -> None:
        """Async version of resume_task."""
        self.resume_task(task)

    async def cancel_build_aio(self) -> None:
        """Async version of cancel_build."""
        self.cancel_build()

    async def exit_early_aio(self, reason: str | None = None) -> None:
        """Async version of exit_early."""
        self.exit_early(reason)

    async def cancel_task_aio(self, task: "BaseTask") -> None:
        """Async version of cancel_task."""
        self.cancel_task(task)

    async def task_waiting_for_lock_aio(
        self, task: "BaseTask", lock_owner: str | None = None
    ) -> None:
        """Async version of task_waiting_for_lock."""
        self.task_waiting_for_lock(task, lock_owner)


class NoOpRegistry(RegistryABC):
    """A registry that does nothing.

    Used as a default when no registry is configured.
    """

    def register_task(self, task: "BaseTask") -> None:
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
