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
    from stardag.build.registry._api_registry import APIRegistry
    from stardag.config import config_provider

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
