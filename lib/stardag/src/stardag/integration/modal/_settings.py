"""Modal function settings: the declaration and how it is merged at deploy.

:class:`FunctionSettings` is what a user writes when configuring an app's
builder/worker/tick functions. :func:`_prepare_function_settings` is what
:meth:`stardag.integration.modal.StardagApp.finalize` applies to it before
handing the result to Modal — as a :class:`PreparedFunction`, because a
declaration reaches Modal through two doors rather than one: see below.

**These settings are not a pass-through, and cannot be.** Modal renamed
three container-scaling parameters in February 2025 and moved input
concurrency out of ``function()`` into the ``@modal.concurrent`` decorator
in April 2025, and its client *raises* on the old spellings rather than
warning. A ``FunctionSettings`` carrying one of them is therefore a failed
deploy, not a deprecation notice. So this module owns the vocabulary:
current Modal names are what a user should write, the four legacy names are
accepted and translated (see :data:`_RENAMED_SETTINGS`), and the two
concurrency keys are lifted out of the ``function()`` kwargs and returned
beside them for the decorator.
"""

from __future__ import annotations

import logging
import pathlib
import typing

import modal

from stardag.exceptions import StardagError

logger = logging.getLogger(__name__)


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
        max_containers: Ceiling on how many containers this function may
            scale out to.
        min_containers: Containers kept warm regardless of load.
        buffer_containers: Extra idle containers kept ready above current
            demand, to absorb a burst without a cold start.
        scaledown_window: Seconds an idle container is kept before it is
            shut down.
        max_concurrent_inputs: How many inputs one container may serve at
            once. **Only safe on a function whose body is safe to run
            concurrently**, and what "concurrently" means depends on the
            function: Modal serves an ``async def`` as asyncio tasks on one
            event loop, and a ``def`` on threads, which is the harder
            contract. Stardag defaults this for the reactive ``tick``
            (async, and almost entirely I/O wait) and deliberately not for
            workers, which run user code — see ``StardagApp.finalize``.
        target_concurrent_inputs: The per-container concurrency Modal's
            autoscaler *aims* for, below ``max_concurrent_inputs``, which
            containers may burst past when demand outruns supply. Trades a
            little average latency for smaller tail latencies. Leaving it
            unset maximises packing, which is usually what you want for an
            I/O-bound function.
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
        concurrency_limit: Deprecated alias for ``max_containers``.
        keep_warm: Deprecated alias for ``min_containers``.
        container_idle_timeout: Deprecated alias for ``scaledown_window``.
        allow_concurrent_inputs: Deprecated alias for
            ``max_concurrent_inputs``.
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
    max_containers: int
    min_containers: int
    buffer_containers: int
    scaledown_window: int
    max_concurrent_inputs: int
    target_concurrent_inputs: int
    ephemeral_disk: int
    retries: int
    nonpreemptible: bool
    # Legacy Modal spellings, translated at deploy — see _RENAMED_SETTINGS.
    concurrency_limit: int
    keep_warm: int
    container_idle_timeout: int
    allow_concurrent_inputs: int


_RENAMED_SETTINGS: dict[str, str] = {
    # Renamed by Modal on 2025-02-24.
    "concurrency_limit": "max_containers",
    "keep_warm": "min_containers",
    "container_idle_timeout": "scaledown_window",
    # Moved out of `function()` into `@modal.concurrent` on 2025-04-09; the
    # stardag name keeps "max" in front for the same reason Modal's
    # `max_inputs=` does, and because `allow_…` reads like a boolean.
    "allow_concurrent_inputs": "max_concurrent_inputs",
}
"""Legacy Modal parameter names accepted in :class:`FunctionSettings`.

Translated rather than rejected, and translated silently enough to be
worth spelling out: every one of these was a **hard error** from Modal
≥1.0 — its client raises ``DeprecationError`` from ``function()`` instead
of warning — so an app that sets one does not deploy at all today. There
is no behaviour anyone can be relying on, which makes translation pure
gain and makes a one-line warning the right volume for it.
"""


# stardag's own names for the two `@modal.concurrent` arguments, and the
# Modal ones they map to. Kept apart from _RENAMED_SETTINGS because these
# are not renames: they are settings that stop being `function()` keywords
# and become a decorator, which is a different edit to make on the way out.
_CONCURRENCY_SETTINGS: dict[str, str] = {
    "max_concurrent_inputs": "max_inputs",
    "target_concurrent_inputs": "target_inputs",
}


InputConcurrency = dict[str, int]
"""Keyword arguments for ``modal.concurrent`` — ``max_inputs``/``target_inputs``."""


def _normalize_legacy_names(settings: typing.Mapping[str, typing.Any]) -> dict:
    """Rewrite any legacy Modal parameter names to their current spelling.

    Warns once per call site rather than raising: the alternative is the
    ``DeprecationError`` Modal itself throws, which is what this exists to
    prevent. An explicit current-name value wins over a legacy one — the
    two would otherwise resolve by dict order, which is not a rule anyone
    should have to know.
    """
    result = dict(settings)
    for legacy, current in _RENAMED_SETTINGS.items():
        if legacy not in result:
            continue
        value = result.pop(legacy)
        if current in result:
            logger.warning(
                "Modal function settings declare both %r and its current "
                "name %r; using %r=%r and ignoring %r.",
                legacy,
                current,
                current,
                result[current],
                legacy,
            )
            continue
        logger.warning(
            "Modal function setting %r has been renamed to %r; stardag is "
            "translating it, but please update the declaration.",
            legacy,
            current,
        )
        result[current] = value
    return result


def _input_concurrency(
    normalized: typing.Mapping[str, typing.Any],
) -> InputConcurrency | None:
    """The ``modal.concurrent`` arguments a normalized declaration carries.

    ``None`` when it carries none — which is not the same as
    ``max_inputs=1``. Modal's decorator changes how a container serves
    inputs at all, so a function that never asks for concurrency should not
    be wrapped in it.
    """
    concurrency: InputConcurrency = {
        modal_name: normalized[stardag_name]
        for stardag_name, modal_name in _CONCURRENCY_SETTINGS.items()
        if normalized.get(stardag_name) is not None
    }
    if not concurrency:
        return None
    _validate_input_concurrency(concurrency)
    return concurrency


def _validate_input_concurrency(concurrency: InputConcurrency) -> None:
    """Reject a concurrency declaration Modal will refuse, in stardag's words.

    Both of these are caught by Modal too — but at *registration*, and
    phrased in its own parameter names, which are not the ones the user
    wrote. ``max_concurrent_inputs=…`` producing a ``TypeError`` about a
    missing ``max_inputs`` is a bad way to learn this, so the settings
    module that owns the renaming owns the error message too.
    """
    if "max_inputs" not in concurrency:
        raise StardagError(
            "FunctionSettings sets target_concurrent_inputs="
            f"{concurrency['target_inputs']} without max_concurrent_inputs. "
            "A target is the concurrency Modal's autoscaler aims for "
            "*below* a ceiling, so it is meaningless on its own and Modal "
            "refuses the function. Set max_concurrent_inputs as well — and "
            "note that stardag's own default for the reactive tick is not "
            "merged in underneath, because a ceiling nobody asked for is "
            "not a safe thing to invent."
        )
    if (target := concurrency.get("target_inputs")) is not None:
        if target > concurrency["max_inputs"]:
            raise StardagError(
                f"FunctionSettings sets target_concurrent_inputs={target} "
                f"above max_concurrent_inputs={concurrency['max_inputs']}. "
                "The target is what the autoscaler aims for below the "
                "ceiling; it cannot exceed it."
            )


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


class PreparedFunction(typing.NamedTuple):
    """One ``FunctionSettings`` resolved into the two things Modal needs.

    Two, and not one, because Modal takes them through different doors:
    ``kwargs`` goes to ``modal.App.function()`` and ``concurrency`` — when
    the declaration asks for any — to ``@modal.concurrent``. Returning them
    together is what keeps a single declaration from being read twice and
    from being half-applied.

    Attributes:
        kwargs: Keyword arguments for ``modal.App.function()``.
        concurrency: Keyword arguments for ``modal.concurrent``, or None
            when this function declared no input concurrency.
    """

    kwargs: dict[str, typing.Any]
    concurrency: InputConcurrency | None


def _prepare_function_settings(
    settings: FunctionSettings,
    *,
    extra_secrets: list[modal.Secret],
    auto_volumes: dict[str, modal.Volume],
) -> PreparedFunction:
    """Resolve ``settings`` into what ``StardagApp.finalize`` hands Modal.

    Auto-mounted volumes are merged with user volumes, where user-specified
    volumes at the same mount path take precedence over auto-mounted ones.
    Secrets are de-duplicated by name (a named secret propagated from the
    builder plus one the function already declares should apply once).

    Legacy parameter names are translated to their current spelling here —
    once, which is why this is the only public way in: normalizing in two
    places would warn twice about the same declaration. The
    input-concurrency settings are then lifted out of the ``function()``
    kwargs, because they are not ``function()`` keywords at all.
    """
    normalized = _normalize_legacy_names(settings)
    concurrency = _input_concurrency(normalized)
    result: dict[str, typing.Any] = normalized
    for stardag_name in _CONCURRENCY_SETTINGS:
        result.pop(stardag_name, None)

    # Merge secrets: existing + extra, de-duplicated by name.
    existing_secrets: list[modal.Secret] = list(result.get("secrets") or [])
    result["secrets"] = _dedupe_secrets(existing_secrets + extra_secrets)

    # Merge volumes: auto-mounted (lower priority) + user (higher priority)
    user_volumes = dict(result.get("volumes") or {})
    result["volumes"] = {**auto_volumes, **user_volumes}

    return PreparedFunction(kwargs=result, concurrency=concurrency)
