"""SDK↔API compatibility: who is calling, and what we still support.

The hosted service always runs the latest API, so the compatibility case
that actually occurs is an **old SDK against a new API**. (The reverse — a
newer SDK against an older self-hosted API — is not a supported
configuration: self-hosters upgrade both together, and nothing here tries
to make it work.) For the server to say anything useful about that case it
first has to *know* which SDK is calling, which until now it did not: the
SDK sent no version identity at all.

This module is the whole of the contract:

1. **Identity.** The SDK sends its version in a dedicated, machine-readable
   header, :data:`SDK_VERSION_HEADER`. It also sends a descriptive
   ``User-Agent`` for logs — the server deliberately never parses that to
   make a policy decision.
2. **Observation.** Every request's SDK version is parsed and recorded, so
   "which SDK versions are actually calling us?" is a question the logs can
   answer. That is what makes a future minimum-version decision *informed*
   rather than a guess, and it is the main thing this module is for today.
3. **A dormant floor.** ``minimum_version`` is unset by default, which
   means nothing is ever rejected. When an API change genuinely drops
   support for older SDKs, setting it makes the server refuse them with
   ``426 Upgrade Required`` and a message that names both versions and the
   upgrade command.

**Raising the floor is a product decision, not an implementation detail.**
It breaks working deployments on purpose, so it carries an obligation: an
API change that raises ``minimum_version`` must say so in ``CHANGELOG.md``,
in ``RELEASE_NOTES.md``, **and** in the error the server returns (that last
one is this module's job, and it is why the message is written the way it
is). See "Releasing the Server" in ``DEV_README.md``.

Nothing here is a security control. The header is self-reported and
trivially forged; a client determined to lie about its version can, and the
consequence is that it gets confusing errors from an API it cannot speak
to. The floor exists to turn *that* confusion into one clear sentence, not
to keep anyone out.
"""

from __future__ import annotations

import logging

from packaging.version import InvalidVersion, Version
from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

# The header the SDK reports its own version in. Dedicated and
# machine-readable on purpose: a policy decision must never depend on
# pattern-matching a ``User-Agent`` string, which is free-form, and which
# proxies and instrumentation libraries rewrite without telling anyone.
# ``X-`` prefixed to match the codebase's existing custom header
# (``X-API-Key``). Changing this name breaks every released SDK's ability
# to identify itself — treat it as a wire constant.
SDK_VERSION_HEADER = "X-Stardag-SDK-Version"

# Sent back on a 426 so a client can distinguish "upgrade your SDK" from
# every other error without parsing prose.
SDK_VERSION_ERROR_CODE = "SDK_VERSION_UNSUPPORTED"


class SdkCompatSettings(BaseSettings):
    """Which SDK versions this server accepts.

    ``minimum_version`` is **unset by default, and unset means no request is
    ever rejected** — which is the state the API ships in, because it is
    currently wire-compatible with every SDK that has been released (the
    changes since have been additive fields, parameters and endpoints; no
    response has changed shape or meaning). A floor with nothing below it is
    machinery in search of a purpose, so we don't invent one.

    Set it (``STARDAG_API_SDK_MINIMUM_VERSION``) only when an API change
    genuinely leaves older SDKs unable to function, and only together with
    the changelog and release-notes entries that explain why.
    """

    model_config = SettingsConfigDict(env_prefix="STARDAG_API_SDK_")

    minimum_version: str | None = None

    @field_validator("minimum_version")
    @classmethod
    def _validate_minimum_version(cls, value: str | None) -> str | None:
        """Reject a misconfigured floor at startup rather than at request time.

        A typo'd version string that we merely logged-and-ignored would
        leave the operator believing a minimum is enforced when it silently
        is not — the failure mode is invisible, so it has to be loud.
        """
        if value is None:
            return None
        value = value.strip()
        if not value:
            return None
        try:
            Version(value)
        except InvalidVersion as e:
            raise ValueError(
                f"{cls.__name__}.minimum_version must be a PEP 440 version "
                f"string, got {value!r}"
            ) from e
        return value


class SdkVersionRejection(BaseModel):
    """Body of a 426, and the user's entire experience of this feature."""

    error_code: str = SDK_VERSION_ERROR_CODE
    message: str
    sdk_version: str
    minimum_sdk_version: str


def parse_sdk_version(raw: str | None) -> Version | None:
    """Parse a reported SDK version, or ``None`` if there isn't a usable one.

    Absent, empty and unparseable all collapse to ``None`` — "unknown" —
    and unknown is always allowed. That is not laziness: every SDK released
    before the header existed sends nothing, so rejecting on absence would
    break every existing client the moment this deploys. A malformed value
    is treated identically because the alternative — a 400, or worse a 500
    out of a version parser — would make an unrelated client bug look like
    an outage.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    try:
        return Version(raw)
    except InvalidVersion:
        return None


def _effective_version(version: Version) -> Version:
    """The release a build belongs to, ignoring pre-release/dev/local parts.

    ``0.18.0.dev1``, ``0.18.0rc1``, ``0.18.0+g1a2b3c`` and ``0.18.0.post1``
    all count as ``0.18.0``. Under strict PEP 440 ordering the first two sort
    *below* ``0.18.0`` and would be refused against a ``0.18.0`` floor —
    which would mean a developer running a local build of the very version we
    require gets told to upgrade to it. That is an infuriating error to
    receive and a normal thing to be doing, so we don't produce it.

    The trade-off is deliberate and mild: an ``rc`` of X is trusted to
    behave like X. Anyone running pre-releases against the hosted API has
    already opted into tracking it closely.
    """
    return Version(version.base_version)


def check_minimum_sdk_version(
    version: Version | None,
    settings: SdkCompatSettings,
) -> SdkVersionRejection | None:
    """Return a rejection if this SDK version is below the floor, else ``None``.

    Returns ``None`` when no floor is configured (the default) and when the
    version is unknown, so the only way to get a rejection is to explicitly
    report a version that is explicitly too old.
    """
    minimum = settings.minimum_version
    if minimum is None or version is None:
        return None

    if _effective_version(version) >= Version(minimum):
        return None

    return SdkVersionRejection(
        message=(
            f"This Stardag server requires stardag SDK {minimum} or newer, "
            f"but this request came from stardag {version}. Upgrade with: "
            f'pip install --upgrade "stardag>={minimum}"  '
            f"(the server's current requirement is always readable as "
            f"minimum_sdk_version at GET /api/v1/version)."
        ),
        sdk_version=str(version),
        minimum_sdk_version=minimum,
    )


# Distinct SDK versions this process has already logged. The point of
# recording versions is the *set* of them ("who is still on 0.14?"), not the
# per-request count, and one line per request would drown the signal in its
# own noise — so we log each version once per process and leave rates to
# whatever aggregates the access log. Capped because the value is
# client-supplied: a client sending a random version per request must not be
# able to grow this without bound.
_seen_sdk_versions: set[str] = set()
_MAX_TRACKED_SDK_VERSIONS = 200
_MALFORMED = "<malformed>"


def record_sdk_version(raw: str | None, version: Version | None) -> None:
    """Log the first request seen from each distinct SDK version."""
    if raw is None:
        return
    label = str(version) if version is not None else _MALFORMED
    if label in _seen_sdk_versions:
        return
    if len(_seen_sdk_versions) >= _MAX_TRACKED_SDK_VERSIONS:
        return
    _seen_sdk_versions.add(label)
    if version is None:
        logger.info(
            "Unparseable %s header: %r (treated as unknown, request allowed)",
            SDK_VERSION_HEADER,
            raw[:64],
        )
    else:
        logger.info("First request in this process from stardag SDK %s", label)


def clear_seen_sdk_versions() -> None:
    """Reset the first-seen log dedupe (tests)."""
    _seen_sdk_versions.clear()
