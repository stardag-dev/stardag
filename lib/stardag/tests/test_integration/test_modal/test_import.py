"""Test that stardag.integration.modal imports successfully.

Regression test for https://github.com/stardag-dev/stardag/issues/113
where `from modal.gpu import GPU_T` broke on modal >= 1.4 because the
`modal.gpu` module was removed.

Also covers the sibling failure mode: reaching a *submodule* off a bare
``import modal``. See
:func:`test_import_survives_unbound_modal_exception_submodule`.
"""

import subprocess
import sys
import textwrap

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


# Runs in a subprocess because the setup is a mutation of the interpreter's
# `modal` package object that must precede the very first import of
# `stardag.integration.modal` — by the time this test module is collected,
# the pytest process has long since imported both.
_UNBOUND_EXCEPTION_SUBMODULE = textwrap.dedent(
    """
    import modal

    # `exception` is not in modal's __all__, and modal's package-level
    # __getattr__ raises AttributeError for anything it does not export.
    # So `modal.exception` is bound only when something else in the process
    # already imported the submodule — an accident of the import graph, not
    # a guarantee. Recreate the case where nothing did.
    try:
        del modal.exception
    except AttributeError:
        pass
    assert not hasattr(modal, "exception"), "test setup failed to unbind it"

    import stardag.integration.modal  # noqa: F401
    from stardag.integration.modal import MODAL_INTERRUPTIONS

    # Importing must not merely survive: the interruption tuple has to stay
    # populated. Silently degrading to KeyboardInterrupt-only would mean
    # function timeouts stop being recognised as resumable.
    assert KeyboardInterrupt in MODAL_INTERRUPTIONS, MODAL_INTERRUPTIONS
    assert len(MODAL_INTERRUPTIONS) == 2, MODAL_INTERRUPTIONS

    print("ok")
    """
)


def test_import_survives_unbound_modal_exception_submodule():
    """Importing the integration must not depend on `modal.exception` being bound.

    Regression test. `_runner.py` read `modal.exception.InputCancellation`
    with only a bare `import modal` in scope, at module level. That works
    exactly when some other import in the process has already pulled in the
    `modal.exception` submodule, which binds it as an attribute on the
    parent package — and blows up with

        AttributeError: module 'modal' has no attribute 'exception'

    when nothing has. Because `get_default_prefix_to_target_prototype()`
    imports the Modal integration for the `modalvol://` prefix, the failure
    landed on *any* stardag user with `modal` merely present in the
    environment, on the ordinary target-factory path, whether or not they
    used Modal for anything.

    The ordinary tests in this file cannot catch it: the pytest process has
    other modal-importing modules loaded, so the attribute is always bound
    by the time they run. Hence the subprocess and the explicit unbind.
    """
    result = subprocess.run(
        [sys.executable, "-c", _UNBOUND_EXCEPTION_SUBMODULE],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"import failed with modal.exception unbound:\n{result.stderr}"
    )
    assert "ok" in result.stdout
