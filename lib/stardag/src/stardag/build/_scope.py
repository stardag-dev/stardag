"""The structure scope: which code and config a build's dependency edges belong to.

A build's dependency edges are facts about the code that evaluated
``requires()`` and yielded dynamic dependencies, and about the
``dependencies_only`` config that code read. The registry keys every edge by
a **scope key** ``<code_id>:<config_hash>`` and evaluates a build's
readiness over its own scope only. This module computes both halves. See
``docs/design/scope-keyed-dependency-structure.md``.

**Code id.** The full git SHA of a clean working tree; a fresh UUID, with a
loud warning, for a dirty one — the code is unknown, so it shares nothing
and caches nothing. A deployment mints its code id once at ``finalize()``
and bakes it into every function as ``STARDAG_CODE_ID``, so every
container of that deployment answers the same; a local process computes it
at first use. ``STARDAG_CODE_ID`` set in the environment always wins, which
is also how a CI job with no git checkout names its code.

**Config hash.** :func:`stardag.build_config.structure_config_hash` — the
``dependencies_only`` overrides, validated through their fields and
serialised in hash mode, defaults dropped.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import uuid
from collections.abc import Mapping
from typing import TYPE_CHECKING
from typing import Any

from stardag.build_config import structure_config_hash

if TYPE_CHECKING:
    from uuid import UUID

logger = logging.getLogger(__name__)

STARDAG_CODE_ID_ENV = "STARDAG_CODE_ID"
"""Env var carrying a process's code id. Baked into a deployment at
``finalize()``; honoured everywhere before git is consulted."""

SYNTHETIC_SCOPE_PREFIX = "build:"
"""Prefix of the scope a build has until something sets a real one (the
server assigns ``build:<build id>``). Nobody else shares it."""

_SYNTHETIC_SCOPE_RE = re.compile(
    r"^build:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

_process_code_id: str | None = None


def _git(*args: str) -> str | None:
    try:
        return (
            subprocess.check_output(["git", *args], stderr=subprocess.DEVNULL)
            .strip()
            .decode("utf-8")
        )
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def code_id() -> str:
    """This process's code identity. Stable for the process; see the module
    docstring for where it comes from."""
    global _process_code_id
    env = os.environ.get(STARDAG_CODE_ID_ENV)
    if env is not None:
        if not env.strip() or ":" in env:
            raise ValueError(
                f"{STARDAG_CODE_ID_ENV}={env!r} is not a usable code id: it must "
                "be non-empty and must not contain ':', the scope key's "
                "separator between the code id and the config hash."
            )
        return env
    if _process_code_id is not None:
        return _process_code_id
    sha = _git("rev-parse", "HEAD")
    dirty = _git("status", "--porcelain")
    if sha and not dirty:
        _process_code_id = sha
    else:
        _process_code_id = uuid.uuid4().hex
        if sha:
            logger.warning(
                "The working tree has uncommitted changes, so this code has no "
                "identity: using a one-off code id %s. Builds started from it "
                "share dependency structure with nothing, and a deployment "
                "made from it is its own version. Commit to get a stable one.",
                _process_code_id,
            )
        else:
            logger.warning(
                "No git repository found and %s is not set: using a one-off "
                "code id %s. Set %s to give this code a stable identity.",
                STARDAG_CODE_ID_ENV,
                _process_code_id,
                STARDAG_CODE_ID_ENV,
            )
    return _process_code_id


def structure_scope_key(
    code: str, build_config: Mapping[str, Mapping[str, Any]] | None
) -> str:
    """``<code_id>:<config_hash>`` for this code and this build config."""
    return f"{code}:{structure_config_hash(build_config)}"


def scope_code_id(scope_key: str) -> str:
    """The code id half of a structure scope key.

    A tick or worker that has to decide whether it may act on a build
    compares this against its own :func:`code_id`, and nothing more. The
    config half is a function of the build's own config — the same config
    that container installs — so recomputing it there would verify nothing,
    and it would need every task class the config names to be importable in
    that container, which a worker rehydrating one task has no reason to
    guarantee. Two containers of one code id agree on the config hash by
    construction; two of different code ids are told apart by this half.
    """
    code, _, _ = scope_key.partition(":")
    return code


def scope_config_hash(scope_key: str) -> str:
    """The config-hash half of a structure scope key (empty for a synthetic
    scope, which has no config half).

    A worker registers the dynamic edges it yields under *its own* code id
    with the config half it was handed: the config is the build's, fixed for
    its life, and hashing it again would need every configured task class
    importable in a container that rehydrated one task. Two code versions
    with the same config half are still two scopes, told apart by the code
    half.
    """
    if is_synthetic_scope(scope_key):
        return ""
    _, sep, config = scope_key.partition(":")
    return config if sep else ""


def is_synthetic_scope(
    scope_key: str | None, *, build_id: "UUID | str | None" = None
) -> bool:
    """Whether ``scope_key`` is the server's per-build placeholder — a build
    that never set a real scope, which every current tick may drive.

    The server writes exactly ``build:<the build's own id>``. Given
    ``build_id``, only that string counts: a synthetic scope is the one case
    that skips the code-id check, and the registry accepts any claimed
    scope, so ``build:<some other uuid>`` set by a caller must read as a
    real (foreign) scope, not as the placeholder. Without a build id the
    exact shape is all there is to go on; a prefix test would also match a
    real scope whose code id happens to be ``build``.
    """
    if scope_key is None:
        return True
    if build_id is not None:
        return scope_key.lower() == f"{SYNTHETIC_SCOPE_PREFIX}{build_id}".lower()
    return _SYNTHETIC_SCOPE_RE.match(scope_key) is not None


def _reset_for_tests() -> None:
    global _process_code_id
    _process_code_id = None
