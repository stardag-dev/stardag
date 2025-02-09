import typing
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.target import FileSystemTarget, LocalTarget
from stardag.target._base import RemoteFileSystemTarget
from stardag.task import Task
from stardag.utils.resource_provider import resource_provider

# A class or callable that takes a (fully qualifed/"absolute") path (/uri) and returns
# a FileSystemTarget.
_TargetFromPath = (
    typing.Type[FileSystemTarget] | typing.Callable[[str], FileSystemTarget]
)


@typing.runtime_checkable
class TargetFromPathFromURIProtocol(typing.Protocol):
    def __call__(self, uri: str) -> _TargetFromPath: ...


PrefixToTargetFromPath = typing.Mapping[str, _TargetFromPath]

_DEFAULT_PREFIX_TO_TARGET_FROM_PATH: dict[str, _TargetFromPath] = {
    "/": LocalTarget,
}

try:
    from stardag.integration.aws.s3 import s3_rfs_provider

    def s3_target_from_path(path: str) -> FileSystemTarget:
        return RemoteFileSystemTarget(path=path, rfs=s3_rfs_provider.get())

    _DEFAULT_PREFIX_TO_TARGET_FROM_PATH["s3://"] = s3_target_from_path
except ImportError:
    pass


class TargetFromPathByPrefix(TargetFromPathFromURIProtocol):
    def __init__(
        self,
        prefix_to_target_from_path: PrefixToTargetFromPath = _DEFAULT_PREFIX_TO_TARGET_FROM_PATH,
    ) -> None:
        self.prefix_to_target_from_path = prefix_to_target_from_path

    def __call__(self, uri: str) -> _TargetFromPath:
        for prefix, target_from_path in self.prefix_to_target_from_path.items():
            if uri.startswith(prefix):
                return target_from_path
        raise ValueError(f"URI {uri} does not match any prefixes.")


DEFAULT_TARGET_ROOT_KEY = "default"
_DEFAULT_TARGET_ROOTS = {
    DEFAULT_TARGET_ROOT_KEY: str(
        Path("~/.stardag/target-roots/default").expanduser().absolute()
    ),
}


class TargetFactoryConfig(BaseSettings):
    root: dict[str, str] = _DEFAULT_TARGET_ROOTS

    model_config = SettingsConfigDict(env_prefix="stardag_target_")


class TargetFactory:
    def __init__(
        self,
        target_roots: dict[str, str] = _DEFAULT_TARGET_ROOTS,
        target_from_path_by_prefix: (
            PrefixToTargetFromPath | TargetFromPathFromURIProtocol | None
        ) = None,
    ) -> None:
        self.target_roots = {
            key: value.removesuffix("/") + "/" for key, value in target_roots.items()
        }
        self.target_from_path_by_prefix = (
            target_from_path_by_prefix
            if isinstance(target_from_path_by_prefix, TargetFromPathFromURIProtocol)
            else TargetFromPathByPrefix(target_from_path_by_prefix)
            if target_from_path_by_prefix is not None
            else TargetFromPathByPrefix()
        )

    @classmethod
    def from_config(cls, config: TargetFactoryConfig) -> "TargetFactory":
        return cls(target_roots=config.root)

    def get_target(
        self,
        relpath: str,
        task: Task | None,  # noqa
        target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
    ) -> FileSystemTarget:
        """Get a file system target.

        Args:
            relpath: The path to the target, relative to the configured root path for
              `target_root_key`.
            task: The task that will use the target. NOTE: this can be used to for
              advanced configuration of targets, such as in-memory/local disk caching
              etc.
            target_root: The key to the target root to use.
        """
        path = self.get_path(relpath, target_root_key)
        target_from_path = self.target_from_path_by_prefix(path)
        return target_from_path(path)

    def get_path(
        self, relpath: str, target_root_key: str = DEFAULT_TARGET_ROOT_KEY
    ) -> str:
        return f"{self.target_roots[target_root_key]}{relpath}"


target_factory_provider = resource_provider(
    type_=TargetFactory,
    default_factory=lambda: TargetFactory.from_config(TargetFactoryConfig()),
)


def get_target(
    relpath: str,
    task: Task | None,
    target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
) -> FileSystemTarget:
    return target_factory_provider.get().get_target(
        relpath=relpath,
        task=task,
        target_root_key=target_root_key,
    )
