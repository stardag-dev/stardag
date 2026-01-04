import typing

from stardag._auto_task import AutoTask
from stardag._task import auto_namespace
from stardag.target import Serializable
from stardag.target.serialize import JSONSerializer, PlainTextSerializer

auto_namespace(__name__)


class IntAutoTask(AutoTask[int]):
    value: int

    def run(self):
        self.output().save(self.value)


class StrAutoTask(AutoTask[str]):
    value: str

    def run(self):
        self.output().save(self.value)


class DictAutoTask(AutoTask[dict[str, int]]):
    data: dict[str, int]

    def run(self):
        self.output().save(self.data)


class TestSerializerInference:
    """Tests that the correct serializer is inferred from the generic type."""

    def test_int_type_uses_json_serializer(self):
        assert isinstance(IntAutoTask._serializer, JSONSerializer)

    def test_str_type_uses_plain_text_serializer(self):
        assert isinstance(StrAutoTask._serializer, PlainTextSerializer)

    def test_dict_type_uses_json_serializer(self):
        assert isinstance(DictAutoTask._serializer, JSONSerializer)


class TestOutputPath:
    """Tests for the automatic output path construction."""

    def test_relpath_contains_namespace(self):
        task = IntAutoTask(value=42)
        assert __name__.replace(".", "/") in task._relpath

    def test_relpath_contains_name(self):
        task = IntAutoTask(value=42)
        assert "IntAutoTask" in task._relpath

    def test_relpath_contains_task_id(self):
        task = IntAutoTask(value=42)
        task_id_str = str(task.id)
        assert task_id_str in task._relpath

    def test_relpath_contains_id_prefix_dirs(self):
        task = IntAutoTask(value=42)
        task_id_str = str(task.id)
        # ID is split into prefix directories: id[:2]/id[2:4]/id
        assert f"/{task_id_str[:2]}/{task_id_str[2:4]}/" in task._relpath

    def test_relpath_includes_version_when_set(self):
        task = IntAutoTask(value=42, version="1.0")
        assert "v1.0" in task._relpath

    def test_relpath_excludes_version_when_empty(self):
        task = IntAutoTask(value=42, version="")
        assert "/v/" not in task._relpath
        assert "/v" not in task._relpath.split("/")

    def test_relpath_has_json_extension_for_int(self):
        task = IntAutoTask(value=42)
        assert task._relpath.endswith(".json")

    def test_relpath_has_txt_extension_for_str(self):
        task = StrAutoTask(value="hello")
        assert task._relpath.endswith(".txt")


class TestCustomPathComponents:
    """Tests for customizing path components via property overrides."""

    def test_custom_relpath_base(self):
        class CustomBaseTask(AutoTask[int]):
            value: int

            @property
            def _relpath_base(self) -> str:
                return "custom/base"

            def run(self):
                self.output().save(self.value)

        task = CustomBaseTask(value=42)
        assert task._relpath.startswith("custom/base/")

    def test_custom_relpath_extra(self):
        class CustomExtraTask(AutoTask[int]):
            value: int

            @property
            def _relpath_extra(self) -> str:
                return "extra/path"

            def run(self):
                self.output().save(self.value)

        task = CustomExtraTask(value=42)
        assert "/extra/path/" in task._relpath

    def test_custom_relpath_filename(self):
        class CustomFilenameTask(AutoTask[int]):
            value: int

            @property
            def _relpath_filename(self) -> str:
                return "result"

            def run(self):
                self.output().save(self.value)

        task = CustomFilenameTask(value=42)
        assert task._relpath.endswith("/result.json")

    def test_custom_relpath_extension(self):
        class CustomExtensionTask(AutoTask[int]):
            value: int

            @property
            def _relpath_extension(self) -> str:
                return "custom"

            def run(self):
                self.output().save(self.value)

        task = CustomExtensionTask(value=42)
        assert task._relpath.endswith(".custom")


class TestOutput:
    """Tests for the output() method."""

    def test_output_returns_serializable(self):
        task = IntAutoTask(value=42)
        output = task.output()
        assert isinstance(output, Serializable)

    def test_output_has_correct_serializer(self):
        task = IntAutoTask(value=42)
        output = task.output()
        assert isinstance(output, Serializable)
        assert isinstance(output.serializer, JSONSerializer)

    def test_output_path_matches_relpath(self):
        task = IntAutoTask(value=42)
        output = task.output()
        # The path includes the target prefix (e.g., "in-memory://")
        assert output.path.endswith(task._relpath)


class TestRunAndSave:
    """Tests for the full run and save workflow."""

    def test_run_saves_int_value(self, default_in_memory_fs_target):
        task = IntAutoTask(value=42)
        task.run()
        assert task.output().load() == 42

    def test_run_saves_str_value(self, default_in_memory_fs_target):
        task = StrAutoTask(value="hello world")
        task.run()
        assert task.output().load() == "hello world"

    def test_run_saves_dict_value(self, default_in_memory_fs_target):
        task = DictAutoTask(data={"a": 1, "b": 2})
        task.run()
        assert task.output().load() == {"a": 1, "b": 2}

    def test_complete_returns_false_before_run(self, default_in_memory_fs_target):
        task = IntAutoTask(value=42)
        assert not task.complete()

    def test_complete_returns_true_after_run(self, default_in_memory_fs_target):
        task = IntAutoTask(value=42)
        task.run()
        assert task.complete()


class TestGenericTypeVar:
    """Tests for generic AutoTask subclasses with unresolved TypeVars."""

    def test_concrete_subclass_of_generic_sets_correct_serializer(self):
        """A concrete subclass of a generic AutoTask should set the correct _serializer."""
        T = typing.TypeVar("T")

        class GenericTask(AutoTask[T], typing.Generic[T]):
            value: T  # type: ignore

            def run(self):
                self.output().save(self.value)

        class ConcreteIntTask(GenericTask[int]):
            pass

        class ConcreteStrTask(GenericTask[str]):
            pass

        assert isinstance(ConcreteIntTask._serializer, JSONSerializer)
        assert isinstance(ConcreteStrTask._serializer, PlainTextSerializer)
