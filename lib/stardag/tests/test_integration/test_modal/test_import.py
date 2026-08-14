"""Test that stardag.integration.modal imports successfully.

Regression test for https://github.com/stardag-dev/stardag/issues/113
where `from modal.gpu import GPU_T` broke on modal >= 1.4 because the
`modal.gpu` module was removed.

Also covers the sibling failure mode: a *consumer* package named ``modal``
shadowing the distribution on ``sys.path``. See
:func:`test_a_shadowed_modal_package_does_not_break_the_target_factory`.
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


# A first-party package named `modal` on `sys.path` ahead of site-packages
# shadows the distribution for the whole process. This is not exotic: running
# an entrypoint as a script path (`python pkg/service/main.py` rather than
# `python -m ...`) puts that script's own directory on `sys.path[0]`, so any
# `modal/` subpackage sitting next to it wins. `import modal` then succeeds
# and returns the wrong thing — which is why an `except ImportError` guard
# around the integration never fired.
_SHADOWED_MODAL = textwrap.dedent(
    """
    import sys

    sys.path.insert(0, sys.argv[1])

    import modal

    assert "site-packages" not in (modal.__file__ or ""), modal.__file__
    assert not hasattr(modal, "exception")

    # 1. The integration must fail *loudly and correctly*: a shadowed modal
    #    genuinely means "the Modal integration is unavailable here", which
    #    is ImportError. Reaching `modal.exception` off the parent package
    #    instead produced `AttributeError: module 'modal' has no attribute
    #    'exception'` — a message identical to CPython's own, blamed on
    #    modal, and invisible to every `except ImportError` in the stack.
    try:
        import stardag.integration.modal  # noqa: F401
    except ImportError:
        pass
    except AttributeError as e:
        raise AssertionError(f"shadowed modal must raise ImportError, got {e!r}")
    else:
        raise AssertionError("expected the integration import to fail")

    # 2. And the rest of stardag must not care. This is the actual incident:
    #    a service that never used Modal could not resolve a local or S3
    #    target, because the target factory imports the Modal integration to
    #    register `modalvol://`.
    from stardag.target._factory import get_default_prefix_to_target_prototype

    prefixes = get_default_prefix_to_target_prototype()
    assert "/" in prefixes, prefixes
    assert "modalvol://" not in prefixes, prefixes

    print("ok")
    """
)


def test_a_shadowed_modal_package_does_not_break_the_target_factory(tmp_path):
    """A consumer package named `modal` must cost the Modal integration, not stardag.

    Regression test for a reported incident, reproduced here exactly: the
    affected service ran its entrypoint as a script path, which put the
    entrypoint's directory on `sys.path[0]`, and that directory contained a
    first-party `modal/` subpackage. Every `import modal` in that process —
    stardag's included — resolved to the empty local package.

    Two things then went wrong, and this test covers both. The integration
    read `modal.exception` off the parent package, so the failure presented
    as `AttributeError` rather than `ImportError`; and the target factory
    guarded its integration imports against `ImportError` only. The
    `AttributeError` went straight through and took down target resolution
    for every prefix, so a service that had never used Modal for anything
    could not build a local target.

    Runs in a subprocess: the shadowing has to be in place before the first
    `import modal` in the process, and pytest imported the real one long ago.
    """
    shadow = tmp_path / "modal"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("")

    result = subprocess.run(
        [sys.executable, "-c", _SHADOWED_MODAL, str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"shadowed-modal handling regressed:\n{result.stdout}\n{result.stderr}"
    )
    assert "ok" in result.stdout
