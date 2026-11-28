"""The S3 file system's async client: one per event loop, closed with it.

Every async operation used to open its own aioboto3 client, paying for client
construction and a TLS handshake per call, which dominated discovery of
S3-backed tasks (STA-181). These tests pin the replacement: one client per
loop, created once under concurrent first use, and closed on loop shutdown or
an explicit ``aclose``. The session is stubbed, so no AWS account is reached.
"""

import asyncio
import gc
import threading
import time
import warnings
from pathlib import Path

import pytest

# The s3 extra is optional: without it, skip rather than fail collection.
pytest.importorskip("aioboto3")
pytest.importorskip("botocore")

from botocore.exceptions import ClientError  # noqa: E402

import stardag as sd  # noqa: E402
from stardag.build import build_sequential  # noqa: E402
from stardag.integration.aws.s3 import (  # noqa: E402
    DEFAULT_MAX_POOL_CONNECTIONS,
    S3FileSystem,
    S3FileSystemConfig,
)
from stardag.target import CachedRemoteFileSystem, RemoteFileTarget  # noqa: E402


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code}}, "HeadObject")


class _FakeClient:
    def __init__(self, session: "_FakeSession") -> None:
        self.session = session
        self.loop = asyncio.get_running_loop()
        self.closed = False

    def _check_loop(self) -> None:
        # An aiobotocore client used from another loop fails; so does this.
        assert asyncio.get_running_loop() is self.loop
        assert not self.closed

    async def head_object(self, *, Bucket: str, Key: str) -> dict:
        self._check_loop()
        await asyncio.sleep(self.session.head_latency)
        if Key.startswith("missing"):
            raise _client_error("404")
        if Key.startswith("forbidden"):
            raise _client_error("403")
        return {}

    async def download_file(self, bucket: str, key: str, filename: str) -> None:
        self._check_loop()
        self.session.calls.append(("download", bucket, key, filename))

    async def upload_file(self, filename: str, bucket: str, key: str) -> None:
        self._check_loop()
        self.session.calls.append(("upload", bucket, key, filename))


class _FakeClientContext:
    def __init__(self, session: "_FakeSession") -> None:
        self.session = session
        self.client: _FakeClient | None = None

    async def __aenter__(self) -> _FakeClient:
        # Construction (service model, event handlers) and the first TLS
        # handshake are what the shared client amortizes.
        await asyncio.sleep(self.session.setup_latency)
        if self.session.fail_setups > 0:
            self.session.fail_setups -= 1
            raise RuntimeError("could not construct the client")
        self.client = _FakeClient(self.session)
        self.session.created.append(self.client)
        return self.client

    async def __aexit__(self, *exc_info) -> None:
        assert self.client is not None
        await asyncio.sleep(self.session.close_latency)
        self.client.closed = True


class _FakeSession:
    """Stands in for ``aioboto3.Session``: counts clients, opens none."""

    def __init__(self, setup_latency: float = 0.0, head_latency: float = 0.0):
        self.setup_latency = setup_latency
        self.head_latency = head_latency
        self.created: list[_FakeClient] = []
        self.configs: list = []
        self.calls: list[tuple] = []
        self.fail_setups = 0
        self.close_latency = 0.0
        self._lock = threading.Lock()

    def client(self, service_name: str, config=None) -> _FakeClientContext:
        assert service_name == "s3"
        with self._lock:
            self.configs.append(config)
        return _FakeClientContext(self)


def _file_system(session: _FakeSession, **kwargs) -> S3FileSystem:
    return S3FileSystem(
        s3_client=object(),  # type: ignore[arg-type]  # sync path unused here
        aioboto3_session=session,  # type: ignore[arg-type]
        **kwargs,
    )


async def test_concurrent_calls_on_a_cold_loop_share_one_client():
    session = _FakeSession(setup_latency=0.01)
    fs = _file_system(session)

    results = await asyncio.gather(
        *(fs.exists_aio(f"s3://bucket/key-{i}") for i in range(200)),
        *(fs.exists_aio(f"s3://bucket/missing-{i}") for i in range(50)),
    )

    assert results == [True] * 200 + [False] * 50
    assert len(session.created) == 1
    await fs.aclose()


async def test_download_and_upload_reuse_the_same_client(tmp_path: Path):
    session = _FakeSession()
    fs = _file_system(session)

    assert await fs.exists_aio("s3://bucket/a")
    await fs.download_aio("s3://bucket/a", tmp_path / "a")
    await fs.upload_aio(tmp_path / "a", "s3://bucket/nested/b")

    assert len(session.created) == 1
    assert session.calls == [
        ("download", "bucket", "a", str(tmp_path / "a")),
        ("upload", "bucket", "nested/b", str(tmp_path / "a")),
    ]
    await fs.aclose()


async def test_a_non_404_error_still_raises():
    fs = _file_system(_FakeSession())
    with pytest.raises(ClientError):
        await fs.exists_aio("s3://bucket/forbidden")
    # ...and does not cost the client.
    assert await fs.exists_aio("s3://bucket/a")
    await fs.aclose()


def test_each_loop_gets_its_own_client_closed_at_loop_shutdown():
    session = _FakeSession()
    fs = _file_system(session)

    async def check() -> None:
        await asyncio.gather(*(fs.exists_aio(f"s3://b/k{i}") for i in range(20)))

    # No explicit aclose: asyncio.run shuts down async generators before it
    # closes the loop, and that is what closes the client.
    asyncio.run(check())
    asyncio.run(check())

    assert len(session.created) == 2
    first, second = session.created
    assert first.loop is not second.loop
    assert first.closed and second.closed
    # A closed loop's entry does not linger.
    assert fs._loop_clients == {}


def test_loops_on_different_threads_do_not_share_a_client():
    session = _FakeSession(setup_latency=0.01, head_latency=0.01)
    fs = _file_system(session)
    errors: list[BaseException] = []

    def run() -> None:
        async def check() -> None:
            for _ in range(3):
                await asyncio.gather(
                    *(fs.exists_aio(f"s3://b/k{i}") for i in range(10))
                )

        try:
            asyncio.run(check())
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(session.created) == 4
    assert len({client.loop for client in session.created}) == 4
    assert all(client.closed for client in session.created)


async def test_aclose_closes_the_client_and_a_later_call_opens_a_new_one():
    session = _FakeSession()
    fs = _file_system(session)

    assert await fs.exists_aio("s3://bucket/a")
    await fs.aclose()
    assert session.created[0].closed

    assert await fs.exists_aio("s3://bucket/a")
    assert len(session.created) == 2
    assert not session.created[1].closed
    await fs.aclose()
    assert session.created[1].closed
    # Closing a loop with no client is a no-op.
    await fs.aclose()


async def test_a_failed_client_construction_is_retried_by_the_next_call():
    session = _FakeSession()
    session.fail_setups = 1
    fs = _file_system(session)

    with pytest.raises(RuntimeError, match="could not construct"):
        await fs.exists_aio("s3://bucket/a")
    assert await fs.exists_aio("s3://bucket/a")
    assert await fs.exists_aio("s3://bucket/b")
    assert len(session.created) == 1
    await fs.aclose()
    assert session.created[0].closed


async def test_aclose_cancelled_during_a_construction_leaves_the_client_reachable():
    """Cancelled while waiting for a construction, ``aclose`` changes nothing.

    The entry stays published, so the constructed client is the one every
    later call reuses (no second pool) and a later ``aclose`` closes it.
    """
    session = _FakeSession(setup_latency=0.1)
    fs = _file_system(session)

    check = asyncio.create_task(fs.exists_aio("s3://bucket/a"))
    await asyncio.sleep(0.02)  # the check now holds the lock, constructing
    closing = asyncio.create_task(fs.aclose())
    await asyncio.sleep(0.02)  # aclose now waits for the lock
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert await check
    assert await fs.exists_aio("s3://bucket/b")
    assert len(session.created) == 1
    assert not session.created[0].closed

    await fs.aclose()
    assert session.created[0].closed


async def test_aclose_cancelled_while_closing_still_closes_the_client():
    session = _FakeSession()
    session.close_latency = 0.1
    fs = _file_system(session)
    assert await fs.exists_aio("s3://bucket/a")

    closing = asyncio.create_task(fs.aclose())
    await asyncio.sleep(0.02)  # the entry is unpublished, the close under way
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing

    await asyncio.sleep(0.15)
    assert session.created[0].closed


async def test_a_call_waiting_out_an_aclose_opens_a_reachable_client():
    """A call that waited on the lock while ``aclose`` took the entry looks up
    again: its client is published, so ``aclose`` can reach and close it."""
    session = _FakeSession(setup_latency=0.05)
    fs = _file_system(session)

    first = asyncio.create_task(fs.exists_aio("s3://bucket/a"))
    await asyncio.sleep(0.01)  # constructing, under the lock
    closing = asyncio.create_task(fs.aclose())
    await asyncio.sleep(0.01)
    late = asyncio.create_task(fs.exists_aio("s3://bucket/b"))  # waits too

    assert await first
    await closing
    assert await late

    assert len(session.created) == 2
    assert session.created[0].closed
    assert not session.created[1].closed
    await fs.aclose()
    assert session.created[1].closed
    assert fs._loop_clients == {}


async def test_a_cached_wrapper_forwards_aclose(tmp_path: Path):
    session = _FakeSession()
    fs = CachedRemoteFileSystem(wrapped=_file_system(session), root=str(tmp_path))

    assert await fs.exists_aio("s3://bucket/a")
    await fs.aclose()
    assert session.created[0].closed


async def test_time_scales_with_latency_over_concurrency_not_per_call_setup():
    """N checks at concurrency C take about latency * N / C, plus one setup.

    With a client per call each check would also pay the setup, which here
    is as large as the request itself, roughly doubling the total.
    """
    latency, setup, n, concurrency = 0.05, 0.05, 160, 16
    session = _FakeSession(setup_latency=setup, head_latency=latency)
    fs = _file_system(session)
    semaphore = asyncio.Semaphore(concurrency)

    async def check(i: int) -> bool:
        async with semaphore:
            return await fs.exists_aio(f"s3://bucket/key-{i}")

    started = time.monotonic()
    assert all(await asyncio.gather(*(check(i) for i in range(n))))
    elapsed = time.monotonic() - started

    ideal = latency * n / concurrency + setup  # 0.55 s
    per_call_setup = (latency + setup) * n / concurrency  # 1.0 s
    assert len(session.created) == 1
    assert ideal * 0.9 <= elapsed < per_call_setup * 0.9, elapsed
    await fs.aclose()


async def test_the_client_pool_is_sized_for_discovery_concurrency():
    session = _FakeSession()
    await _file_system(session).exists_aio("s3://bucket/a")
    await _file_system(session, max_pool_connections=128).exists_aio("s3://bucket/a")

    pools = [config.max_pool_connections for config in session.configs]
    assert pools == [DEFAULT_MAX_POOL_CONNECTIONS, 128]
    # Above every builder's default max_concurrent_discover.
    assert DEFAULT_MAX_POOL_CONNECTIONS >= 50


def test_pool_size_is_configurable_from_the_environment(monkeypatch):
    monkeypatch.setenv("STARDAG_TARGET_S3_MAX_POOL_CONNECTIONS", "200")
    assert S3FileSystemConfig().max_pool_connections == 200


def test_a_real_client_is_closed_at_loop_shutdown_without_warnings(monkeypatch):
    """The real aioboto3 client, with its HTTP session open but no request.

    aiobotocore opens the aiohttp session lazily on the first request, so the
    test opens it by hand: a client that was never used has nothing to leak.
    """
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    import aioboto3

    fs = S3FileSystem(
        s3_client=object(),  # type: ignore[arg-type]
        aioboto3_session=aioboto3.Session(),
        max_pool_connections=33,
    )
    clients = []
    http_sessions = []

    async def open_client() -> None:
        client = await fs._client_aio()
        clients.append(client)
        assert await fs._client_aio() is client
        endpoint = client._endpoint  # pyright: ignore[reportAttributeAccessIssue]
        http_sessions.append(await endpoint.http_session._get_session(None))

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        asyncio.run(open_client())
        gc.collect()

    assert clients[0].meta.config.max_pool_connections == 33
    assert http_sessions[0].closed
    assert fs._loop_clients == {}
    unclosed = [w for w in caught if "nclosed" in str(w.message)]
    assert unclosed == []


# A sequential build runs discovery on its own helper loop rather than under
# ``asyncio.run``, so it has to shut that loop down the same way for the
# client to close. The file system a task's target uses is module state here,
# since a task's fields must be serializable.
_BUILD_FS: list[S3FileSystem] = []


class _S3CheckedTask(sd.TargetTask[RemoteFileTarget]):
    __namespace__ = "s3_client_tests"

    key: str

    def target(self) -> RemoteFileTarget:
        return RemoteFileTarget(f"s3://bucket/{self.key}", _BUILD_FS[-1])

    def run(self) -> None:
        raise RuntimeError("this task fails when it runs")


class _S3CheckedRoot(sd.TargetTask[RemoteFileTarget]):
    __namespace__ = "s3_client_tests"

    keys: tuple[str, ...]

    def requires(self):  # type: ignore[override]
        return [_S3CheckedTask(key=key) for key in self.keys]

    def target(self) -> RemoteFileTarget:
        return RemoteFileTarget("s3://bucket/missing-root", _BUILD_FS[-1])

    def run(self) -> None:
        pass


@pytest.fixture
def build_session():
    session = _FakeSession()
    _BUILD_FS.append(_file_system(session))
    yield session
    _BUILD_FS.clear()


def test_a_sequential_build_closes_the_client_it_opened(build_session):
    summary = build_sequential(
        [_S3CheckedTask(key=f"done-{i}") for i in range(20)], registry=None
    )

    assert summary is not None
    assert len(build_session.created) == 1
    assert build_session.created[0].closed
    assert _BUILD_FS[-1]._loop_clients == {}


def test_a_failed_sequential_build_closes_the_client_too(build_session):
    # The root's dependencies are missing, so one runs and raises.
    root = _S3CheckedRoot(keys=("missing-a", "missing-b"))
    with pytest.raises(Exception, match="fails when it runs"):
        build_sequential(root, registry=None)

    assert len(build_session.created) == 1
    assert build_session.created[0].closed
