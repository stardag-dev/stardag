"""Modal function settings: the declaration and how it is merged at deploy.

:class:`FunctionSettings` is what a user writes when configuring an app's
builder/worker/tick functions. :func:`_prepare_function_settings` is what
:meth:`stardag.integration.modal.StardagApp.finalize` applies to it before
handing the result to ``modal.App.function()``.
"""

from __future__ import annotations

import pathlib
import typing

import modal


class FunctionSettings(typing.TypedDict, total=False):
    """Settings for Modal function configuration.

    These settings are passed to modal.App.function() when creating
    builder and worker functions.

    Attributes:
        image: Required. The Modal image to use for the function.
        gpu: GPU configuration (e.g., "A10G", "T4", or list for fallback).
        cpu: CPU cores (float or (min, max) tuple).
        memory: Memory in MB (int or (min, max) tuple).
        timeout: Function *execution* timeout in seconds. Note it does not
            bound how long the container lives: Modal signals the timeout
            and then allows roughly a further minute before the hard kill,
            so a task that catches the signal to checkpoint outlives its
            declared timeout by design. Also per *attempt* — with
            ``retries`` set, each retry gets a fresh window.
        startup_timeout: Seconds allowed for container startup (loading
            weights, importing a large dependency tree), separate from
            ``timeout``. Modal client >= 1.1.4; before that, and when
            unset, ``timeout`` covers both.
        volumes: Dict of mount path to Volume or CloudBucketMount.
        secrets: List of Modal secrets to inject.
        concurrency_limit: Max number of concurrent containers.
        allow_concurrent_inputs: Max concurrent inputs per container.
        container_idle_timeout: Seconds before idle container shuts down.
        keep_warm: Number of containers to keep warm.
        ephemeral_disk: Ephemeral disk size in MB.
        retries: Number of retries on failure. Covers exceptions raised
            *inside* the container and function timeouts; it is not what
            recovers a preempted or crashed container (Modal restarts
            those on the same input regardless). See
            :class:`~stardag.exceptions.ResumableInterruption` and
            ``TickConfig.max_attempts`` for the other two layers, and note
            they multiply: ``retries=3`` under a task allowed 20
            resumptions is up to 80 container attempts.
        nonpreemptible: Run on an instance that will not be reclaimed. The
            direct answer to "this task must not be preempted", for work
            that genuinely cannot checkpoint. Modal client >= 1.2.3;
            carries a 3x price multiplier on CPU and memory, and is not
            supported for GPU functions.
    """

    image: typing.Required[modal.Image]
    gpu: str | list[str]
    cpu: float | tuple[float, float]
    memory: int | tuple[int, int]
    timeout: int
    startup_timeout: int
    volumes: dict[
        typing.Union[str, pathlib.PurePosixPath],
        typing.Union[modal.Volume, modal.CloudBucketMount],
    ]
    secrets: list[modal.Secret]
    concurrency_limit: int
    allow_concurrent_inputs: int
    container_idle_timeout: int
    keep_warm: int
    ephemeral_disk: int
    retries: int
    nonpreemptible: bool


def _dedupe_secrets(secrets: list[modal.Secret]) -> list[modal.Secret]:
    """De-duplicate Modal secrets by name, preserving order.

    Named secrets (``Secret.from_name``) dedupe by name so a secret
    propagated from the builder to a worker that also declares it is applied
    once. Secrets without a usable name (e.g. ``Secret.from_dict``) fall back
    to object identity.
    """
    seen: set[str | int] = set()
    result: list[modal.Secret] = []
    for secret in secrets:
        key: str | int = getattr(secret, "name", None) or id(secret)
        if key in seen:
            continue
        seen.add(key)
        result.append(secret)
    return result


def _prepare_function_settings(
    settings: FunctionSettings,
    *,
    extra_secrets: list[modal.Secret],
    auto_volumes: dict[str, modal.Volume],
) -> dict[str, typing.Any]:
    """Merge extra secrets and auto-volumes into function settings.

    Auto-mounted volumes are merged with user volumes, where user-specified
    volumes at the same mount path take precedence over auto-mounted ones.
    Secrets are de-duplicated by name (a named secret propagated from the
    builder plus one the function already declares should apply once).
    """
    result: dict[str, typing.Any] = dict(settings)

    # Merge secrets: existing + extra, de-duplicated by name.
    existing_secrets: list[modal.Secret] = list(result.get("secrets") or [])
    result["secrets"] = _dedupe_secrets(existing_secrets + extra_secrets)

    # Merge volumes: auto-mounted (lower priority) + user (higher priority)
    user_volumes = dict(result.get("volumes") or {})
    result["volumes"] = {**auto_volumes, **user_volumes}

    return result
