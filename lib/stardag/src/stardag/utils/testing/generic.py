from typing import Literal

from pydantic import TypeAdapter


def assert_serialize_validate_roundtrip(
    type_or_annotation,
    value,
    target_format: list[Literal["python", "json"]] = ["python", "json"],
    python_modes: list[Literal["python", "json"]] = ["python", "json"],
):
    type_adapter = TypeAdapter(type_or_annotation)
    for fmt in target_format:
        if fmt == "python":
            for mode in python_modes:
                dumped = type_adapter.dump_python(value, mode=mode)
                reconstructed = type_adapter.validate_python(dumped)
                assert value == reconstructed, (
                    f"Serialization/validation roundtrip failed for format '{fmt}', "
                    f"mode '{mode}'"
                )
        elif fmt == "json":
            dumped = type_adapter.dump_json(value)
            reconstructed = type_adapter.validate_json(dumped)
            assert (
                value == reconstructed
            ), f"Serialization/validation roundtrip failed for format '{fmt}'"
