"""The local cache of instance bodies: ``~/.stardag/registry-cache/``.

An instance (a registry ``task_instance`` row) is immutable once
registered, so a copy read once can be served again by its id without
asking the registry -- which is what lets a pinned
``from_registry(task_id, instance_id=...)`` work offline after one online
read.

What is cached, and what is not:

- **Instance bodies only, keyed by instance id.** Nothing mutable: no
  status, no completion, no ``expanded_at`` (a cache hit reports it as
  ``None``), and never "the newest instance of a task id", which changes
  as new instances are registered.
- **One partition per registry identity**: the registry URL plus what
  decides which environment answers -- the API key (by fingerprint) with
  API-key auth, the workspace and environment otherwise. A profile never
  reads another profile's entries, so a body cached against one
  environment is never served for another.
- **Never trusted blindly.** An entry that does not parse, or names
  another instance, is discarded and re-fetched; a hit for a pinned task
  still goes through the recomputed task id check in ``from_registry``.

Writes are atomic (a temporary file in the same directory, then
``os.replace``), so a reader never sees a half-written entry. There is no
eviction; ``stardag cache clear`` empties one partition or all of them.
Turn the cache off with ``STARDAG_REGISTRY_CACHE=0``.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any
from uuid import UUID

from stardag.config import config_provider
from stardag.config.paths import get_registry_cache_dir, registry_key_from_url
from stardag.registry._models import TaskInstanceInfo

logger = logging.getLogger(__name__)

# Bumped if the entry layout changes; an entry of another version is a miss.
_ENTRY_VERSION = 1

# :func:`secret_fingerprint`'s KDF parameters. Changing either moves every
# API-key partition (a one-off cold cache, nothing worse).
_FINGERPRINT_SALT = b"stardag-registry-cache"
_FINGERPRINT_ITERATIONS = 100_000


def cache_root() -> Path:
    """The cache's root directory, holding one directory per partition."""
    return get_registry_cache_dir()


def cache_enabled() -> bool:
    """Whether the instance-body cache is on (``STARDAG_REGISTRY_CACHE``)."""
    return config_provider.get().registry_cache


def partition_key(api_url: str, *identity: str | None) -> str:
    """The directory name of one registry identity's partition.

    ``<host>-<digest>``: the host keeps a listing readable, the digest
    (over the URL and every ``identity`` part) keeps partitions apart.
    Nothing secret is recoverable from it; an API key is passed in as its
    own fingerprint, never raw.
    """
    url = api_url.rstrip("/")
    material = "\0".join([url, *(part or "" for part in identity)])
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{registry_key_from_url(url)}-{digest}"


@functools.lru_cache(maxsize=16)
def secret_fingerprint(secret: str) -> str:
    """A one-way fingerprint of a credential, for :func:`partition_key`.

    PBKDF2, not a bare hash: the input is a credential, and the digest's
    prefix ends up in a directory name on disk, so it is derived with a
    deliberately slow key-derivation function rather than a fast hash.
    Memoised per process, so the cost (tens of milliseconds) is paid once
    per credential, and only when the cache is used.
    """
    return hashlib.pbkdf2_hmac(
        "sha256", secret.encode("utf-8"), _FINGERPRINT_SALT, _FINGERPRINT_ITERATIONS
    ).hex()


def _partition_dir(partition: str) -> Path:
    if not partition or "/" in partition or "\\" in partition or ".." in partition:
        raise ValueError(f"Invalid registry cache partition: {partition!r}")
    return cache_root() / partition


def _entry_path(partition: str, instance_id: UUID | str) -> Path:
    # Normalised through UUID: the id becomes a file name, so nothing else
    # may reach the path.
    return _partition_dir(partition) / f"{UUID(str(instance_id))}.json"


def read(partition: str, instance_id: UUID | str) -> TaskInstanceInfo | None:
    """The cached instance, or ``None`` on a miss.

    An entry that cannot be read back as this instance (corrupt JSON,
    another layout version, a body naming another id, no task id) is
    deleted and reported as a miss, so the caller re-fetches it.
    """
    path = _entry_path(partition, instance_id)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as e:
        logger.debug("Registry cache entry %s unreadable: %s", path, e)
        return None
    try:
        payload: Any = json.loads(raw.decode("utf-8"))
        if payload.get("version") != _ENTRY_VERSION:
            raise ValueError(f"entry version {payload.get('version')!r}")
        info = TaskInstanceInfo.model_validate(payload["instance"])
        if info.id != UUID(str(instance_id)) or not info.task_id:
            raise ValueError("entry does not describe the requested instance")
    except Exception as e:
        logger.warning("Discarding corrupt registry cache entry %s: %s", path, e)
        _unlink(path)
        return None
    return info


def write(partition: str, info: TaskInstanceInfo) -> None:
    """Cache ``info`` under its id, atomically. Best effort: a failure to
    write is logged and otherwise ignored -- the cache is an optimisation,
    never a source of errors. An instance without a task id is not cached
    (a hit could not be checked against the task it is pinned for)."""
    if not info.task_id:
        return
    path = _entry_path(partition, info.id)
    # ``expanded_at`` is the one mutable column of an instance (set when it
    # is expanded), so it is not part of the cached copy.
    payload = {
        "version": _ENTRY_VERSION,
        "instance": info.model_dump(mode="json", exclude={"expanded_at"}),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, separators=(",", ":"))
            os.replace(tmp, path)
        except BaseException:
            _unlink(Path(tmp))
            raise
    except OSError as e:
        logger.debug("Could not write registry cache entry %s: %s", path, e)


def has_entry(partition: str, instance_id: UUID | str) -> bool:
    """Whether an entry exists (not whether it is readable). An
    inaccessible cache directory reads as no entry, like every other cache
    failure."""
    try:
        return _entry_path(partition, instance_id).is_file()
    except OSError:
        return False


def discard(partition: str, instance_id: UUID | str) -> bool:
    """Delete one entry; whether there was one."""
    return _unlink(_entry_path(partition, instance_id))


def clear(partition: str | None = None) -> int:
    """Delete one partition's entries, or every partition's (``None``).
    Returns the number of entries removed."""
    root = cache_root()
    targets = [_partition_dir(partition)] if partition is not None else [root]
    removed = 0
    for target in targets:
        if not target.exists():
            continue
        removed += sum(1 for p in target.rglob("*.json") if p.is_file())
        shutil.rmtree(target, ignore_errors=True)
    return removed


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as e:
        logger.debug("Could not delete registry cache entry %s: %s", path, e)
        return False
    return True


def registry_partition(registry: object) -> str | None:
    """``registry``'s partition when the cache is on and the registry has
    one (an HTTP registry with a known identity); ``None`` otherwise -- a
    test double, the no-op registry, or the cache turned off."""
    if not cache_enabled():
        return None
    get = getattr(registry, "instance_cache_partition", None)
    partition = get() if callable(get) else None
    return partition if isinstance(partition, str) and partition else None
