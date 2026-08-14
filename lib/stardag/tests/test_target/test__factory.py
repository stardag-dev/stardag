"""Tests for the default URI-prefix → target mapping.

The theme here is blast radius. ``get_default_prefix_to_target_prototype``
is on the path of every target resolution in every stardag process, and it
imports optional third-party integrations to populate its mapping. An
integration that is broken in the installed environment must cost the user
that one prefix, not the process.
"""

import logging
from unittest import mock

from stardag.target._factory import get_default_prefix_to_target_prototype

_MODAL_TARGET_MODULE = "stardag.integration.modal._target"
_S3_MODULE = "stardag.integration.aws.s3"


def _import_raising(module_name: str, exc: BaseException):
    """Patch ``__import__`` so importing ``module_name`` raises ``exc``.

    Note this models the exception escaping *module execution*, which is
    what a broken integration actually does — the failing statement is at
    the top level of the imported module (or of a package ``__init__`` it
    pulls in), and whatever it raises propagates unchanged.

    A stub module in ``sys.modules`` does not model it: ``from x import y``
    turns a missing attribute into ``ImportError``, so the stub would
    always land in the "integration not installed" branch and the test
    would pass against the very bug it is meant to catch.
    """
    real_import = __import__

    def _import(name, *args, **kwargs):
        if name == module_name:
            raise exc
        return real_import(name, *args, **kwargs)

    return mock.patch("builtins.__import__", _import)


def test_local_prefix_is_always_present():
    assert "/" in get_default_prefix_to_target_prototype()


def test_broken_modal_integration_does_not_break_the_factory(caplog):
    """A broken Modal integration costs the `modalvol://` prefix, nothing else.

    Regression test for the incident where `stardag.integration.modal`
    raised `AttributeError` at import (reading the `modal.exception`
    submodule off a bare `import modal`). The guard here caught only
    `ImportError`, so the `AttributeError` propagated and killed the target
    factory — taking down services that had `modal` installed as a
    transitive dependency and never used a `modalvol://` root.
    """
    boom = AttributeError("module 'modal' has no attribute 'exception'")

    with _import_raising(_MODAL_TARGET_MODULE, boom):
        with caplog.at_level(logging.WARNING):
            mapping = get_default_prefix_to_target_prototype()

    assert "modalvol://" not in mapping
    # The prefixes that have nothing to do with Modal are unaffected.
    assert "/" in mapping
    # ...and the cause is reported rather than swallowed, so a user who
    # *did* want modalvol:// can tell a broken integration from an absent
    # one instead of only seeing "unsupported prefix" downstream.
    assert any("Modal integration" in record.message for record in caplog.records), (
        caplog.text
    )


def test_broken_s3_integration_does_not_break_the_factory(caplog):
    """Same contract for the S3 integration guard."""
    boom = RuntimeError("botocore exploded on import")

    with _import_raising(_S3_MODULE, boom):
        with caplog.at_level(logging.WARNING):
            mapping = get_default_prefix_to_target_prototype()

    assert "s3://" not in mapping
    assert "/" in mapping
    assert any("S3 integration" in record.message for record in caplog.records), (
        caplog.text
    )


def test_absent_integration_is_silent(caplog):
    """A *missing* optional dependency is the expected case — no warning.

    Distinguishing this from the broken case is the point of keeping the
    `ImportError` branch separate: warning on every plain install would
    train users to ignore the warning that matters.
    """
    absent = ImportError(f"No module named {_MODAL_TARGET_MODULE!r}")

    with _import_raising(_MODAL_TARGET_MODULE, absent):
        with caplog.at_level(logging.WARNING):
            mapping = get_default_prefix_to_target_prototype()

    assert "modalvol://" not in mapping
    assert not [r for r in caplog.records if "Modal integration" in r.message]
