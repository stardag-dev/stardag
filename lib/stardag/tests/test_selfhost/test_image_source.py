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
