import json
import typing

import pytest

from stardag import Task, auto_namespace, get_default_relpath
from stardag.polymorphic import PolymorphicRoot as _PolymorphicRoot
from stardag.target import DirectorySerializable, FileSerializable
from stardag.target._base import DirectoryTarget
from stardag.target.serialize import (
    JSONSerializer,
    PlainTextSerializer,
    SelfDirectorySerializing,
)

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


class _DirData(SelfDirectorySerializing):
    def __init__(self, data: dict) -> None:
        self.data = data

    def dump(self, target: DirectoryTarget) -> None:
        with (target / "data.json").open("w") as f:
            f.write(json.dumps(self.data))
        target.mark_done()

    @classmethod
    def load(cls, target: DirectoryTarget) -> typing.Self:
        with (target / "data.json").open("r") as f:
            return cls(json.loads(f.read()))


class DirTask(Task[_DirData]):
    def run(self):
        self._save(_DirData({"result": 1}))


class TestGetDefaultRelpath:
    """Tests for the standalone get_default_relpath utility."""

    def test_matches_task_relpath(self):
        """get_default_relpath produces the same result as Task._relpath."""
        task = IntTask(value=42)
        result = get_default_relpath(task, extension="json")
        assert result == task._relpath

    def test_matches_task_relpath_with_version(self):
        task = IntTask(value=42, version="1.0")
        result = get_default_relpath(task, extension="json")
        assert result == task._relpath

    def test_basic_structure(self):
        task = IntTask(value=42)
        task_id_str = str(task.id)
        result = get_default_relpath(task)
        parts = result.split("/")
        # Should contain: namespace parts, name, id prefix dirs, id
        assert "IntTask" in parts
        assert task_id_str[:2] in parts
        assert task_id_str[2:4] in parts
        assert task_id_str in parts

    def test_with_base(self):
        task = IntTask(value=42)
        result = get_default_relpath(task, base="my/base")
        assert result.startswith("my/base/")

    def test_with_extra(self):
        task = IntTask(value=42)
        result = get_default_relpath(task, extra="extra/path")
        assert "/extra/path/" in result

    def test_with_extension(self):
        task = IntTask(value=42)
        result = get_default_relpath(task, extension="json")
        assert result.endswith(".json")

    def test_with_dotted_extension(self):
        task = IntTask(value=42)
        result = get_default_relpath(task, extension=".json")
        assert result.endswith(".json")
        assert not result.endswith("..json")

    def test_with_filename(self):
        task = IntTask(value=42)
        result = get_default_relpath(task, filename="output", extension="csv")
        assert result.endswith("/output.csv")

    def test_with_all_params(self):
        task = IntTask(value=42, version="2")
        task_id_str = str(task.id)
        result = get_default_relpath(
            task, base="base", extra="extra", extension="json", filename="result"
        )
        assert result.startswith("base/")
        assert "/v2/" in result
        assert "/extra/" in result
        assert result.endswith(f"/{task_id_str}/result.json")

    def test_no_extension(self):
        task = IntTask(value=42)
        result = get_default_relpath(task)
        assert not result.endswith(".json")
        # Should end with just the task id
        assert result.endswith(str(task.id))

    def test_empty_version_excluded(self):
        task = IntTask(value=42, version="")
        result = get_default_relpath(task)
        assert "/v/" not in result
        assert "/v" not in result.split("/")


class TestDirectoryTarget:
    """Tests that Task with a directory serializer returns DirectorySerializable."""

    def test_target_returns_directory_serializable(self):
        task = DirTask()
        target = task.target()
        assert isinstance(target, DirectorySerializable)

    def test_run_saves_and_loads(self, default_in_memory_fs_target):
        task = DirTask()
        task.run()
        assert task.complete()
        loaded = task.load()
        assert isinstance(loaded, _DirData)
        assert loaded.data == {"result": 1}


class TestDirectGenericInstantiation:
    """Generic user tasks can be instantiated directly without a concrete subclass.

    Prior behavior required ``class Concrete(Generic[int]): pass`` boilerplate when
    the type parameter was only meant as a type-hint convenience — direct
    instantiation raised ``AttributeError: __type_id__`` at serialization time.
    """

    def test_direct_instantiation_of_user_generic_task(
        self, default_in_memory_fs_target
    ):
        ItemT = typing.TypeVar("ItemT")

        class GenericListTask(Task[list[ItemT]], typing.Generic[ItemT]):
            items: list[ItemT]

            def run(self):
                self._save(list(self.items))

        task: GenericListTask[int] = GenericListTask(items=[1, 2, 3])

        # Serialization works (previously failed with AttributeError: __type_id__)
        dumped = task.model_dump(mode="json")
        assert dumped["items"] == [1, 2, 3]

        # Deterministic id works (depends on model_dump with hash context)
        assert task.id is not None

        # Build + load round-trip works
        task.run()
        assert task.complete()
        assert task.load() == [1, 2, 3]

    def test_generic_task_has_type_id(self):
        ItemT = typing.TypeVar("ItemT")

        class RegisteredGeneric(Task[ItemT], typing.Generic[ItemT]):
            value: int

            def run(self):
                self._save(self.value)  # type: ignore[arg-type]

        assert hasattr(RegisteredGeneric, "__type_id__")
        assert RegisteredGeneric.__type_id__.name == "RegisteredGeneric"

    def test_parameterized_alias_does_not_get_registered(self):
        """``Task[int]`` must not grab its own type id — only user classes do."""
        ItemT = typing.TypeVar("ItemT")

        class MyGeneric(Task[ItemT], typing.Generic[ItemT]):
            value: int

            def run(self):
                self._save(self.value)  # type: ignore[arg-type]

        alias = MyGeneric[int]
        assert not hasattr(alias, "__type_id__") or (
            alias.__type_id__ is MyGeneric.__type_id__
        )


class TestGenericTypeArgTransfer:
    """Parameterized-at-call-site generics (``TestGeneric[int](...)``) transfer
    their resolved type args in the serialized payload so distinct parameteriza-
    tions get distinct ids and round-trip back to the correct parameterized class.
    """

    def test_distinct_parameterizations_have_distinct_ids(self):
        ItemT = typing.TypeVar("ItemT")

        class ParamTask(Task[list[ItemT]], typing.Generic[ItemT]):
            items: list[ItemT]

            def run(self):
                self._save(list(self.items))

        int_task: ParamTask[int] = ParamTask[int](items=[])
        str_task: ParamTask[str] = ParamTask[str](items=[])
        bare = ParamTask(items=[])

        assert int_task.id != str_task.id
        assert int_task.id != bare.id
        assert str_task.id != bare.id

    def test_round_trip_preserves_parameterization(self):
        from pydantic import TypeAdapter

        from stardag import BaseTask
        from stardag.polymorphic import SubClass

        ItemT = typing.TypeVar("ItemT")

        class ParamTask(Task[list[ItemT]], typing.Generic[ItemT]):
            items: list[ItemT]

            def run(self):
                self._save(list(self.items))

        original: ParamTask[int] = ParamTask[int](items=[1, 2, 3])
        dumped = original.model_dump(mode="json")

        # Wire format carries the pickled type args.
        assert "__type_args" in dumped

        adapter = TypeAdapter(SubClass[BaseTask])
        restored = adapter.validate_python(dumped)

        # Class reconstructed with correct parameterization.
        assert type(restored).__name__ == "ParamTask[int]"
        assert typing.get_args(getattr(restored, "__orig_class__")) == (int,)
        # Id survives round-trip.
        assert restored.id == original.id

    def test_concrete_subclass_skips_type_arg_transfer(self):
        """``class Concrete(Gen[int])`` does not emit ``__type_args`` — its name
        discriminator already determines the class fully."""
        ItemT = typing.TypeVar("ItemT")

        class ParamTask(Task[list[ItemT]], typing.Generic[ItemT]):
            items: list[ItemT]

            def run(self):
                self._save(list(self.items))

        class IntParamTask(ParamTask[int]):
            pass

        task = IntParamTask(items=[1, 2])
        dumped = task.model_dump(mode="json")
        assert "__type_args" not in dumped
        assert dumped["__name"] == "IntParamTask"


class _TypeVarRoundTripBase(_PolymorphicRoot):
    """Module-level PolymorphicRoot family used by the round-trip test below.

    The pickle-transfer mechanism needs module-level classes (pickle can't
    find locally-defined ones by qualname), so this base and its subclass
    must live at module scope.
    """

    pass


class _TypeVarRoundTripConcrete(_TypeVarRoundTripBase):
    payload: int = 0


class TestGenericTaskWithSubClassField:
    """A generic task can use a ``TypeVar`` bound to a ``PolymorphicRoot`` as a
    ``SubClass[...]`` field annotation, so the polymorphic base narrows with the
    task's type parameter.

    Pattern:

        class BaseParam(PolymorphicRoot):
            ...

        ParamT = TypeVar("ParamT", bound=BaseParam)

        class GenericTask(Task[...], Generic[ParamT]):
            param: SubClass[ParamT]

        GenericTask[ConcreteParam](param=ConcreteParam(), ...)

    The generic form builds a schema using the TypeVar's bound as the dispatch
    target (so ``GenericTask(param=...)`` accepts any ``BaseParam`` subclass).
    Pydantic re-invokes schema construction for each parameterized form,
    narrowing the dispatch to the concrete type.
    """

    def test_generic_class_with_subclass_typevar_field_can_be_declared(self):
        """Previously raised ``TypeError: Polymorphic() can only be used with
        PolymorphicRoot subclasses`` at class-body evaluation because source_type
        was the unresolved TypeVar."""
        from stardag.polymorphic import PolymorphicRoot, SubClass

        class BaseParam(PolymorphicRoot):
            pass

        ParamT = typing.TypeVar("ParamT", bound=BaseParam)

        class GenericTask(Task[int], typing.Generic[ParamT]):
            param: SubClass[ParamT]
            value: int

            def run(self):
                self._save(self.value)

        assert hasattr(GenericTask, "__type_id__")
        assert GenericTask.__type_id__.name == "GenericTask"

    def test_narrowing_rejects_wrong_concrete_subclass(self):
        """``GenericTask[ConcreteA](param=ConcreteB())`` is rejected by Pydantic's
        strict schema for the parameterized form."""
        from pydantic import ValidationError

        from stardag.polymorphic import PolymorphicRoot, SubClass

        class BaseParam(PolymorphicRoot):
            pass

        class ConcreteA(BaseParam):
            a: int = 1

        class ConcreteB(BaseParam):
            b: int = 2

        ParamT = typing.TypeVar("ParamT", bound=BaseParam)

        class GenericTask(Task[int], typing.Generic[ParamT]):
            param: SubClass[ParamT]
            value: int

            def run(self):
                self._save(self.value)

        # Correct parameterization: accepted.
        ok = GenericTask[ConcreteA](param=ConcreteA(a=5), value=1)
        assert isinstance(ok.param, ConcreteA)

        # Mismatched concrete: rejected.
        with pytest.raises(ValidationError):
            GenericTask[ConcreteA](param=ConcreteB(), value=1)  # type: ignore[arg-type]

    def test_round_trip_preserves_typevar_parameterization(self):
        """Serialize + dispatch on ``SubClass[BaseTask]`` reconstructs the
        parameterized class; the ``SubClass[ParamT]`` field still resolves
        polymorphically inside.

        Uses module-level classes for the polymorphic family because the
        pickle-transferred ``__type_args`` need qualname-addressable types.
        """
        from pydantic import TypeAdapter

        from stardag import BaseTask
        from stardag.polymorphic import SubClass

        ParamT = typing.TypeVar("ParamT", bound=_TypeVarRoundTripBase)

        class _RTGenericTask(Task[int], typing.Generic[ParamT]):
            param: SubClass[ParamT]
            value: int

            def run(self):
                self._save(self.value)

        original = _RTGenericTask[_TypeVarRoundTripConcrete](
            param=_TypeVarRoundTripConcrete(payload=7), value=3
        )
        dumped = original.model_dump(mode="json")
        assert "__type_args" in dumped

        adapter = TypeAdapter(SubClass[BaseTask])
        restored = adapter.validate_python(dumped)
        assert type(restored).__name__ == "_RTGenericTask[_TypeVarRoundTripConcrete]"
        assert isinstance(restored, _RTGenericTask)
        assert isinstance(restored.param, _TypeVarRoundTripConcrete)
        assert restored.id == original.id

    def test_typevar_without_polymorphic_bound_raises(self):
        """A TypeVar without a PolymorphicRoot bound can't drive polymorphic
        dispatch — declaring such a field must raise at schema-build time."""
        from stardag.polymorphic import SubClass

        ParamT = typing.TypeVar("ParamT")  # no bound

        with pytest.raises(TypeError, match="TypeVar"):

            class GenericTask(Task[int], typing.Generic[ParamT]):
                param: SubClass[ParamT]  # type: ignore[type-var]
                value: int

                def run(self):
                    self._save(self.value)
