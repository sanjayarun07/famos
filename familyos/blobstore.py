"""Object storage for sealed originals: S3-compatible in production, a local
directory in development and tests. Keys are opaque ids, never hashes or
filenames, so the store learns nothing about the content."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol

from familyos.settings import settings


class BlobStore(Protocol):
    async def put(self, key: str, data: bytes) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


class LocalBlobStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("blob key escapes the store root")
        return path

    async def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)

    async def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    async def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3BlobStore:
    def __init__(self, bucket: str, client=None):
        import boto3

        self.bucket = bucket
        self.client = client or boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
        )

    async def put(self, key: str, data: bytes) -> None:
        await asyncio.to_thread(self.client.put_object, Bucket=self.bucket, Key=key, Body=data,
                                ContentType="application/octet-stream")

    async def get(self, key: str) -> bytes:
        obj = await asyncio.to_thread(self.client.get_object, Bucket=self.bucket, Key=key)
        return await asyncio.to_thread(obj["Body"].read)

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self.client.delete_object, Bucket=self.bucket, Key=key)


_store: BlobStore | None = None


def store() -> BlobStore:
    global _store
    if _store is None:
        _store = S3BlobStore(settings.s3_bucket) if settings.blob_backend == "s3" else LocalBlobStore(settings.blob_dir)
    return _store


def use(custom: BlobStore | None) -> None:
    """Swap the store (tests)."""
    global _store
    _store = custom
