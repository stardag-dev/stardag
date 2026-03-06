import typing

from stardag import Task, auto_namespace
from stardag.target import FileSerializable
from stardag.target.serialize import JSONSerializer, PlainTextSerializer

auto_namespace(__name__)


class IntTask(Task[int]):
    value: int

    def run(self):
        self._save(self.value)


class StrTask(Task[str]):
    value: str

    def run(self):
        self._save(self.value)


class DictTask(Task[dict[str, int]]):
    data: dict[str, int]

    def run(self):
        self._save(self.data)


class TestSerializerInference:
    """Tests that the correct serializer is inferred from the generic type."""

    def test_int_type_uses_json_serializer(self):
        assert isinstance(IntTask._serializer, JSONSerializer)

    def test_str_type_uses_plain_text_serializer(self):
        assert isinstance(StrTask._serializer, PlainTextSerializer)

    def test_dict_type_uses_json_serializer(self):
        assert isinstance(DictTask._serializer, JSONSerializer)


class TestTargetPath:
    """Tests for the automatic output path construction."""

    def test_relpath_contains_namespace(self):
        task = IntTask(value=42)
        assert __name__.replace(".", "/") in task._relpath

    def test_relpath_contains_name(self):
        task = IntTask(value=42)
        assert "IntTask" in task._relpath

    def test_relpath_contains_task_id(self):
        task = IntTask(value=42)
        task_id_str = str(task.id)
        assert task_id_str in task._relpath

    def test_relpath_contains_id_prefix_dirs(self):
        task = IntTask(value=42)
        task_id_str = str(task.id)
        # ID is split into prefix directories: id[:2]/id[2:4]/id
        assert f"/{task_id_str[:2]}/{task_id_str[2:4]}/" in task._relpath

    def test_relpath_includes_version_when_set(self):
        task = IntTask(value=42, version="1.0")
        assert "v1.0" in task._relpath

    def test_relpath_excludes_version_when_empty(self):
        task = IntTask(value=42, version="")
        assert "/v/" not in task._relpath
        assert "/v" not in task._relpath.split("/")

    def test_relpath_has_json_extension_for_int(self):
        task = IntTask(value=42)
        assert task._relpath.endswith(".json")

    def test_relpath_has_txt_extension_for_str(self):
        task = StrTask(value="hello")
        assert task._relpath.endswith(".txt")


class TestCustomPathComponents:
    """Tests for customizing path components via property overrides."""

    def test_custom_relpath_base(self):
        class CustomBaseTask(Task[int]):
            value: int

            @property
            def _relpath_base(self) -> str:
                return "custom/base"

            def run(self):
                self._save(self.value)

        task = CustomBaseTask(value=42)
        assert task._relpath.startswith("custom/base/")

    def test_custom_relpath_extra(self):
        class CustomExtraTask(Task[int]):
            value: int

            @property
            def _relpath_extra(self) -> str:
                return "extra/path"

            def run(self):
                self._save(self.value)

        task = CustomExtraTask(value=42)
        assert "/extra/path/" in task._relpath

    def test_custom_relpath_filename(self):
        class CustomFilenameTask(Task[int]):
            value: int

            @property
            def _relpath_filename(self) -> str:
                return "result"

            def run(self):
                self._save(self.value)

        task = CustomFilenameTask(value=42)
        assert task._relpath.endswith("/result.json")

    def test_custom_relpath_extension(self):
        class CustomExtensionTask(Task[int]):
            value: int

            @property
            def _relpath_extension(self) -> str:
                return "custom"

            def run(self):
                self._save(self.value)

        task = CustomExtensionTask(value=42)
        assert task._relpath.endswith(".custom")


class TestTarget:
    """Tests for the target() method."""

    def test_target_returns_serializable(self):
        task = IntTask(value=42)
        target = task.target()
        assert isinstance(target, FileSerializable)

    def test_target_has_correct_serializer(self):
        task = IntTask(value=42)
        target = task.target()
        assert isinstance(target, FileSerializable)
        assert isinstance(target.serializer, JSONSerializer)

    def test_target_uri_matches_relpath(self):
        task = IntTask(value=42)
        target = task.target()
        # The uri includes the target prefix (e.g., "in-memory://")
        assert target.uri.endswith(task._relpath)


class TestRunAndSave:
    """Tests for the full run and save workflow."""

    def test_run_saves_int_value(self, default_in_memory_fs_target):
        task = IntTask(value=42)
        task.run()
        assert task.target().load() == 42

    def test_run_saves_str_value(self, default_in_memory_fs_target):
        task = StrTask(value="hello world")
        task.run()
        assert task.target().load() == "hello world"

    def test_run_saves_dict_value(self, default_in_memory_fs_target):
        task = DictTask(data={"a": 1, "b": 2})
        task.run()
        assert task.target().load() == {"a": 1, "b": 2}

    def test_complete_returns_false_before_run(self, default_in_memory_fs_target):
        task = IntTask(value=42)
        assert not task.complete()

    def test_complete_returns_true_after_run(self, default_in_memory_fs_target):
        task = IntTask(value=42)
        task.run()
        assert task.complete()


class TestGenericTypeVar:
    """Tests for generic Task subclasses with unresolved TypeVars."""

    def test_concrete_subclass_of_generic_sets_correct_serializer(self):
        """A concrete subclass of a generic Task should set the correct _serializer."""
        T = typing.TypeVar("T")

        class GenericTask(Task[T], typing.Generic[T]):
            value: T  # type: ignore

            def run(self):
                self._save(self.value)

        class ConcreteIntTask(GenericTask[int]):
            pass

        class ConcreteStrTask(GenericTask[str]):
            pass

        assert isinstance(ConcreteIntTask._serializer, JSONSerializer)
        assert isinstance(ConcreteStrTask._serializer, PlainTextSerializer)
