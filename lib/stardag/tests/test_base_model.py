import enum
import warnings
from typing import Annotated, Any, Generic, Type, TypeVar

import pytest
from pydantic import ValidationError, WrapSerializer

from stardag.base_model import (
    CONTEXT_MODE_KEY,
    SerializationContextMode,
    StardagBaseModel,
    StardagField,
    ValidationContextMode,
)


class ModelPlain(StardagBaseModel):
    a: int


class ModelWithCompatDefault(StardagBaseModel):
    a: Annotated[int, StardagField(compat_default=0)]


class ModelWithCompatDefaultAndRegularDefault(StardagBaseModel):
    a: Annotated[int, StardagField(compat_default=0)] = 5


class ModelWrongRegularDefaultType(StardagBaseModel):
    a: Annotated[int, StardagField()] = "string_instead_of_int"  # type: ignore


class ModelWrongCompatDefaultType(StardagBaseModel):
    a: Annotated[int, StardagField(compat_default="string_instead_of_int")]


@pytest.mark.parametrize(
    "description,cls,data,mode,expected",
    [
        (
            "plain model no mode",
            ModelPlain,
            {"a": 0},
            None,
            ModelPlain(a=0),
        ),
        (
            "plain model compat mode",
            ModelPlain,
            {"a": 0},
            "compat",
            ModelPlain(a=0),
        ),
        (
            "plain model no mode missing value",
            ModelPlain,
            {},
            None,
            pytest.raises(ValidationError),
        ),
        (
            "plain model compat mode missing value",
            ModelPlain,
            {},
            "compat",
            pytest.raises(ValidationError),
        ),
        # With compat default (no regular default):
        (
            "with compat default no mode",
            ModelWithCompatDefault,
            {"a": 1},
            None,
            ModelWithCompatDefault(a=1),
        ),
        (
            "with compat default no mode missing value",
            ModelWithCompatDefault,
            {},
            None,
            pytest.raises(ValidationError),
        ),
        (
            "with compat default compat mode no value",
            ModelWithCompatDefault,
            {},
            "compat",
            ModelWithCompatDefault(a=0),
        ),
        (
            "with compat default compat mode with value",
            ModelWithCompatDefault,
            {"a": 1},
            "compat",
            ModelWithCompatDefault(a=1),
        ),
        # With compat default and regular default:
        (
            "with compat and regular default no mode",
            ModelWithCompatDefaultAndRegularDefault,
            {},
            None,
            ModelWithCompatDefaultAndRegularDefault(a=5),
        ),
        (
            "with compat and regular default compat mode",
            ModelWithCompatDefaultAndRegularDefault,
            {},
            "compat",
            ModelWithCompatDefaultAndRegularDefault(a=0),
        ),
        (
            "with compat and regular default compat mode with value",
            ModelWithCompatDefaultAndRegularDefault,
            {"a": 10},
            "compat",
            ModelWithCompatDefaultAndRegularDefault(a=10),
        ),
        (
            "with compat and regular default no mode with value",
            ModelWithCompatDefaultAndRegularDefault,
            {"a": 10},
            None,
            ModelWithCompatDefaultAndRegularDefault(a=10),
        ),
        # Wrong regular default type:
        (
            "wrong regular default type",
            ModelWrongRegularDefaultType,
            {},
            None,
            pytest.raises(ValidationError),
        ),
        (
            "wrong regular default type compat mode",
            ModelWrongRegularDefaultType,
            {},
            "compat",
            pytest.raises(ValidationError),
        ),
        (
            "wrong regular default type with value",
            ModelWrongRegularDefaultType,
            {"a": 5},
            None,
            ModelWrongRegularDefaultType(a=5),
        ),
        (
            "wrong regular default type with value compat mode",
            ModelWrongRegularDefaultType,
            {"a": 5},
            "compat",
            ModelWrongRegularDefaultType(a=5),
        ),
        # Wrong compat default type:
        (
            "wrong compat default type",
            ModelWrongCompatDefaultType,
            {},
            None,
            pytest.raises(ValidationError),
        ),
        (
            "wrong compat default type compat mode",
            ModelWrongCompatDefaultType,
            {},
            "compat",
            pytest.raises(ValidationError),
        ),
        (
            "wrong compat default type with value",
            ModelWrongCompatDefaultType,
            {"a": 5},
            None,
            ModelWrongCompatDefaultType(a=5),
        ),
        (
            "wrong compat default type with value compat mode",
            ModelWrongCompatDefaultType,
            {"a": 5},
            "compat",
            ModelWrongCompatDefaultType(a=5),
        ),
    ],
)
def test_stardag_base_model_validate(
    description: str,
    cls: Type[StardagBaseModel],
    data: dict,
    mode: ValidationContextMode,
    expected: StardagBaseModel | pytest.RaisesExc,
):
    if isinstance(expected, pytest.RaisesExc):
        with expected:
            cls.model_validate(data, context={"mode": mode})
    else:
        actual = cls.model_validate(
            data,
            context=({CONTEXT_MODE_KEY: mode} if mode else None),
        )
        assert actual == expected, f"Failed: {description}"


# ``hash_exclude`` is deprecated in favour of ``significance``; it keeps its
# hash-mode behaviour (and, unlike the new levels, still allows init) for one
# release, which is what these expectations pin.
with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)

    class ModelWithHashExclude(StardagBaseModel):
        a: Annotated[int, StardagField(hash_exclude=True)]
        b: int


def test_hash_exclude_is_deprecated() -> None:
    with pytest.warns(DeprecationWarning, match="execution_only"):
        StardagField(hash_exclude=True)


class Color(str, enum.Enum):  # `str, Enum` for Python 3.10 (StrEnum is 3.11+)
    RED = "red"
    GREEN = "green"


class ModelWithEnumTupleCompatDefault(StardagBaseModel):
    """Enum-tuple field whose serialized form (list of strings) differs from
    its Python value (tuple of enums). See issue #146."""

    colors: Annotated[tuple[Color, ...], StardagField(compat_default=(Color.RED,))] = (
        Color.RED,
    )


def _hash_serialize(value: Any, handler, info):
    """Hash-only custom serializer producing a form != the Python value."""
    if info.context and info.context.get(CONTEXT_MODE_KEY) == "hash":
        return f"<{value}>"
    return handler(value)


Canonical = Annotated[float, WrapSerializer(_hash_serialize)]


class ModelWithCustomSerializerCompatDefault(StardagBaseModel):
    """Field with a hash-only custom serializer; the serialized form
    (``"<0.0>"``) differs from the Python value (``0.0``). See issue #146."""

    weight: Annotated[Canonical, StardagField(compat_default=0.0)] = 0.0


@pytest.mark.parametrize(
    "description,instance,mode,expected",
    [
        (
            "plain model",
            ModelPlain(a=10),
            None,
            {"a": 10},
        ),
        (
            "with compat default",
            ModelWithCompatDefault(a=0),
            None,
            {"a": 0},
        ),
        (
            "with compat default",
            ModelWithCompatDefault(a=0),
            "hash",
            {},
        ),
        (
            "with hash exclude",
            ModelWithHashExclude(a=5, b=10),
            None,
            {"a": 5, "b": 10},
        ),
        (
            "with hash exclude hash mode",
            ModelWithHashExclude(a=5, b=10),
            "hash",
            {"b": 10},
        ),
        # Enum-tuple compat default (issue #146): serialized form is a list of
        # strings, but the field is at its compat default -> dropped in hash
        # mode, kept (serialized) otherwise.
        (
            "enum-tuple compat default no mode at default",
            ModelWithEnumTupleCompatDefault(colors=(Color.RED,)),
            None,
            {"colors": ["red"]},
        ),
        (
            "enum-tuple compat default hash mode at default",
            ModelWithEnumTupleCompatDefault(colors=(Color.RED,)),
            "hash",
            {},
        ),
        (
            "enum-tuple compat default hash mode not at default",
            ModelWithEnumTupleCompatDefault(colors=(Color.GREEN,)),
            "hash",
            {"colors": ["green"]},
        ),
        # Custom hash-only serializer compat default (issue #146): serialized
        # form ("<0.0>") differs from the Python value (0.0).
        (
            "custom serializer compat default no mode at default",
            ModelWithCustomSerializerCompatDefault(weight=0.0),
            None,
            {"weight": 0.0},
        ),
        (
            "custom serializer compat default hash mode at default",
            ModelWithCustomSerializerCompatDefault(weight=0.0),
            "hash",
            {},
        ),
        (
            "custom serializer compat default hash mode not at default",
            ModelWithCustomSerializerCompatDefault(weight=1.5),
            "hash",
            {"weight": "<1.5>"},
        ),
    ],
)
def test_stardag_base_model_serialize(
    description: str,
    instance: StardagBaseModel,
    mode: SerializationContextMode,
    expected: dict | pytest.RaisesExc,
):
    if isinstance(expected, pytest.RaisesExc):
        with expected:
            instance.model_dump(
                mode="json",
                context={CONTEXT_MODE_KEY: mode},
            )
    else:
        actual = instance.model_dump(
            mode="json",
            context={CONTEXT_MODE_KEY: mode},
        )
        assert actual == expected, f"Failed: {description}"


# ---------------------------------------------------------------------------
# The three levels of parameter significance on the model: resolved from the
# build config, refused at init, dropped from the hash and registry payloads.
# The config module itself is tested in ``tests/test_build_config.py``; the
# scope key in ``tests/test_build/test_scope.py``. Design:
# ``docs/design/scope-keyed-dependency-structure.md``.
# ---------------------------------------------------------------------------

import stardag as sd  # noqa: E402
from stardag.base_model import field_significance  # noqa: E402
from stardag.build_config import (  # noqa: E402
    BuildConfigError,
    build_config_scope,
    canonical_structure_config,
    get_build_config,
    get_build_config_class,
    rebind_to_build_config,
)
from stardag.polymorphic import PolymorphicRoot  # noqa: E402
from stardag.target import InMemoryTarget  # noqa: E402


class Fanout(sd.Task[int]):
    __namespace__ = "sig_tests"
    __version__ = "1"

    key: str
    partition_size: Annotated[int, StardagField(significance="dependencies_only")] = 100
    threads: Annotated[int, StardagField(significance="execution_only")] = 1

    def run(self) -> None:
        self.target().save(self.partition_size)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


class Required(sd.Task[int]):
    """A level 2 field with no default: the config must supply it."""

    __namespace__ = "sig_tests"
    key: str
    width: Annotated[int, StardagField(significance="dependencies_only")]

    def run(self) -> None:
        return None


class Holder(sd.Task[int]):
    """A task whose parameter is a configured task."""

    __namespace__ = "sig_tests"
    inner: Fanout

    def run(self) -> None:
        return None


KEY = "sig_tests.Fanout"


class TestSignificanceOnTheModel:
    def test_defaults_apply_without_a_build_config(self):
        task = Fanout(key="a")
        assert (task.partition_size, task.threads) == (100, 1)
        assert get_build_config() is None

    @pytest.mark.parametrize("field", ["partition_size", "threads"])
    def test_non_identity_fields_cannot_be_passed_at_init(self, field: str):
        with pytest.raises(ValidationError, match="build config"):
            # Plain validation is init: only ``mode="compat"`` (registry data)
            # tolerates the field being present.
            Fanout.model_validate({"key": "a", field: 7})

    def test_values_are_resolved_from_the_build_config(self):
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            task = Fanout(key="a")
        assert (task.partition_size, task.threads) == (250, 8)

    def test_levels_two_and_three_do_not_move_the_id(self):
        plain = Fanout(key="a")
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            configured = Fanout(key="a")
        assert configured.id == plain.id

    def test_registry_mode_dump_carries_identity_only(self):
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            task = Fanout(key="a")
        data = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        assert "partition_size" not in data and "threads" not in data
        assert data["key"] == "a"
        assert data["__name"] == "Fanout"
        # ...and the ordinary dump still shows the effective values.
        assert task.model_dump()["partition_size"] == 250

    def test_compat_validation_strips_a_stale_value(self):
        """Registry data written before significance existed carries the
        value; rehydration drops it and resolves from the config instead."""
        with build_config_scope({KEY: {"partition_size": 9}}):
            task = Fanout.model_validate(
                {"key": "a", "version": "1", "partition_size": 42, "threads": 3},
                context={CONTEXT_MODE_KEY: "compat"},
            )
        assert task.partition_size == 9
        assert task.threads == 1

    def test_compat_mode_lets_the_config_win_over_a_stale_value(self):
        """Both present: the stored value is old data, the config is the
        build's; the config wins for every non-identity field it names."""
        with build_config_scope({KEY: {"partition_size": 9, "threads": 4}}):
            task = Fanout.model_validate(
                {"key": "a", "version": "1", "partition_size": 42, "threads": 3},
                context={CONTEXT_MODE_KEY: "compat"},
            )
        assert (task.partition_size, task.threads) == (9, 4)

    def test_rebind_re_resolves_from_the_installed_config(self):
        task = Fanout(key="a")
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebound = rebind_to_build_config(task)
        assert isinstance(rebound, Fanout)
        assert rebound.partition_size == 3
        assert rebound.id == task.id

    def test_a_required_level_two_field_needs_the_config(self):
        # ``model_validate`` rather than the constructor: the field has no
        # default, so the static signature demands it, while at runtime the
        # build config is the only place it may come from.
        with pytest.raises(ValidationError, match="width"):
            Required.model_validate({"key": "a"})
        with build_config_scope({"sig_tests.Required": {"width": 5}}):
            assert Required.model_validate({"key": "a"}).width == 5

    def test_compat_default_is_refused_on_a_non_identity_field(self):
        with pytest.raises(ValueError, match="compat_default"):
            StardagField(compat_default=1, significance="execution_only")

    def test_hash_exclude_reads_as_execution_only(self):
        with pytest.warns(DeprecationWarning):
            legacy = StardagField(hash_exclude=True)
        assert legacy.effective_significance == "execution_only"
        assert not legacy.is_identity
        assert field_significance(Fanout.model_fields["key"]) == "identity"
        assert (
            field_significance(Fanout.model_fields["partition_size"])
            == "dependencies_only"
        )

    def test_field_significance_by_kind(self):
        with pytest.warns(DeprecationWarning):

            class Mixed(StardagBaseModel):
                plain: int = 0
                legacy: Annotated[int, StardagField(hash_exclude=True)] = 0
                explicit: Annotated[
                    int, StardagField(significance="execution_only")
                ] = 0
                compat: Annotated[int, StardagField(compat_default=0)] = 0

        fields = Mixed.model_fields
        assert field_significance(fields["plain"]) == "identity"
        assert field_significance(fields["legacy"]) == "execution_only"
        assert field_significance(fields["explicit"]) == "execution_only"
        assert field_significance(fields["compat"]) == "identity"

    def test_non_identity_fields_are_the_build_config_fields_and_cached(self):
        # Not ``Mixed``: a model with a build-config field is indexed under
        # its bare class name, and the one above already holds that key.
        with pytest.warns(DeprecationWarning):

            class MixedCached(StardagBaseModel):
                plain: int = 0
                legacy: Annotated[int, StardagField(hash_exclude=True)] = 0
                deps: Annotated[int, StardagField(significance="dependencies_only")] = 0
                exec_: Annotated[int, StardagField(significance="execution_only")] = 0

        first = MixedCached._non_identity_fields()
        assert first == ("deps", "exec_")
        assert MixedCached._non_identity_fields() is first

    def test_registry_dump_drops_nested_non_identity_fields_too(self):
        """A configured task nested as a parameter is serialised by its own
        class, under the same context, so its level 2/3 fields are dropped
        from the outer payload as well."""
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            holder = Holder(inner=Fanout(key="a"))
        data = holder.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        assert data["inner"]["key"] == "a"
        assert "partition_size" not in data["inner"]
        assert "threads" not in data["inner"]
        # And rehydration under compat mode resolves the nested task from the
        # config installed at that point.
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebuilt = Holder.model_validate(data, context={CONTEXT_MODE_KEY: "compat"})
        assert rebuilt.inner.partition_size == 3
        assert rebuilt.id == holder.id


class TestLegacyHashExcludeInPayloads:
    """A deprecated ``hash_exclude=True`` field may still be passed at init,
    so a task registered with a non-default value must rehydrate with it:
    dropped from the hash, kept in the registry payload. An explicit
    ``execution_only`` field is in neither — its value lives in the build
    config."""

    @pytest.fixture
    def legacy(self):
        with pytest.warns(DeprecationWarning):

            class Legacy(sd.Task[int]):
                __namespace__ = "sig_legacy"
                key: str
                knob: Annotated[int, StardagField(hash_exclude=True)] = 1
                threads: Annotated[int, StardagField(significance="execution_only")] = 1

                def run(self):
                    return None

        return Legacy

    def test_hash_excluded_value_is_stored_but_not_hashed(self, legacy):
        task = legacy(key="a", knob=7)
        registry_data = task.model_dump(
            mode="json", context={CONTEXT_MODE_KEY: "registry"}
        )
        hash_data = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "hash"})
        assert registry_data["knob"] == 7
        assert "knob" not in hash_data
        assert "threads" not in registry_data and "threads" not in hash_data
        # And the id does not move with the knob.
        assert legacy(key="a", knob=7).id == legacy(key="a").id

    def test_a_stored_value_rehydrates(self, legacy):
        data = legacy(key="a", knob=7).model_dump(
            mode="json", context={CONTEXT_MODE_KEY: "registry"}
        )
        rebuilt = legacy.model_validate(data, context={CONTEXT_MODE_KEY: "compat"})
        assert rebuilt.knob == 7


class TestSignificanceIsChecked:
    def test_hash_exclude_with_an_explicit_significance_is_refused(self):
        """The deprecated flag and an explicit non-identity significance
        disagree about init (one allows it, the other refuses), so the pair
        is contradictory rather than redundant."""
        with pytest.warns(DeprecationWarning):
            with pytest.raises(ValueError, match="contradictory"):
                StardagField(hash_exclude=True, significance="execution_only")
        with pytest.warns(DeprecationWarning):
            legacy = StardagField(hash_exclude=True)
        assert legacy.is_legacy_hash_exclude and not legacy.is_build_config_field
        assert legacy.effective_significance == "execution_only"
        explicit = StardagField(significance="dependencies_only")
        assert explicit.is_build_config_field and not explicit.is_legacy_hash_exclude

    def test_a_typo_is_refused_at_field_creation(self):
        """A Literal is a hint; an unchecked typo would read as non-identity
        on the model and as execution-only in the structure hash."""
        with pytest.raises(ValueError, match="dependencies_only") as excinfo:
            StardagField(significance="dependencies-only")  # type: ignore[arg-type]
        assert "identity" in str(excinfo.value)
        assert "execution_only" in str(excinfo.value)


class TestBuildConfigRegistration:
    """Which classes a build config may name, and under which key.

    A model that declares a level 2 or 3 field is looked up by key when the
    build's structure scope is hashed, so it has to be findable then — not
    only when a field is resolved at validation. Tasks are found through the
    task registry; this is the other half (STA-77).
    """

    def test_a_model_with_a_build_config_field_is_registered_by_its_name(self):
        class Indexed(StardagBaseModel):
            a: int = 0
            threads: Annotated[int, StardagField(significance="execution_only")] = 1

        assert get_build_config_class("Indexed") is Indexed

    def test_a_namespace_separates_the_key(self):
        class Namespaced(StardagBaseModel):
            __namespace__ = "sig_tests"
            threads: Annotated[int, StardagField(significance="execution_only")] = 1

        assert get_build_config_class("sig_tests.Namespaced") is Namespaced
        assert get_build_config_class("Namespaced") is None
        assert Namespaced._build_config_key() == "sig_tests.Namespaced"

    def test_a_polymorphic_model_is_keyed_by_its_registered_type_id(self):
        """A non-task polymorphic model is keyed like a task: the namespace
        and name it was *registered* under, overrides included. That id is
        set as the class is defined, so this index is filled after it."""

        class Strategy(PolymorphicRoot):
            __namespace__ = "sig_tests"

        class Chunked(Strategy, namespace_override="other_ns"):
            threads: Annotated[int, StardagField(significance="execution_only")] = 1

        assert Chunked._build_config_key() == "other_ns.Chunked"
        assert get_build_config_class("other_ns.Chunked") is Chunked
        assert get_build_config_class("sig_tests.Chunked") is None
        # ...and the scope hash resolves that key.
        assert canonical_structure_config({"other_ns.Chunked": {"threads": 8}}) == {}
        with build_config_scope({"other_ns.Chunked": {"threads": 8}}):
            assert Chunked().threads == 8

    def test_a_model_without_build_config_fields_is_not_registered(self):
        class Ordinary(StardagBaseModel):
            a: int = 0

        assert get_build_config_class("Ordinary") is None

    def test_a_legacy_hash_exclude_model_is_not_registered(self):
        """``hash_exclude=True`` is passable at init and needs no config
        entry, so it does not make the class nameable."""
        with pytest.warns(DeprecationWarning):

            class LegacyOnly(StardagBaseModel):
                threads: Annotated[int, StardagField(hash_exclude=True)] = 1

        assert get_build_config_class("LegacyOnly") is None

    def test_a_parameterized_generic_alias_is_not_registered(self):
        T = TypeVar("T")

        class Box(StardagBaseModel, Generic[T]):
            item: T
            threads: Annotated[int, StardagField(significance="execution_only")] = 1

        assert Box[int].__name__ == "Box[int]"
        assert get_build_config_class("Box") is Box
        assert get_build_config_class("Box[int]") is None

    def test_a_task_class_is_left_to_the_task_registry(self):
        assert get_build_config_class(KEY) is None
        assert get_build_config_class("Fanout") is None
        # ...and the config still resolves it, through that registry.
        assert canonical_structure_config({KEY: {"partition_size": 7}}) == {
            KEY: {"partition_size": 7}
        }

    def test_two_models_with_one_key_are_refused_at_definition(self):
        # Two *different* classes with one key: different qualified names,
        # as two modules each defining a ``Duplicated`` would have. (Same
        # module and qualified name is a re-creation, below.)
        def first():
            class Duplicated(StardagBaseModel):
                threads: Annotated[int, StardagField(significance="execution_only")] = 1

            return Duplicated

        def second():
            class Duplicated(StardagBaseModel):
                threads: Annotated[int, StardagField(significance="execution_only")] = 2

            return Duplicated

        first()
        with pytest.raises(BuildConfigError) as excinfo:
            second()

        message = str(excinfo.value)
        assert "'Duplicated'" in message
        # Both classes are named, so the user can find the two definitions.
        assert ".first.<locals>.Duplicated" in message
        assert ".second.<locals>.Duplicated" in message
        assert "__namespace__" in message

    def test_a_namespace_resolves_a_collision(self):
        class Separated(StardagBaseModel):
            threads: Annotated[int, StardagField(significance="execution_only")] = 1

        class Separated2(StardagBaseModel):
            __namespace__ = "other_ns"
            threads: Annotated[int, StardagField(significance="execution_only")] = 2

        # Same class name, different keys: rename the second to match the
        # first and only the namespace keeps them apart.
        Separated2.__name__ = "Separated"
        assert get_build_config_class("Separated") is Separated
        assert get_build_config_class("other_ns.Separated2") is Separated2

    def test_a_class_recreated_under_the_same_name_replaces_it(self):
        """What cloudpickle does to a by-value class in a worker: the class
        object is new, its module and qualified name are not."""

        def define():
            class Recreated(StardagBaseModel):
                threads: Annotated[int, StardagField(significance="execution_only")] = 1

            return Recreated

        first, second = define(), define()
        assert first is not second
        assert get_build_config_class("Recreated") is second
