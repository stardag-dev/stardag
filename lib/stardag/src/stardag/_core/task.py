import abc
import typing

from stardag._core.base_task import BaseTask, LoadableTask, TargetTask
from stardag._core.validate import LoadValidator, get_validators, run_validators
from stardag.config import DEFAULT_TARGET_ROOT_KEY
from stardag.target import (
    FileSerializable,
    LoadableSaveableFileSystemTarget,
    get_directory_target,
    get_file_target,
)
from stardag.target.serialize import (
    DirectorySerializable,
    get_serializer,
    is_directory_serializer,
)

LoadedT = typing.TypeVar("LoadedT")


def get_default_relpath(
    task: BaseTask,
    *,
    base: str = "",
    extra: str = "",
    extension: str = "",
    filename: str = "",
) -> str:
    """Construct the default relative path for a task's target.

    This is the same path structure that ``Task`` and ``@task`` use automatically.
    Useful when you need a task-derived path outside of the ``Task`` class hierarchy.

    The path structure is::

        [<base>/][<namespace>/]<name>/[v<version>/][<extra>/]
        <id>[:2]/<id>[2:4]/<id>[/<filename>][.<extension>]

    Args:
        task: The task to derive the path from.
        base: Optional base path prefix.
        extra: Optional extra path component (inserted after version).
        extension: Optional file extension (without leading dot).
        filename: Optional filename (appended after the task ID).

    Returns:
        The constructed relative path string.
    """
    task_id_str = str(task.id)
    relpath = "/".join(
        [
            part
            for part in [
                base,
                task.get_namespace().replace(".", "/"),
                task.get_name(),
                f"v{task.version}" if task.version else "",
                extra,
                task_id_str[:2],
                task_id_str[2:4],
                task_id_str,
                filename,
            ]
            if part
        ]
    )
    if extension:
        relpath = f"{relpath}.{extension.lstrip('.')}"

    return relpath


class Task(
    TargetTask[LoadableSaveableFileSystemTarget[LoadedT]],
    LoadableTask[LoadedT],
    abc.ABC,
    typing.Generic[LoadedT],
):
    """A base class for tasks with automatic serialization and filesystem targets.

    The target of a ``Task`` is a ``LoadableSaveableFileSystemTarget`` that uses a
    serializer inferred from the generic type parameter ``LoadedT``.

    The target file path is automatically constructed based on the task's
    namespace, name, version, and unique ID and has the following structure:

    ```
    [<relpath_base>/][<namespace>/]<name>/v<version>/[<relpath_extra>/]
    <id>[:2]/<id>[2:4]/<id>[/<relpath_filename>].<relpath_extension>
    ```

    You can override the following properties to customize the target path:
    ``_relpath_base``, ``_relpath_extra``, ``_relpath_filename``, and
    ``_relpath_extension``.

    See ``stardag.target.serialize.get_serializer`` for details on how the serializer
    is inferred from the generic type parameter, and how to customize it.

    Example:

    ```python
    import stardag as sd

    class MyTask(sd.Task[dict[str, int]]):
        def run(self):
            self._save({"a": 1, "b": 2})

    my_task = MyTask()

    print(my_task.target())
    # FileSerializable(../MyTask/03/6f/036f6e71-1b3c-54b8-aec1-182359f1e09a.json)

    print(my_task.target().serializer)
    # <stardag.target.serialize.JSONSerializer at 0x1064e4710>
    ```
    """

    @classmethod
    def __map_generic_args_to_ancestor__(
        cls, ancestor_origin: type, args: tuple
    ) -> tuple | None:
        """Map generic args from Task to how they appear on an ancestor class.

        This enables type compatibility checking when using Task with
        polymorphic annotations like ``TaskLoads[T]`` and
        ``SubClass[TargetTask[LoadableTarget[T]]]``.

        Args:
            ancestor_origin: The ancestor class to map args to
            args: The generic args of this class (e.g., (str,) for Task[str])

        Returns:
            The mapped args for the ancestor, or None if mapping is not applicable.
        """
        if ancestor_origin is TargetTask and len(args) == 1:
            # Task[T] -> TargetTask[LoadableSaveableFileSystemTarget[T]]
            return (LoadableSaveableFileSystemTarget[args[0]],)
        if ancestor_origin is LoadableTask and len(args) == 1:
            # Task[T] -> LoadableTask[T] (identity mapping)
            return args
        return None

    _load_validators: tuple[LoadValidator, ...] = ()

    @classmethod
    def __pydantic_init_subclass__(cls, **kwargs: typing.Any) -> None:  # type: ignore
        super().__pydantic_init_subclass__(**kwargs)
        # get generic type of self
        orig_class = getattr(cls, "__orig_class__", None)
        if orig_class is None:
            return
        args = typing.get_args(orig_class)
        if not args:
            return
        loaded_t = args[0]
        if type(loaded_t) != typing.TypeVar:  # noqa: E721
            cls._serializer = get_serializer(loaded_t)
            cls._load_validators = get_validators(loaded_t)

    @property
    def _relpath_base(self) -> str:
        """Override to customize the base path of the task target."""
        return ""

    @property
    def _relpath_extra(self) -> str:
        """Override to customize the extra path component of the task target."""
        return ""

    @property
    def _relpath_filename(self) -> str:
        """Override to customize the filename of the task target."""
        return ""

    @property
    def _relpath_extension(self) -> str:
        """Override to customize the file extension of the task target."""
        get_default_ext = getattr(
            self.serializer, "get_default_extension", lambda: None
        )
        assert callable(get_default_ext)
        default_ext = get_default_ext()
        if default_ext is None:
            return ""

        assert isinstance(default_ext, str)
        return default_ext

    @property
    def _relpath(self) -> str:
        return get_default_relpath(
            self,
            base=self._relpath_base,
            extra=self._relpath_extra,
            extension=self._relpath_extension,
            filename=self._relpath_filename,
        )

    @property
    def _target_root_key(self) -> str:
        """Override to customize the target root key for this task's target."""
        return DEFAULT_TARGET_ROOT_KEY

    def target(self) -> LoadableSaveableFileSystemTarget[LoadedT]:
        if is_directory_serializer(self.serializer):
            return DirectorySerializable(
                wrapped=get_directory_target(
                    self._relpath, target_root_key=self._target_root_key
                ),
                serializer=self.serializer,
            )
        return FileSerializable(
            wrapped=get_file_target(
                self._relpath, target_root_key=self._target_root_key
            ),
            serializer=self.serializer,
        )

    @property
    def serializer(self):
        """The serializer used for this task's target."""
        return self._serializer

    def load(self) -> LoadedT:
        """Load the task target and run any ``LoadValidator``s."""
        value = self.target().load()
        return run_validators(self._load_validators, value)

    async def load_aio(self) -> LoadedT:
        """Async load — delegates to the target's ``load_aio`` and validates."""
        value = await self.target().load_aio()
        return run_validators(self._load_validators, value)

    def _save(self, data: LoadedT) -> None:
        """Validate data with any ``LoadValidator``s and save to the task target."""
        data = run_validators(self._load_validators, data)
        self.target().save(data)

    async def _save_aio(self, data: LoadedT) -> None:
        """Async validate and save — delegates to the target's ``save_aio``."""
        data = run_validators(self._load_validators, data)
        await self.target().save_aio(data)
