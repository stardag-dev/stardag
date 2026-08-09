"""Tests for the SDK version header: ingestion, reporting, and the dormant floor.

The compatibility guarantee these pin down, in one line: **a client that
sends nothing, or sends nonsense, is never refused** — and with no minimum
configured (the shipped default) nothing is refused at all.
"""

import logging

import pytest
from httpx import AsyncClient
from packaging.version import Version

from stardag_api.config import sdk_compat_settings
from stardag_api.sdk_compat import (
    SDK_VERSION_HEADER,
    SDK_VERSION_ERROR_CODE,
    SdkCompatSettings,
    check_minimum_sdk_version,
    parse_sdk_version,
)

# An SDK endpoint behind require_sdk_auth (mocked by the `client` fixture),
# used to prove the gate applies to the authenticated SDK surface.
SDK_ENDPOINT = "/api/v1/builds"


@pytest.fixture(autouse=True)
def _reset_seen_versions():
    """The first-seen log dedupe is process-global; isolate tests from each other."""
    from stardag_api.sdk_compat import clear_seen_sdk_versions

    clear_seen_sdk_versions()
    yield
    clear_seen_sdk_versions()


@pytest.fixture
def minimum_sdk_version(monkeypatch):
    """Configure a floor on the live settings object the middleware reads."""

    def _set(value: str | None) -> None:
        monkeypatch.setattr(sdk_compat_settings, "minimum_version", value)

    return _set


# ---------------------------------------------------------------------------
# Settings: unset by default, validated when set
# ---------------------------------------------------------------------------


class TestSdkCompatSettings:
    def test_minimum_version_unset_by_default(self, monkeypatch):
        """The shipped default enforces nothing."""
        monkeypatch.delenv("STARDAG_API_SDK_MINIMUM_VERSION", raising=False)
        assert SdkCompatSettings().minimum_version is None

    def test_minimum_version_from_env(self, monkeypatch):
        monkeypatch.setenv("STARDAG_API_SDK_MINIMUM_VERSION", "0.18.0")
        assert SdkCompatSettings().minimum_version == "0.18.0"

    def test_blank_minimum_version_is_unset(self, monkeypatch):
        monkeypatch.setenv("STARDAG_API_SDK_MINIMUM_VERSION", "  ")
        assert SdkCompatSettings().minimum_version is None

    def test_unparseable_minimum_version_fails_fast(self, monkeypatch):
        """A typo'd floor must fail loudly, not silently enforce nothing."""
        monkeypatch.setenv("STARDAG_API_SDK_MINIMUM_VERSION", "not-a-version")
        with pytest.raises(ValueError):
            SdkCompatSettings()


# ---------------------------------------------------------------------------
# Parsing and comparison
# ---------------------------------------------------------------------------


class TestParseSdkVersion:
    @pytest.mark.parametrize(
        "raw", [None, "", "   ", "banana", "0.18.0.0.0.x", "v=0.18"]
    )
    def test_unknown_values_parse_to_none(self, raw):
        assert parse_sdk_version(raw) is None

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("0.18.0", "0.18.0"),
            ("  0.18.0  ", "0.18.0"),
            ("0.18.0.dev1", "0.18.0.dev1"),
            ("0.18.0rc1", "0.18.0rc1"),
            ("0.18.0+g1a2b3c", "0.18.0+g1a2b3c"),
        ],
    )
    def test_pep440_values_parse(self, raw, expected):
        assert parse_sdk_version(raw) == Version(expected)


class TestCheckMinimumSdkVersion:
    def test_no_floor_configured_allows_everything(self):
        settings = SdkCompatSettings(minimum_version=None)
        assert check_minimum_sdk_version(Version("0.0.1"), settings) is None

    def test_unknown_version_is_allowed(self):
        settings = SdkCompatSettings(minimum_version="0.18.0")
        assert check_minimum_sdk_version(None, settings) is None

    @pytest.mark.parametrize("version", ["0.18.0", "0.18.1", "1.0.0"])
    def test_at_or_above_floor_allowed(self, version):
        settings = SdkCompatSettings(minimum_version="0.18.0")
        assert check_minimum_sdk_version(Version(version), settings) is None

    @pytest.mark.parametrize("version", ["0.17.9", "0.1.0", "0.17.0.dev1"])
    def test_below_floor_rejected(self, version):
        settings = SdkCompatSettings(minimum_version="0.18.0")
        rejection = check_minimum_sdk_version(Version(version), settings)
        assert rejection is not None
        assert rejection.sdk_version == version
        assert rejection.minimum_sdk_version == "0.18.0"

    @pytest.mark.parametrize(
        "version", ["0.18.0.dev1", "0.18.0rc1", "0.18.0a1", "0.18.0+g1a2b3c"]
    )
    def test_prerelease_and_dev_builds_of_the_floor_are_allowed(self, version):
        """A local build of the very version we require must not be told to
        upgrade to it. Pre-release/dev/local parts are ignored for the
        comparison; strict PEP 440 ordering would sort these below 0.18.0."""
        settings = SdkCompatSettings(minimum_version="0.18.0")
        assert check_minimum_sdk_version(Version(version), settings) is None


# ---------------------------------------------------------------------------
# GET /version publishes the policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_reports_no_minimum_by_default(client: AsyncClient):
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json()["minimum_sdk_version"] is None


@pytest.mark.asyncio
async def test_version_reports_configured_minimum(
    client: AsyncClient, minimum_sdk_version
):
    minimum_sdk_version("0.18.0")
    response = await client.get("/api/v1/version")
    assert response.status_code == 200
    assert response.json()["minimum_sdk_version"] == "0.18.0"


@pytest.mark.asyncio
async def test_version_is_never_blocked(client: AsyncClient, minimum_sdk_version):
    """A refused client must still be able to learn what it needs to upgrade to."""
    minimum_sdk_version("0.18.0")
    response = await client.get(
        "/api/v1/version", headers={SDK_VERSION_HEADER: "0.1.0"}
    )
    assert response.status_code == 200
    assert response.json()["minimum_sdk_version"] == "0.18.0"


@pytest.mark.asyncio
async def test_health_is_never_blocked(client: AsyncClient, minimum_sdk_version):
    minimum_sdk_version("0.18.0")
    response = await client.get("/health", headers={SDK_VERSION_HEADER: "0.1.0"})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Ingestion: the header is read and recorded, and never fatal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_absent_header_is_allowed(client: AsyncClient, minimum_sdk_version):
    """THE compatibility guarantee: every SDK released before the header
    existed sends nothing, and must keep working."""
    minimum_sdk_version("0.18.0")
    response = await client.get(SDK_ENDPOINT)
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["", "  ", "banana", "0.18.0.0.0.x", "<script>"])
async def test_malformed_header_is_allowed_not_500(
    client: AsyncClient, minimum_sdk_version, raw
):
    minimum_sdk_version("0.18.0")
    response = await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: raw})
    assert response.status_code == 200


async def _run_middleware(header_value: bytes | None) -> tuple[object, object]:
    """Drive the middleware over a bare ASGI app and read back request state."""
    from starlette.requests import Request

    from stardag_api.middleware.sdk_version import SdkVersionMiddleware

    captured: list[tuple[object, object]] = []

    async def _app(scope, receive, send):
        request = Request(scope, receive)
        captured.append((request.state.sdk_version, request.state.sdk_version_raw))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    headers = (
        []
        if header_value is None
        else [(SDK_VERSION_HEADER.lower().encode(), header_value)]
    )
    scope = {"type": "http", "path": SDK_ENDPOINT, "headers": headers}

    async def _receive():  # pragma: no cover - the bare app never reads a body
        return {"type": "http.request", "body": b"", "more_body": False}

    async def _send(message):
        return None

    await SdkVersionMiddleware(_app)(scope, _receive, _send)
    return captured[0]


@pytest.mark.asyncio
async def test_parsed_version_is_attached_to_request_state():
    """Downstream code can attribute behaviour to a client version."""
    assert await _run_middleware(b" 0.18.0 ") == ("0.18.0", " 0.18.0 ")


@pytest.mark.asyncio
async def test_request_state_distinguishes_absent_from_malformed():
    """Both are "unknown" for policy, but the raw value is kept for diagnosis."""
    assert await _run_middleware(None) == (None, None)
    assert await _run_middleware(b"banana") == (None, "banana")


@pytest.mark.asyncio
async def test_each_distinct_version_is_logged_once(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
):
    """Recording is per distinct version per process, not per request — the
    question it answers is "who is still on 0.14?", and one line per request
    would bury the answer."""
    with caplog.at_level(logging.INFO, logger="stardag_api.sdk_compat"):
        for _ in range(3):
            await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.14.0"})
        await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.18.0"})

    messages = [r.getMessage() for r in caplog.records]
    assert sum("0.14.0" in m for m in messages) == 1
    assert sum("0.18.0" in m for m in messages) == 1


@pytest.mark.asyncio
async def test_malformed_header_is_logged_once(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.INFO, logger="stardag_api.sdk_compat"):
        for _ in range(2):
            await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "banana"})

    messages = [r.getMessage() for r in caplog.records]
    assert sum("banana" in m for m in messages) == 1


# ---------------------------------------------------------------------------
# The floor, once configured
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_below_minimum_is_refused_with_426(
    client: AsyncClient, minimum_sdk_version
):
    minimum_sdk_version("0.18.0")
    response = await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.14.0"})
    assert response.status_code == 426

    detail = response.json()["detail"]
    assert detail["error_code"] == SDK_VERSION_ERROR_CODE
    assert detail["sdk_version"] == "0.14.0"
    assert detail["minimum_sdk_version"] == "0.18.0"
    # Both version numbers and the upgrade command must be in the prose the
    # user actually reads — the message is the whole feature.
    assert "0.14.0" in detail["message"]
    assert "0.18.0" in detail["message"]
    assert 'pip install --upgrade "stardag>=0.18.0"' in detail["message"]


@pytest.mark.asyncio
async def test_at_minimum_is_allowed(client: AsyncClient, minimum_sdk_version):
    minimum_sdk_version("0.18.0")
    response = await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.18.0"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_above_minimum_is_allowed(client: AsyncClient, minimum_sdk_version):
    minimum_sdk_version("0.18.0")
    response = await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.19.3"})
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_dev_build_of_the_minimum_is_allowed(
    client: AsyncClient, minimum_sdk_version
):
    minimum_sdk_version("0.18.0")
    response = await client.get(
        SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.18.0.dev1"}
    )
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_no_minimum_configured_refuses_nothing(client: AsyncClient):
    """The state this ships in: an ancient SDK is served normally."""
    assert sdk_compat_settings.minimum_version is None
    response = await client.get(SDK_ENDPOINT, headers={SDK_VERSION_HEADER: "0.0.1"})
    assert response.status_code == 200


def test_health_is_exempt_with_and_without_a_trailing_slash():
    """`redirect_slashes` sends `/health/` through the router, which runs
    *after* this middleware — so the canonical spelling alone would gate the
    variant with a 426 before the redirect could happen."""
    from stardag_api.middleware.sdk_version import _EXEMPT_PATHS

    for path in ("/health", "/health/", "/api/v1/version", "/api/v1/version/"):
        assert path in _EXEMPT_PATHS


def test_an_absent_header_is_recorded_not_skipped():
    """Allowed, but observed: "how many callers do not send the header yet"
    is the number a floor has to be set against."""
    from stardag_api.sdk_compat import (
        _seen_sdk_versions,
        clear_seen_sdk_versions,
        record_sdk_version,
    )

    clear_seen_sdk_versions()
    record_sdk_version(None, None)
    assert _seen_sdk_versions == {"<absent>"}

    # Deduped like any other label, so it cannot flood the log.
    record_sdk_version(None, None)
    assert _seen_sdk_versions == {"<absent>"}
    clear_seen_sdk_versions()
