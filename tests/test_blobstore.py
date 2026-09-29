import boto3
from moto import mock_aws

from familyos.blobstore import LocalBlobStore, S3BlobStore


async def test_s3_store_round_trip():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="originals")
        store = S3BlobStore("originals", client=client)
        await store.put("households/h/blobs/b", b"sealed")
        assert await store.get("households/h/blobs/b") == b"sealed"
        await store.delete("households/h/blobs/b")
        assert client.list_objects_v2(Bucket="originals").get("KeyCount") == 0


async def test_local_store_refuses_keys_outside_its_root(tmp_path):
    store = LocalBlobStore(tmp_path)
    try:
        await store.put("../escape", b"x")
    except ValueError:
        return
    raise AssertionError("expected ValueError")
