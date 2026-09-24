"""Acting on the world the registry follows: deleting a task's target.

The registry cannot withdraw a completion by fiat (design.md, "Invalidation:
the registry follows the world"); a re-run is asked for by removing the
target, after which a build's discovery observes it missing. So a scenario
that exercises invalidation has to reach the target root itself -- the
Modal Volume the scenario apps write to -- and it does so the way an
operator would, through Modal, in the run's own environment.
"""

from __future__ import annotations

from stardag.integration.modal._target import (
    MODAL_VOLUME_URI_PREFIX,
    get_volume_name_and_path,
)


def delete_target(output_uri: str, *, modal_environment: str) -> None:
    """Remove the file behind ``output_uri`` from its Modal Volume.

    ``environment_name`` is explicit for the reason ``deployed_function``
    gives: a volume name is only unique within an environment, and this
    tier's safety rests on touching nothing outside its own.
    """
    import modal

    assert output_uri.startswith(MODAL_VOLUME_URI_PREFIX), (
        f"Not a Modal Volume target: {output_uri}"
    )
    volume_name, path = get_volume_name_and_path(output_uri)
    volume = modal.Volume.from_name(volume_name, environment_name=modal_environment)
    volume.remove_file(path)
