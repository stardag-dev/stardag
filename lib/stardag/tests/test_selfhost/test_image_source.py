"""Tests for prebuilt-image vs from-source resolution in the self-host CLI."""

import re
from pathlib import Path

import pytest

pytest.importorskip("modal")
pytest.importorskip("cryptography")

import typer  # noqa: E402

from stardag._cli.selfhost import _resolve_image_source  # noqa: E402
from stardag.selfhost._modal_app import (  # noqa: E402
    DEFAULT_SERVER_VERSION,
    SERVER_IMAGE_REPO,
    server_image_ref,
)

REPO_ROOT = Path(__file__).parents[4]


def test_server_image_ref():
    assert server_image_ref("1.2.3") == f"{SERVER_IMAGE_REPO}:1.2.3"
    assert server_image_ref("latest") == f"{SERVER_IMAGE_REPO}:latest"


def test_default_server_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", DEFAULT_SERVER_VERSION)


def test_resolve_default_is_prebuilt_at_default_version():
    repo_root, version = _resolve_image_source(None, False, None)
    assert repo_root is None
    assert version == DEFAULT_SERVER_VERSION


def test_resolve_explicit_server_version():
    repo_root, version = _resolve_image_source(None, False, "9.9.9")
    assert repo_root is None
    assert version == "9.9.9"

    repo_root, version = _resolve_image_source(None, False, "latest")
    assert repo_root is None
    assert version == "latest"


def test_resolve_from_source_requires_repo_checkout(tmp_path: Path):
    # tmp_path is not a stardag checkout
    with pytest.raises(typer.Exit):
        _resolve_image_source(tmp_path, True, None)


def test_resolve_from_source_with_repo():
    repo_root, version = _resolve_image_source(REPO_ROOT, True, None)
    assert repo_root == REPO_ROOT.resolve()
    assert version is None


def test_resolve_explicit_repo_implies_from_source():
    repo_root, version = _resolve_image_source(REPO_ROOT, False, None)
    assert repo_root == REPO_ROOT.resolve()
    assert version is None


def test_resolve_from_source_and_server_version_conflict():
    with pytest.raises(typer.Exit):
        _resolve_image_source(None, True, "1.2.3")
    with pytest.raises(typer.Exit):
        _resolve_image_source(REPO_ROOT, False, "1.2.3")


def test_build_server_app_prebuilt_needs_no_repo():
    """Prebuilt mode constructs the Modal app without any repo checkout.

    (Image definitions are lazy in Modal - nothing is pulled here.)
    """
    from stardag.selfhost._modal_app import build_server_app

    app, functions = build_server_app(server_version="1.2.3")
    assert set(functions) == {"web", "migrate"}


# ---------------------------------------------------------------------------
# Client-Python / image-Python matching (serialized=True functions are
# cloudpickled by the client and unpickled in the image: versions must match)
# ---------------------------------------------------------------------------


def test_client_python_version_matches_interpreter(monkeypatch: pytest.MonkeyPatch):
    import sys

    from stardag.selfhost._modal_app import client_python_version

    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    assert client_python_version() == "3.14"


def test_client_python_version_rejects_too_old(monkeypatch: pytest.MonkeyPatch):
    import sys

    from stardag.selfhost._modal_app import client_python_version

    monkeypatch.setattr(sys, "version_info", (3, 9, 0, "final", 0))
    with pytest.raises(RuntimeError, match="requires Python >= 3.10"):
        client_python_version()


def _record_add_python(monkeypatch: pytest.MonkeyPatch) -> dict:
    import modal

    seen: dict = {}
    real_from_registry = modal.Image.from_registry

    def recording_from_registry(ref, **kwargs):
        if "add_python" in kwargs:
            seen["add_python"] = kwargs["add_python"]
        return real_from_registry(ref, **kwargs)

    monkeypatch.setattr(modal.Image, "from_registry", recording_from_registry)
    return seen


def test_build_server_app_from_source_uses_client_python(
    monkeypatch: pytest.MonkeyPatch,
):
    """The from-source image's Python defaults to the running interpreter's."""
    import sys

    from stardag.selfhost._modal_app import build_server_app

    monkeypatch.setattr(sys, "version_info", (3, 13, 1, "final", 0))
    seen = _record_add_python(monkeypatch)
    build_server_app(repo_root=REPO_ROOT)
    assert seen["add_python"] == "3.13"


def test_build_server_app_from_source_python_override(
    monkeypatch: pytest.MonkeyPatch,
):
    from stardag.selfhost._modal_app import build_server_app

    seen = _record_add_python(monkeypatch)
    build_server_app(repo_root=REPO_ROOT, python_version="3.11")
    assert seen["add_python"] == "3.11"


def test_prebuilt_image_python_constant_matches_dockerfile():
    """PREBUILT_IMAGE_PYTHON must track app/server.Dockerfile's base image."""
    from stardag.selfhost._modal_app import PREBUILT_IMAGE_PYTHON

    dockerfile = (REPO_ROOT / "app" / "server.Dockerfile").read_text()
    expected = "FROM python:{}.{}-slim".format(*PREBUILT_IMAGE_PYTHON)
    assert expected in dockerfile


def test_check_prebuilt_python_matching(monkeypatch: pytest.MonkeyPatch):
    import sys

    from stardag._cli.selfhost import _check_prebuilt_python
    from stardag.selfhost._modal_app import PREBUILT_IMAGE_PYTHON

    monkeypatch.setattr(sys, "version_info", (*PREBUILT_IMAGE_PYTHON, 0, "final", 0))
    _check_prebuilt_python()  # no-op


def test_check_prebuilt_python_mismatch_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
):
    import sys

    from stardag._cli.selfhost import _check_prebuilt_python

    monkeypatch.setattr(sys, "version_info", (3, 14, 0, "final", 0))
    with pytest.raises(typer.Exit):
        _check_prebuilt_python()
