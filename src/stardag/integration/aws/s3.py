from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

from stardag.target import RemoteFileSystemABC
from stardag.utils.resource_provider import resource_provider

if TYPE_CHECKING:
    from mypy_boto3_s3.client import S3Client
else:
    S3Client = object

URI_PREFIX = "s3://"


class S3FileSystem(RemoteFileSystemABC):
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

    def upload(self, source: Path, uri: str):
        bucket, key = get_bucket_key(uri)
        self.s3_client.upload_file(str(source), bucket, key)


def get_bucket_key(uri: str) -> tuple[str, str]:
    """Get the bucket and key from the given S3 URI."""
    if not uri.startswith(URI_PREFIX):
        raise ValueError(f"Unexpected URI {uri}.")
    bucket_key = uri[len(URI_PREFIX) :]
    bucket, key = bucket_key.split("/", 1)
    return bucket, key


def get_uri(bucket: str, key: str) -> str:
    """Get the S3 URI from the given bucket and key."""
    return f"{URI_PREFIX}{bucket}/{key}"


s3_client_provider = resource_provider(S3Client, lambda: boto3.client("s3"))

s3_rfs_provider = resource_provider(
    S3FileSystem, lambda: S3FileSystem(s3_client=s3_client_provider.get())
)
