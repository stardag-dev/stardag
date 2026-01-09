from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError
from pydantic_settings import BaseSettings, SettingsConfigDict

from stardag.target import CachedRemoteFileSystem, RemoteFileSystemABC
from stardag.target._base import CachedRemoteFileSystemConfig
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = object

DEFAULT_CACHE_ROOT = Path("~/.stardag/cache/s3/").expanduser().absolute()


class S3CacheConfig(CachedRemoteFileSystemConfig):
    root: str = str(DEFAULT_CACHE_ROOT)

    model_config = SettingsConfigDict(
        env_prefix="stardag_target_s3_cache_",
        env_nested_delimiter="__",
    )


class S3FileSystemConfig(BaseSettings):
    use_cache: bool = False
    cache: S3CacheConfig = S3CacheConfig()

    model_config = SettingsConfigDict(env_prefix="stardag_target_s3_")


class S3FileSystem(RemoteFileSystemABC):
    URI_PREFIX = "s3://"

    def __init__(self, s3_client: S3Client | None = None):
        self.s3_client: S3Client = s3_client or boto3.client("s3")

    def exists(self, uri: str) -> bool:
        bucket, key = get_bucket_key(uri)
        try:
            self.s3_client.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "404":  # type: ignore
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

    async def exists_aio(self, uri: str) -> bool:
        """Asynchronously check if the S3 object exists."""
        import aioboto3  # type: ignore[import-not-found]

        bucket, key = get_bucket_key(uri)
        session = aioboto3.Session()
        async with session.client("s3") as s3:  # type: ignore[union-attr]
            try:
                await s3.head_object(Bucket=bucket, Key=key)
                return True
            except ClientError as exc:
                if exc.response["Error"]["Code"] == "404":  # type: ignore
                    return False
                raise

    async def download_aio(self, uri: str, destination: Path) -> None:
        """Asynchronously download a file from S3."""
        import aioboto3  # type: ignore[import-not-found]

        bucket, key = get_bucket_key(uri)
        session = aioboto3.Session()
        async with session.client("s3") as s3:  # type: ignore[union-attr]
            await s3.download_file(bucket, key, str(destination))

    async def upload_aio(self, source: Path, uri: str, ok_remove: bool = False) -> None:
        """Asynchronously upload a file to S3."""
        import aioboto3  # type: ignore[import-not-found]

        bucket, key = get_bucket_key(uri)
        session = aioboto3.Session()
        async with session.client("s3") as s3:  # type: ignore[union-attr]
            await s3.upload_file(str(source), bucket, key)


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


def _init_s3_file_system() -> RemoteFileSystemABC:
    file_system = S3FileSystem(s3_client=s3_client_provider.get())
    config = S3FileSystemConfig()
    if config.use_cache:
        file_system = CachedRemoteFileSystem(
            wrapped=file_system,
            **config.cache.model_dump(),
        )

    return file_system


s3_rfs_provider = resource_provider(RemoteFileSystemABC, _init_s3_file_system)
