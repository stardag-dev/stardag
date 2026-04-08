"""Test that stardag.integration.modal imports successfully.

Regression test for https://github.com/stardag-dev/stardag/issues/113
where `from modal.gpu import GPU_T` broke on modal >= 1.4 because the
`modal.gpu` module was removed.
"""

import pytest

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)


def test_modal_integration_import():
    """Importing stardag.integration.modal should not raise."""
    from stardag.integration.modal import FunctionSettings, StardagApp

    assert FunctionSettings is not None
    assert StardagApp is not None


def test_function_settings_gpu_accepts_string():
    """FunctionSettings.gpu should accept str values (modal 1.x API)."""
    import modal

    from stardag.integration.modal import FunctionSettings

    settings = FunctionSettings(
        image=modal.Image.debian_slim(),
        gpu="A10G",
    )
    assert settings.get("gpu") == "A10G"


def test_function_settings_gpu_accepts_list():
    """FunctionSettings.gpu should accept list[str] values."""
    import modal

    from stardag.integration.modal import FunctionSettings

    settings = FunctionSettings(
        image=modal.Image.debian_slim(),
        gpu=["A10G", "T4"],
    )
    assert settings.get("gpu") == ["A10G", "T4"]
