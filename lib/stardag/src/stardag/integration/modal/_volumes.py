"""Modal volumes derived from the configured target roots.

A ``modalvol://`` target root names a Modal Volume. This module resolves
those names to ``modal.Volume`` objects so
:meth:`stardag.integration.modal.StardagApp.finalize` can auto-mount them
into every deployed function (which is what makes target I/O inside a
container a filesystem call rather than a Volume API call).
"""

from __future__ import annotations

import typing

import modal

from stardag.config import config_provider
from stardag.integration.modal._target import (
    MODAL_VOLUME_URI_PREFIX,
    get_volume_name_and_path,
)


class TargetRootsVolumes(typing.NamedTuple):
    """Result of get_target_roots_volumes().

    Attributes:
        by_root_key: Dict of target root key to Modal Volume instance.
        by_volume_name: Dict of volume name to Modal Volume instance (deduped).
    """

    by_root_key: dict[str, modal.Volume]
    by_volume_name: dict[str, modal.Volume]


def get_target_roots_volumes(
    target_roots: dict[str, str] | None = None,
    create_if_missing: bool = True,
) -> TargetRootsVolumes:
    """Get Modal volumes for configured target roots.

    Scans target roots for ``modalvol://`` URIs and returns the corresponding
    Modal Volume objects, both keyed by target root name and deduped by
    volume name.

    Args:
        target_roots: Dict of target root key to URI or None (default from config).
        create_if_missing: Whether to create the Modal volume if it doesn't exist.
            When True, volumes are eagerly hydrated to trigger creation.
            When False, volumes are lazy references (hydrated by Modal at deploy time).

    Returns:
        TargetRootsVolumes with volumes keyed by root key and by volume name.
    """
    if target_roots is None:
        config = config_provider.get()
        target_roots = config.target.roots

    by_root_key: dict[str, modal.Volume] = {}
    by_volume_name: dict[str, modal.Volume] = {}
    for key, uri in target_roots.items():
        if not uri.startswith(MODAL_VOLUME_URI_PREFIX):
            continue
        volume_name, _ = get_volume_name_and_path(uri)
        if volume_name not in by_volume_name:
            vol = modal.Volume.from_name(
                volume_name, create_if_missing=create_if_missing
            )
            if create_if_missing:
                vol.hydrate()
            by_volume_name[volume_name] = vol
        by_root_key[key] = by_volume_name[volume_name]

    return TargetRootsVolumes(by_root_key=by_root_key, by_volume_name=by_volume_name)
