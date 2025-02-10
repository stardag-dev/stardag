import abc
import datetime
import getpass
import os
import subprocess
from functools import lru_cache

from pydantic import BaseModel
from pydantic_settings import SettingsConfigDict

from stardag.target import get_target
from stardag.task import Task
from stardag.task_parameter import TaskParam
from stardag.utils.resource_provider import resource_provider


class RegisterdTaskEnvelope(BaseModel):
    task: TaskParam[Task]
    task_id: str
    user: str
    created_at: datetime.datetime
    commit_hash: str

    @classmethod
    def new(cls, task: Task):
        return cls(
            task=task,
            task_id=task.task_id,
            user=getpass.getuser(),
            created_at=datetime.datetime.now(datetime.timezone.utc),
            commit_hash=get_git_commit_hash(),
        )


class RegistryABC(metaclass=abc.ABCMeta):
    @abc.abstractmethod
    def register(self, task: Task):
        pass


class FileSystemRegistryConfig(BaseModel):
    root: str | None = None

    model_config = SettingsConfigDict(env_prefix="stardag_fs_registry_")


class FileSystemRegistry(RegistryABC):
    def __init__(self, registry_root: str):
        self.registry_root = registry_root.removesuffix("/") + "/"

    def register(self, task: Task):
        envelope = RegisterdTaskEnvelope.new(task)
        target = get_target(self.get_target_path(task), task)
        with target.open("w") as handle:
            handle.write(envelope.model_dump_json())

    def get_target_path(self, task: Task):
        task_id = task.task_id
        task_path = "/".join(
            [
                part
                for part in [
                    task.get_namespace().replace(".", "/"),
                    task.get_family(),
                    f"v{task.version}" if task.version else "",
                    task_id[:2],
                    task_id[2:4],
                    task_id,
                ]
                if part
            ]
        )
        return f"{self.registry_root}{task_path}.json"


class NoOpRegistry(RegistryABC):
    def register(self, task: Task):
        pass


def init_registry():
    fs_registry_config = FileSystemRegistryConfig()
    if fs_registry_config.root:
        return FileSystemRegistry(fs_registry_config.root)

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
