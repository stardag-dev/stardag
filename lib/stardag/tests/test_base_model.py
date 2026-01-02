from typing import Annotated, Type

import pytest
from pydantic import ValidationError

from stardag.base_model import (
    CONTEXT_MODE_KEY,
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
