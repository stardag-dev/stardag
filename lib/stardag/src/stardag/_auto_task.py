import typing

from stardag._task import Task
from stardag.target import LoadableSaveableFileSystemTarget, Serializable, get_target
from stardag.target.serialize import get_serializer

LoadedT = typing.TypeVar("LoadedT")


class AutoTask(
    Task[LoadableSaveableFileSystemTarget[LoadedT]],
    typing.Generic[LoadedT],
):
    """A base class for automatically serializing task outputs.

    The output of an `AutoTask` is a `LoadableSaveableFileSystemTarget` that uses a
    serializer inferred from the generic type parameter `LoadedT`.

    The output file path is automatically constructed based on the task's
    namespace, name, version, and unique ID and has the following structure:

    ```
    [<relpath_base>/][<namespace>/]<name>/v<version>/[<relpath_extra>/]
    <id>[:2]/<id>[2:4]/<id>[/<relpath_filename>].<relpath_extension>
    ```

    You can override the following properties to customize the output path:
    `_relpath_base`, `_relpath_extra`, `_relpath_filename`, and `_relpath_extension`.

    See stardag.target.serialize.get_serializer for details on how the serializer is
    inferred from the generic type parameter, and how to customize it.

    Example:

    ```python
    import stardag as sd

    class MyAutoTask(sd.AutoTask[dict[str, int]]):
        def run(self):
            self.output().save({"a": 1, "b": 2})

    my_task = MyAutoTask()

    print(my_task.output())
    # Serializable(../MyAutoTask/03/6f/036f6e71-1b3c-54b8-aec1-182359f1e09a.json)

    print(my_task.output().serializer)
    # <stardag.target.serialize.JSONSerializer at 0x1064e4710>
    ```
    """

    @classmethod
    def __map_generic_args_to_ancestor__(
        cls, ancestor_origin: type, args: tuple
    ) -> tuple | None:
        """Map generic args from AutoTask to how they appear on an ancestor class.

        This enables type compatibility checking when using AutoTask with TaskLoads.
        For example, AutoTask[str] maps to Task[LoadableSaveableFileSystemTarget[str]],
        which is compatible with TaskLoads[str] (= Task[LoadableTarget[str]]) because
        LoadableSaveableFileSystemTarget is a subtype of LoadableTarget.

        Args:
            ancestor_origin: The ancestor class to map args to (e.g., Task)
            args: The generic args of this class (e.g., (str,) for AutoTask[str])

        Returns:
            The mapped args for the ancestor, or None if mapping is not applicable.
        """
        if ancestor_origin is Task and len(args) == 1:
            # AutoTask[T] -> Task[LoadableSaveableFileSystemTarget[T]]
            return (LoadableSaveableFileSystemTarget[args[0]],)
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
        """Override to customize the base path of the task output."""
        return ""

    @property
    def _relpath_extra(self) -> str:
        """Override to customize the extra path component of the task output."""
        return ""

    @property
    def _relpath_filename(self) -> str:
        """Override to customize the filename of the task output."""
        return ""

    @property
    def _relpath_extension(self) -> str:
        """Override to customize the file extension of the task output."""
        get_default_ext = getattr(
            self._serializer, "get_default_extension", lambda: None
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

    def output(self) -> LoadableSaveableFileSystemTarget[LoadedT]:
        return Serializable(
            wrapped=get_target(self._relpath),
            serializer=self._serializer,
        )
