"""The volume API's existence check survives transient failures (STA-169).

``Volume.iterdir`` is a streaming RPC, and Modal's client retries only its
unary RPCs: a stream reset mid-call surfaced as a bare
``grpclib.exceptions.StreamTerminatedError`` and killed a build's discovery
walk. These tests need no Modal credentials -- a fake volume is installed
into ``_get_volume``.
"""

from types import SimpleNamespace

import pytest

try:
    import modal  # noqa: F401  -- gate import errors at module level
except ImportError:
    pytest.skip("Skipping modal tests (import not available)", allow_module_level=True)

from grpclib.exceptions import StreamTerminatedError
from modal.exception import (
    InternalError,
    InvalidError,
    NotFoundError,
    PermissionDeniedError,
    ResourceExhaustedError,
    ServiceError,
)
from tenacity import wait_none

from stardag.integration.modal import _target as modal_target
from stardag.integration.modal._target import (
    FileEntryType,
    ModalVolumeRemoteFileSystem,
)

URI = "modalvol://some-volume/root/out.json"
IN_VOLUME_PATH = "root/out.json"


class _FlakyVolume:
    """``iterdir`` (sync and ``.aio``) raises the queued errors in order,
    then lists the file."""

    def __init__(self, errors: list[BaseException]):
        self.errors = list(errors)
        self.calls = 0
        volume = self

        def iterdir(path, recursive=False):
            volume.calls += 1
            if volume.errors:
                raise volume.errors.pop(0)
            return iter([self._entry(path)])

        async def iterdir_aio(path, recursive=False):
            volume.calls += 1
            if volume.errors:
                raise volume.errors.pop(0)
            yield self._entry(path)

        iterdir.aio = iterdir_aio  # type: ignore[attr-defined]
        self.iterdir = iterdir

    @staticmethod
    def _entry(path: str):
        return SimpleNamespace(type=FileEntryType.FILE, path=path)


@pytest.fixture(autouse=True)
def no_retry_wait(monkeypatch):
    for method in (
        ModalVolumeRemoteFileSystem.exists,
        ModalVolumeRemoteFileSystem.exists_aio,
    ):
        monkeypatch.setattr(method.retry, "wait", wait_none())  # type: ignore[attr-defined]


def _install(monkeypatch, volume: _FlakyVolume) -> None:
    monkeypatch.setattr(modal_target, "_get_volume", lambda name: volume)


TRANSIENT = [
    pytest.param(lambda: StreamTerminatedError("Protocol error"), id="stream-reset"),
    pytest.param(lambda: ServiceError("unavailable"), id="service-error"),
    pytest.param(lambda: InternalError("internal"), id="internal-error"),
    pytest.param(lambda: ResourceExhaustedError("rate limited"), id="rate-limited"),
    pytest.param(lambda: ConnectionError("reset by peer"), id="connection-error"),
]


@pytest.mark.parametrize("make_error", TRANSIENT)
async def test_exists_aio_retries_a_transient_failure(monkeypatch, make_error):
    volume = _FlakyVolume([make_error(), make_error()])
    _install(monkeypatch, volume)

    assert await ModalVolumeRemoteFileSystem().exists_aio(URI) is True
    assert volume.calls == 3


@pytest.mark.parametrize("make_error", TRANSIENT)
def test_exists_retries_a_transient_failure(monkeypatch, make_error):
    volume = _FlakyVolume([make_error(), make_error()])
    _install(monkeypatch, volume)

    assert ModalVolumeRemoteFileSystem().exists(URI) is True
    assert volume.calls == 3


@pytest.mark.parametrize(
    "error",
    [InvalidError("bad path"), PermissionDeniedError("no")],
    ids=["invalid", "permission-denied"],
)
async def test_exists_aio_raises_a_non_transient_failure_at_once(monkeypatch, error):
    volume = _FlakyVolume([error])
    _install(monkeypatch, volume)

    with pytest.raises(type(error)):
        await ModalVolumeRemoteFileSystem().exists_aio(URI)
    assert volume.calls == 1


async def test_a_missing_file_is_an_answer_not_a_retry(monkeypatch):
    volume = _FlakyVolume([NotFoundError("no such path")])
    _install(monkeypatch, volume)

    assert await ModalVolumeRemoteFileSystem().exists_aio(URI) is False
    assert volume.calls == 1


async def test_an_exhausted_budget_reraises_the_failure_itself(monkeypatch):
    """``reraise=True``: the caller sees the stream reset, not a RetryError."""
    attempts = modal_target._MAX_VOLUME_API_ATTEMPTS
    volume = _FlakyVolume(
        [StreamTerminatedError("Protocol error") for _ in range(attempts)]
    )
    _install(monkeypatch, volume)

    with pytest.raises(StreamTerminatedError, match="Protocol error"):
        await ModalVolumeRemoteFileSystem().exists_aio(URI)
    assert volume.calls == attempts
