import asyncio
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, AsyncGenerator

from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.target import (
    CachedRemoteFileSystem,
    CachedRemoteFileSystemConfig,
    RemoteFileSystemABC,
)
from stardag.utils.resource_provider import resource_provider

try:
    import aioboto3
    import boto3
    from aiobotocore.config import AioConfig
    from botocore.exceptions import ClientError
except ImportError as e:
    raise ImportError(
        "boto3, botocore, and aioboto3 are required for S3 support. "
        "Install with: pip install 'stardag[s3]'"
    ) from e

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
    from types_aiobotocore_s3.client import S3Client as AsyncS3Client
else:
    S3Client = object
    AsyncS3Client = object

DEFAULT_CACHE_ROOT = Path("~/.stardag/cache/s3/").expanduser().absolute()

#: Connections the shared async client keeps per event loop. Every async
#: operation on a loop goes through one client, so its pool bounds how many
#: are in flight: it must be at least the discovery concurrency
#: (``max_concurrent_discover``, 16 by default and 50 for the resident and
#: concurrent builders), or the extra checks queue for a connection.
#: botocore's own default is 10.
DEFAULT_MAX_POOL_CONNECTIONS = 64


class S3CacheConfig(CachedRemoteFileSystemConfig):
    root: str = str(DEFAULT_CACHE_ROOT)

    model_config = SettingsConfigDict(
        env_prefix="stardag_target_s3_cache_",
        env_nested_delimiter="__",
    )


class S3FileSystemConfig(BaseSettings):
    use_cache: bool = False
    max_pool_connections: int = DEFAULT_MAX_POOL_CONNECTIONS
    cache: S3CacheConfig = S3CacheConfig()

    model_config = SettingsConfigDict(env_prefix="stardag_target_s3_")


@dataclass(eq=False)
class _LoopClient:
    """The shared async client of one event loop, and how to close it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: AsyncS3Client | None = None
    lifetime: AsyncGenerator[AsyncS3Client, None] | None = None


class S3FileSystem(RemoteFileSystemABC):
    """S3 as a remote file system.

    The sync operations share one boto3 client. The async ones share one
    aioboto3 client *per event loop*, since an aiobotocore client is bound to
    the loop it was created on: it is created lazily by the first async
    operation on a loop, reused by every later one, and closed when the loop
    shuts down its async generators (which ``asyncio.run`` does before closing
    the loop) or by an explicit :meth:`aclose`.
    """

    URI_PREFIX = "s3://"

    def __init__(
        self,
        s3_client: S3Client | None = None,
        aioboto3_session: aioboto3.Session | None = None,
        max_pool_connections: int = DEFAULT_MAX_POOL_CONNECTIONS,
    ):
        self.s3_client: S3Client = s3_client or boto3.client("s3")
        self.aioboto3_session = aioboto3_session or aioboto3.Session()
        self.max_pool_connections = max_pool_connections
        # Not a WeakKeyDictionary: the client's HTTP session holds its loop,
        # so an entry would keep its own key alive. Entries leave when the
        # client closes, and a loop closed without shutting down its async
        # generators is pruned on the next lookup.
        self._loop_clients: dict[asyncio.AbstractEventLoop, _LoopClient] = {}
        # Loops live on different threads; guards the mapping only.
        self._loop_clients_lock = threading.Lock()

    def exists(self, uri: str) -> bool:
        bucket, key = get_bucket_key(uri)
        try:
            self.s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") == "404":
                # The key does not exist.
                return False
            # Something else has gone wrong.
            raise

    def download(self, uri: str, destination: Path):
        bucket, key = get_bucket_key(uri)
        self.s3_client.download_file(bucket, key, str(destination))

    def upload(self, source: Path, uri: str, ok_remove: bool = False):
        bucket, key = get_bucket_key(uri)
        self.s3_client.upload_file(str(source), bucket, key)

    # Async implementations using aioboto3

    async def _client_aio(self) -> AsyncS3Client:
        """This loop's shared async client, created on first use."""
        loop = asyncio.get_running_loop()
        while True:
            with self._loop_clients_lock:
                entry = self._loop_clients.get(loop)
                if entry is None:
                    for closed in [lp for lp in self._loop_clients if lp.is_closed()]:
                        del self._loop_clients[closed]
                    entry = self._loop_clients[loop] = _LoopClient()
            if entry.client is not None:
                return entry.client
            # Many coroutines can arrive at a cold loop at once (a discovery
            # walk starts with a burst of completion checks): one creates the
            # client, the rest wait for it.
            async with entry.lock:
                with self._loop_clients_lock:
                    published = self._loop_clients.get(loop) is entry
                if not published:
                    # aclose() took the entry while this call waited: look up
                    # again rather than open a client nothing can reach.
                    continue
                if entry.client is None:
                    lifetime = self._client_lifetime(loop, entry)
                    entry.client = await anext(lifetime)
                    entry.lifetime = lifetime
                return entry.client

    async def _client_lifetime(
        self, loop: asyncio.AbstractEventLoop, entry: _LoopClient
    ) -> AsyncGenerator[AsyncS3Client, None]:
        """Hold one client open for as long as this generator is suspended.

        An async generator, rather than a bare ``__aenter__``, because the
        loop tracks it: ``loop.shutdown_asyncgens()`` closes it, which exits
        the client context and closes the connection pool on the loop that
        owns it, without anyone having to call :meth:`aclose`.
        """
        s3: AsyncS3Client
        async with self.aioboto3_session.client(
            "s3",
            config=AioConfig(max_pool_connections=self.max_pool_connections),
        ) as s3:
            try:
                yield s3
            finally:
                # Unpublish before the client closes. Only a client that
                # opened gets here: a failed construction leaves the entry
                # for the next caller to retry on.
                with self._loop_clients_lock:
                    if self._loop_clients.get(loop) is entry:
                        del self._loop_clients[loop]
                entry.client = entry.lifetime = None

    async def aclose(self) -> None:
        """Close the running loop's shared async client, if it has one.

        A later async operation on the loop opens a new one. Not needed
        before ``asyncio.run`` returns: loop shutdown closes the client.
        """
        loop = asyncio.get_running_loop()
        with self._loop_clients_lock:
            entry = self._loop_clients.get(loop)
        if entry is None:
            return
        # Unpublish only once the lock is held, so that a construction in
        # flight finishes first. Cancelled while waiting, this has changed
        # nothing: the entry stays published, and its client reachable.
        async with entry.lock:
            with self._loop_clients_lock:
                if self._loop_clients.get(loop) is entry:
                    del self._loop_clients[loop]
            lifetime, entry.lifetime, entry.client = entry.lifetime, None, None
        if lifetime is not None:
            # Unpublished now, so nothing else will close it: finish the
            # close even if this call is cancelled (which still propagates).
            await asyncio.shield(_close_lifetime(lifetime))

    async def exists_aio(self, uri: str) -> bool:
        """Asynchronously check if the S3 object exists."""
        bucket, key = get_bucket_key(uri)
        s3 = await self._client_aio()
        try:
            await s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            error = exc.response.get("Error", {})
            if error.get("Code") == "404":
                return False
            raise

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Asynchronously download a file from S3."""
        bucket, key = get_bucket_key(uri)
        s3 = await self._client_aio()
        await s3.download_file(bucket, key, str(destination))

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Asynchronously upload a file to S3."""
        bucket, key = get_bucket_key(uri)
        s3 = await self._client_aio()
        await s3.upload_file(str(source), bucket, key)


async def _close_lifetime(lifetime: AsyncGenerator[AsyncS3Client, None]) -> None:
    await lifetime.aclose()


def get_bucket_key(uri: str) -> tuple[str, str]:
    """Get the bucket and key from the given S3 URI."""
    if not uri.startswith(S3FileSystem.URI_PREFIX):
        raise ValueError(f"Unexpected URI {uri}.")
    bucket_key = uri[len(S3FileSystem.URI_PREFIX) :]
    bucket, key = bucket_key.split("/", 1)
    return bucket, key


def get_uri(bucket: str, key: str) -> str:
    """Get the S3 URI from the given bucket and key."""
    return f"{S3FileSystem.URI_PREFIX}{bucket}/{key}"


s3_client_provider = resource_provider(S3Client, lambda: boto3.client("s3"))

aioboto3_session_provider = resource_provider(aioboto3.Session, aioboto3.Session)


def _init_s3_file_system() -> RemoteFileSystemABC:
    config = S3FileSystemConfig()
    file_system = S3FileSystem(
        s3_client=s3_client_provider.get(),
        aioboto3_session=aioboto3_session_provider.get(),
        max_pool_connections=config.max_pool_connections,
    )
    if config.use_cache:
        file_system = CachedRemoteFileSystem(
            wrapped=file_system,
            **config.cache.model_dump(),
        )

    return file_system


s3_rfs_provider = resource_provider(RemoteFileSystemABC, _init_s3_file_system)
