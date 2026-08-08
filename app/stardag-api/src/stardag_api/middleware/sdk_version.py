"""ASGI middleware that reads, records and (optionally) gates on SDK version.

A middleware rather than a FastAPI dependency, for two reasons. It applies
to the whole surface without anyone remembering to opt a new route in — the
SDK surface is spread across five routers, and a compatibility floor that
half the endpoints honour is worse than none. And it sits *outside* the
dependency graph, so it is unaffected by auth being overridden or failing:
a client too old to speak the protocol should be told that, not handed an
auth error about a request it could never have made anyway.

See :mod:`stardag_api.sdk_compat` for the policy this enforces — including
why "no floor configured" is the default and the state this ships in.
"""

from __future__ import annotations

import json

from stardag_api.config import sdk_compat_settings
from stardag_api.sdk_compat import (
    SDK_VERSION_HEADER,
    check_minimum_sdk_version,
    parse_sdk_version,
    record_sdk_version,
)

_HEADER_BYTES = SDK_VERSION_HEADER.lower().encode()

# Paths that are never gated, whatever the floor. ``/api/v1/version``
# carries the floor itself, so blocking it would make the rejection
# undiagnosable — a client told "upgrade" could not ask *to what*. The rest
# are unauthenticated infrastructure endpoints that predate any notion of an
# SDK and have no business failing on one.
_EXEMPT_PATHS = frozenset({"/health", "/api/v1/version"})
_EXEMPT_PREFIXES = ("/.well-known/",)


class SdkVersionMiddleware:
    """Record the calling SDK's version; refuse it only if below the floor."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw: str | None = None
        for name, value in scope["headers"]:
            if name.lower() == _HEADER_BYTES:
                raw = value.decode("latin-1")
                break

        version = parse_sdk_version(raw)
        record_sdk_version(raw, version)

        # Expose it on the request for anything downstream that wants to
        # attribute behaviour to a client version (``request.state``).
        # Starlette reads ``scope["state"]``; seed it the same way it does.
        state = scope.setdefault("state", {})
        state["sdk_version_raw"] = raw
        state["sdk_version"] = str(version) if version is not None else None

        path = scope.get("path", "")
        if not (path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES)):
            rejection = check_minimum_sdk_version(version, sdk_compat_settings)
            if rejection is not None:
                body = json.dumps(
                    {"detail": rejection.model_dump()},
                    separators=(",", ":"),
                ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        # 426 Upgrade Required: the one status that means
                        # "your client, not your request". RFC 9110 wants an
                        # Upgrade header with it, which is about protocol
                        # switching and does not apply here; the body carries
                        # the actionable part.
                        "status": 426,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode()),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return

        await self.app(scope, receive, send)
