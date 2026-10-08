import dataclasses
import enum
import logging
from typing import Annotated, Any, Literal, Type

import pytest
from pydantic import ConfigDict, ValidationError, WrapSerializer

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


class ModelWithNonSignificant(StardagBaseModel):
    a: Annotated[int, StardagField(significant=False)]
    b: int


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
            "with compat default registry mode keeps the value",
            ModelWithCompatDefault(a=0),
            "registry",
            {"a": 0},
        ),
        (
            "non-significant",
            ModelWithNonSignificant(a=5, b=10),
            None,
            {"a": 5, "b": 10},
        ),
        (
            "non-significant hash mode",
            ModelWithNonSignificant(a=5, b=10),
            "hash",
            {"b": 10},
        ),
        (
            "non-significant registry mode keeps every field",
            ModelWithNonSignificant(a=5, b=10),
            "registry",
            {"a": 5, "b": 10},
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
# ``StardagField(significant=...)``: the one flag of v2. A non-significant
# field is an ordinary parameter (passed at init, stored in the body) that
# is left out of the task id only. Design: docs/design/registry-v2/design.md,
# "Two hashes, one flag". The two hashes themselves, the stability check and
# conflict detection are in tests/test__core/test_instance.py.
# ---------------------------------------------------------------------------

import stardag as sd  # noqa: E402
from stardag.base_model import is_significant  # noqa: E402


class Fanout(sd.Task[int]):
    __namespace__ = "sig_tests"
    __version__ = "1"

    key: str
    partition_size: Annotated[int, StardagField(significant=False)] = 100
    threads: Annotated[int, StardagField(significant=False)] = 1

    def run(self) -> None:
        return None


class Holder(sd.Task[int]):
    """A task whose parameter is a task with non-significant fields."""

    __namespace__ = "sig_tests"
    inner: Fanout

    def run(self) -> None:
        return None


class Options(StardagBaseModel):
    """A nested plain model carrying ``significant`` on its own fields."""

    pattern: str
    max_workers: Annotated[int, StardagField(significant=False)] = 4


class Parse(sd.Task[int]):
    __namespace__ = "sig_tests"
    options: Options

    def run(self) -> None:
        return None


class TestStardagField:
    def test_defaults(self):
        field = StardagField()
        assert field.significant is True
        assert StardagField(significant=False).significant is False

    @pytest.mark.parametrize("removed", ["significance", "hash_exclude"])
    def test_removed_options_are_a_hard_error_naming_the_replacement(
        self, removed: str
    ):
        value = "execution_only" if removed == "significance" else True
        with pytest.raises(TypeError, match=r"significant=False"):
            StardagField(**{removed: value})  # type: ignore[arg-type]

    def test_an_unknown_option_is_refused(self):
        with pytest.raises(TypeError, match="nope"):
            StardagField(nope=1)  # type: ignore[arg-type]

    def test_significant_must_be_a_bool(self):
        with pytest.raises(TypeError, match="bool"):
            StardagField(significant="no")  # type: ignore[arg-type]

    def test_compat_default_is_refused_on_a_non_significant_field(self):
        with pytest.raises(ValueError, match="compat_default"):
            StardagField(compat_default=1, significant=False)

    def test_compat_default_on_a_significant_field_is_fine(self):
        assert StardagField(compat_default=1).compat_default == 1

    def test_is_significant(self):
        fields = Fanout.model_fields
        assert is_significant(fields["key"])
        assert not is_significant(fields["partition_size"])

    def test_fields_are_frozen_and_comparable(self):
        field = StardagField(significant=False)
        assert field == StardagField(significant=False)
        assert field != StardagField()
        with pytest.raises(AttributeError):
            field.significant = True  # type: ignore[misc]


class TestNonSignificantFields:
    def test_passable_at_init(self):
        task = Fanout(key="a", partition_size=7, threads=2)
        assert (task.partition_size, task.threads) == (7, 2)

    def test_do_not_move_the_task_id(self):
        assert Fanout(key="a", partition_size=7).id == Fanout(key="a").id
        assert Fanout(key="a").id != Fanout(key="b").id

    def test_hash_mode_drops_them_and_registry_mode_keeps_them(self):
        task = Fanout(key="a", partition_size=7)
        registry = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        assert registry["partition_size"] == 7
        assert registry["threads"] == 1  # defaults included
        # The hash-mode dump of a task finalizes to its id; check the fields
        # through a nested plain model instead, which does not finalize.
        options = Options(pattern="*", max_workers=9)
        assert options.model_dump(mode="json", context={CONTEXT_MODE_KEY: "hash"}) == {
            "pattern": "*"
        }

    def test_a_nested_task_carries_its_own_flags(self):
        a = Holder(inner=Fanout(key="a", partition_size=1))
        b = Holder(inner=Fanout(key="a", partition_size=2))
        assert a.id == b.id
        body = a.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        assert body["inner"]["partition_size"] == 1

    def test_a_nested_plain_model_carries_its_own_flags(self):
        a = Parse(options=Options(pattern="*.log", max_workers=1))
        b = Parse(options=Options(pattern="*.log", max_workers=8))
        assert a.id == b.id
        assert Parse(options=Options(pattern="*.txt")).id != a.id
        assert a.instance_hash != b.instance_hash

    def test_rehydrate_from_the_registry_mode_dump(self):
        task = Holder(inner=Fanout(key="a", partition_size=3))
        body = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        rebuilt = Holder.model_validate(body, context={CONTEXT_MODE_KEY: "compat"})
        assert rebuilt == task
        assert rebuilt.inner.partition_size == 3


class Legacy(StardagBaseModel):
    scale: float
    tau: float


class New(StardagBaseModel):
    # Disjoint from Legacy by shape: no required field in common.
    model_config = ConfigDict(extra="forbid")
    type: Literal["new"] = "new"
    quantiles: tuple[float, ...] = (0.1, 0.5, 0.9)


class Encoded(StardagBaseModel):
    # Undiscriminated: a discriminator would change Legacy's stored hash.
    encoding: Legacy | New


class Loose(StardagBaseModel):
    a: int = 0


class Exact(StardagBaseModel):
    a: int = 0
    b: int = 0


class SmartUnion(StardagBaseModel):
    # Both members validate {"a", "b"}; pydantic's smart mode keeps the one
    # that set more fields.
    value: Loose | Exact


_DROP_MESSAGE = "Dropping stored field(s)"


def _drop_warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if _DROP_MESSAGE in r.getMessage()]


def _compat(cls: Type[StardagBaseModel], data: dict[str, Any]) -> Any:
    return cls.model_validate(data, context={CONTEXT_MODE_KEY: "compat"})


class TestCompatDroppedKeysWarning:
    def test_a_probed_union_member_does_not_warn(self, caplog):
        stored = Encoded(encoding=New()).model_dump(mode="json")
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = _compat(Encoded, stored)
        assert rebuilt == Encoded(encoding=New())
        assert _drop_warnings(caplog) == []

    def test_an_outscored_union_member_does_not_warn(self, caplog):
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = _compat(SmartUnion, {"value": {"a": 1, "b": 2}})
        assert rebuilt == SmartUnion(value=Exact(a=1, b=2))
        assert _drop_warnings(caplog) == []

    def test_a_key_dropped_by_the_winning_member_warns_once(self, caplog):
        stored = {"encoding": {"scale": 1.0, "tau": 2.0, "removed": 3}}
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = _compat(Encoded, stored)
        assert rebuilt == Encoded(encoding=Legacy(scale=1.0, tau=2.0))
        assert _drop_warnings(caplog) == [
            "Dropping stored field(s) 'removed' unknown to "
            f"{__name__}.Legacy while rehydrating: the class no longer "
            "declares them."
        ]

    def test_a_key_dropped_at_the_top_level_warns_once(self, caplog):
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            _compat(ModelPlain, {"a": 1, "removed": 2})
        assert len(_drop_warnings(caplog)) == 1

    def test_repeated_drops_by_one_class_warn_once(self, caplog):
        class Many(StardagBaseModel):
            items: list[Legacy]

        item = {"scale": 1.0, "tau": 2.0, "removed": 3}
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = _compat(Many, {"items": [item, item, item]})
        assert len(rebuilt.items) == 3
        assert len(_drop_warnings(caplog)) == 1

    def test_one_key_set_in_another_order_warns_once(self, caplog):
        class Many(StardagBaseModel):
            items: list[Legacy]

        first = {"scale": 1.0, "tau": 2.0, "x": 0, "y": 0}
        second = {"y": 0, "x": 0, "scale": 1.0, "tau": 2.0}
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            _compat(Many, {"items": [first, second]})
        assert len(_drop_warnings(caplog)) == 1

    def test_a_drop_inside_a_dataclass_field_warns(self, caplog):
        @dataclasses.dataclass(frozen=True)
        class Pair:
            left: Legacy

        class InDataclass(StardagBaseModel):
            pair: Pair

        stored = {"pair": {"left": {"scale": 1.0, "tau": 2.0, "removed": 3}}}
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            _compat(InDataclass, stored)
        assert len(_drop_warnings(caplog)) == 1

    def test_outside_compat_mode_nothing_is_dropped(self):
        with pytest.raises(ValidationError):
            New.model_validate({"removed": 1})
