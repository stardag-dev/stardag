import json
import typing

from stardag._task import BaseTask
from stardag.config import DEFAULT_TARGET_ROOT_KEY, config_provider
from stardag.target import DirectoryTarget, FileSystemTarget, LocalTarget
from stardag.target._base import RemoteFileSystemTarget
from stardag.utils.resource_provider import resource_provider

# A class or callable that takes a (fully qualifed/"absolute") path (/uri) and returns
# a FileSystemTarget.
TargetPrototype = (
    typing.Type[FileSystemTarget] | typing.Callable[[str], FileSystemTarget]
)


PrefixToTargetPrototype = typing.Mapping[str, TargetPrototype]


def get_default_prefix_to_target_prototype() -> dict[str, TargetPrototype]:
    prefix_to_target_prototype: dict[str, TargetPrototype] = {
        "/": LocalTarget,
    }
    # S3 integration
    try:
        from stardag.integration.aws.s3 import s3_rfs_provider

        def s3_target_from_path(path: str) -> FileSystemTarget:
            return RemoteFileSystemTarget(path=path, rfs=s3_rfs_provider.get())

        prefix_to_target_prototype["s3://"] = s3_target_from_path
    except ImportError:
        pass

    # Modal integration
    try:
        from stardag.integration.modal._target import get_modal_target

        prefix_to_target_prototype["modalvol://"] = get_modal_target
    except ImportError:
        pass

    return prefix_to_target_prototype


class TargetFactory:
    def __init__(
        self,
        target_roots: dict[str, str] | None = None,
        prefixt_to_target_prototype: PrefixToTargetPrototype | None = None,
    ) -> None:
        # If no target_roots provided, get from central config
        if target_roots is None:
            target_roots = config_provider.get().target.roots

        self.target_roots = {
            key: value.removesuffix("/") + "/" for key, value in target_roots.items()
        }
        self.prefixt_to_target_prototype = (
            prefixt_to_target_prototype or get_default_prefix_to_target_prototype()
        )

    def get_target(
        self,
        relpath: str,
        task: BaseTask | None = None,  # noqa
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

        Returns:
            A file system target.
        """
        if self._is_full_path(relpath):
            path = relpath
        else:
            path = self.get_path(relpath, target_root_key)
        target_prototype = self._get_target_prototype(path)
        return target_prototype(path)

    def get_directory_target(
        self,
        relpath: str,
        task: BaseTask | None = None,  # noqa
        target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
    ) -> DirectoryTarget:
        """Get a directory target.

        Args:
            relpath: The path to the target, relative to the configured root path for
              `target_root_key`.
            task: The task that will use the target. NOTE: this can be used to for
              advanced configuration of targets, such as in-memory/local disk caching
              etc.
            target_root: The key to the target root to use.

        Returns:
            A directory target.
        """
        if self._is_full_path(relpath):
            path = relpath
        else:
            path = self.get_path(relpath, target_root_key)
        target_prototype = self._get_target_prototype(path)
        return DirectoryTarget(path, target_prototype)

    def get_path(
        self, relpath: str, target_root_key: str = DEFAULT_TARGET_ROOT_KEY
    ) -> str:
        """Get the full (/"absolute") path (/"URI") to the target."""
        target_root = self.target_roots.get(target_root_key)
        if target_root is None:
            example_json = json.dumps({target_root_key: "...", "default": "..."})
            raise ValueError(
                f"No target root is configured for key: '{target_root_key}'. "
                f"Available keys are: {list(self.target_roots.keys())}. Set the missing "
                "target root in your registry config or via environment variable: "
                f"`STARDAG_TARGET_ROOTS='{example_json}'`."
            )

        return f"{target_root}{relpath}"

    def _get_target_prototype(self, path: str) -> TargetPrototype:
        for prefix, target_prototype in self.prefixt_to_target_prototype.items():
            if path.startswith(prefix):
                return target_prototype
        raise ValueError(
            f"URI {path} does not match any of the configured prefixes: "
            f"{list(self.prefixt_to_target_prototype.keys())}."
        )

    def _is_full_path(self, path: str) -> bool:
        for prefix in self.prefixt_to_target_prototype.keys():
            if path.startswith(prefix):
                return True

        return False


target_factory_provider = resource_provider(
    type_=TargetFactory,
    default_factory=TargetFactory,
)


def get_target(
    relpath: str,
    task: BaseTask | None = None,
    target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
) -> FileSystemTarget:
    return target_factory_provider.get().get_target(
        relpath=relpath,
        task=task,
        target_root_key=target_root_key,
    )


def get_directory_target(
    relpath: str,
    task: BaseTask | None = None,
    target_root_key: str = DEFAULT_TARGET_ROOT_KEY,
) -> DirectoryTarget:
    return target_factory_provider.get().get_directory_target(
        relpath=relpath,
        task=task,
        target_root_key=target_root_key,
    )
