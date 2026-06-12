"""Tests for the `stardag modal` CLI.

Regression test for https://github.com/stardag-dev/stardag/issues/148
where `from modal.environments import ensure_env` broke on modal >= 1.4.3
because `ensure_env` moved to the private `modal._environments` module.
"""

import pytest
from typer.testing import CliRunner

try:
    import modal  # noqa: F401
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from stardag._cli.modal import app

runner = CliRunner()


def test_deploy_reaches_module_import():
    """`deploy` should get past its modal imports and env resolution.

    With a nonexistent script path, the command must fail with the
    "Error importing module" message — not an ImportError from the
    modal imports that precede it (issue #148).
    """
    result = runner.invoke(app, ["deploy", "nonexistent_script_xyz.py"])

    assert result.exception is None or isinstance(result.exception, SystemExit), (
        f"deploy raised unexpectedly: {result.exception!r}"
    )
    assert result.exit_code == 1
    assert "Error importing module" in result.output
