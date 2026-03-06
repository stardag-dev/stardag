import abc
import typing

from stardag._core.base_task import LoadableTask, TargetTask
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
        task_id_str = str(self.id)
        relpath = "/".join(
            [
                part
                for part in [
                    self._relpath_base,
                    self.get_namespace().replace(".", "/"),
                    self.get_name(),
                    f"v{self.version}" if self.version else "",
                    self._relpath_extra,
                    task_id_str[:2],
                    task_id_str[2:4],
                    task_id_str,
                    self._relpath_filename,
                ]
                if part
            ]
        )
        extension = self._relpath_extension
        if extension:
            relpath = f"{relpath}.{extension.lstrip('.')}"

        return relpath

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
        """Convenience method to load the task target."""
        return self.target().load()

    async def load_aio(self) -> LoadedT:
        """Async load — delegates to the target's ``load_aio``."""
        return await self.target().load_aio()

    def _save(self, data: LoadedT) -> None:
        """Convenience method to save data to the task target."""
        self.target().save(data)

    async def _save_aio(self, data: LoadedT) -> None:
        """Async save — delegates to the target's ``save_aio``."""
        await self.target().save_aio(data)
