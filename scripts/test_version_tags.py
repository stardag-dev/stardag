"""Each package is versioned from its own tag family, never the other's.

The repo carries two tag families, routinely on the same commit: `vX.Y.Z` for
the SDK (`lib/stardag`, via hatch-vcs) and `server-vX.Y.Z` for the server
image (`scripts/server-version.sh`). An unfiltered `git describe --tags`
considers both, so for a while every non-release SDK build was versioned from
the server's tags (`0.7.1.devN` while the SDK was at 0.28.x), and a build
exactly at a release commit could report the server's `X.Y.Z` as the SDK's.

Nothing about a wrong version fails loudly, so this exercises the real config
rather than a copy of it: it copies `lib/stardag/pyproject.toml` and
`scripts/server-version.sh` into a throwaway repo laid out like this one, tags
it the way releases are tagged, and asks hatchling and the script for their
versions at each commit.

Run: `python -m pytest scripts/test_version_tags.py --noconftest`
(needs `hatchling` and `hatch-vcs`; the pre-commit hook installs them.)
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SDK_PYPROJECT = Path("lib/stardag/pyproject.toml")
SERVER_VERSION_SCRIPT = Path("scripts/server-version.sh")

# (commit label, tags on it). Mirrors how releases are actually cut: the SDK
# and server tags on one commit, plus a server-only release and an SDK release
# candidate, so the nearest tag of *either* family is sometimes the wrong one.
HISTORY: list[tuple[str, list[str]]] = [
    ("c1", ["v0.28.0", "server-v0.7.0"]),
    ("c2", []),
    ("c3", ["v0.29.0", "server-v0.8.0"]),
    ("c4", []),
    ("c5", ["server-v0.9.0"]),
    ("c6", []),
    ("c7", ["v0.30.0rc1"]),
    ("c8", []),
]

# commit label -> (SDK version pattern, server version pattern)
EXPECTED: dict[str, tuple[str, str]] = {
    "c1": (r"0\.28\.0", r"0\.7\.0"),
    "c2": (r"0\.28\.1\.dev1\+g[0-9a-f]+", r"0\.7\.0\+1\.g[0-9a-f]+"),
    # Both families on one commit: each must report exactly its own release.
    "c3": (r"0\.29\.0", r"0\.8\.0"),
    "c4": (r"0\.29\.1\.dev1\+g[0-9a-f]+", r"0\.8\.0\+1\.g[0-9a-f]+"),
    # A server-only release is the nearest tag; the SDK must look past it.
    "c5": (r"0\.29\.1\.dev2\+g[0-9a-f]+", r"0\.9\.0"),
    "c6": (r"0\.29\.1\.dev3\+g[0-9a-f]+", r"0\.9\.0\+1\.g[0-9a-f]+"),
    # An SDK release candidate is an SDK tag like any other.
    "c7": (r"0\.30\.0rc1", r"0\.9\.0\+2\.g[0-9a-f]+"),
    "c8": (r"0\.30\.0rc2\.dev1\+g[0-9a-f]+", r"0\.9\.0\+3\.g[0-9a-f]+"),
}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture(scope="module")
def tagged_repo(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    with pytest.MonkeyPatch.context() as mp:
        # Drop every inherited GIT_* variable first. Run as a git hook (this
        # is a pre-commit hook), git exports GIT_DIR and GIT_INDEX_FILE, and
        # every git command below would then act on *this* repository rather
        # than the throwaway one -- `git init` rewriting its config, `git add`
        # replacing its index.
        for var in list(os.environ):
            if var.startswith("GIT_"):
                mp.delenv(var)
        # Isolate from the developer's git config (signing, hooks, templates).
        mp.setenv("HOME", str(tmp_path_factory.mktemp("home")))
        mp.setenv("GIT_CONFIG_NOSYSTEM", "1")
        mp.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        for who in ("AUTHOR", "COMMITTER"):
            mp.setenv(f"GIT_{who}_NAME", "t")
            mp.setenv(f"GIT_{who}_EMAIL", "t@example.com")
        # A pretend version would bypass git entirely and mask the config.
        for var in list(os.environ):
            if var.startswith("SETUPTOOLS_SCM_PRETEND_VERSION"):
                mp.delenv(var)
        yield _build_repo(tmp_path_factory)


def _build_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    repo = tmp_path_factory.mktemp("repo")
    _git(repo, "init", "-q", "-b", "main")
    # Belt and braces for the above: never write to a repo that is not ours.
    toplevel = Path(_git(repo, "rev-parse", "--show-toplevel")).resolve()
    assert toplevel == repo.resolve(), toplevel
    for rel in (SDK_PYPROJECT, SERVER_VERSION_SCRIPT):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, repo / rel)
    (repo / "lib/stardag/src/stardag").mkdir(parents=True)
    (repo / "lib/stardag/src/stardag/__init__.py").write_text("")
    for label, tags in HISTORY:
        (repo / "history.txt").write_text(label + "\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", label)
        _git(repo, "branch", "-f", label)
        for tag in tags:
            _git(repo, "tag", tag)
    return repo


def _checkout(repo: Path, label: str) -> None:
    _git(repo, "checkout", "-q", "--detach", label)


def sdk_version(repo: Path) -> str:
    """The SDK's version as hatchling computes it for a build."""
    return subprocess.run(
        [sys.executable, "-m", "hatchling", "version"],
        cwd=repo / "lib/stardag",
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def server_version(repo: Path) -> str:
    return subprocess.run(
        ["bash", str(repo / SERVER_VERSION_SCRIPT)],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_the_fixture_reproduces_the_hazard(tagged_repo: Path) -> None:
    """An unfiltered describe picks the server tag here — else the tests
    below would pass without the filter."""
    _checkout(tagged_repo, "c6")
    assert _git(tagged_repo, "describe", "--tags").startswith("server-v0.9.0-")


@pytest.mark.parametrize("label", list(EXPECTED))
def test_sdk_is_versioned_from_sdk_tags_only(tagged_repo: Path, label: str) -> None:
    _checkout(tagged_repo, label)
    version = sdk_version(tagged_repo)
    assert re.fullmatch(EXPECTED[label][0], version), version


@pytest.mark.parametrize("label", list(EXPECTED))
def test_server_is_versioned_from_server_tags_only(
    tagged_repo: Path, label: str
) -> None:
    _checkout(tagged_repo, label)
    version = server_version(tagged_repo)
    assert re.fullmatch(EXPECTED[label][1], version), version


def test_a_release_checkout_carrying_only_its_own_tag(
    tagged_repo: Path, tmp_path: Path
) -> None:
    """The publish workflow's checkout: just the pushed tag, shallow.

    actions/checkout fetches `--no-tags`, so the server tag on the same commit
    is absent there. That, not the version config, is why published releases
    were right all along; this pins that the config keeps them right.
    """
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", str(clone))
    _git(
        clone,
        "fetch",
        "-q",
        "--no-tags",
        "--depth=1",
        str(tagged_repo),
        "+refs/tags/v0.29.0:refs/tags/v0.29.0",
    )
    _git(clone, "checkout", "-q", "--detach", "v0.29.0")
    assert sdk_version(clone) == "0.29.0"
