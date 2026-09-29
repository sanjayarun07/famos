import base64
import os
import tempfile

# Configure before familyos.settings is imported.
os.environ.setdefault("FAMILYOS_DATABASE_URL", os.environ.get("TEST_DATABASE_URL", "postgresql://postgres@localhost:5433/familyos_test"))
os.environ["FAMILYOS_MASTER_KEY"] = base64.b64encode(b"k" * 32).decode()
os.environ["FAMILYOS_BLOB_DIR"] = tempfile.mkdtemp(prefix="familyos-blobs-")
os.environ["FAMILYOS_BLOB_BACKEND"] = "local"
os.environ["FAMILYOS_INBOUND_WEBHOOK_SECRET"] = "test-secret"
os.environ["FAMILYOS_INBOUND_DOMAIN"] = "in.familyos.test"
os.environ["FAMILYOS_JOBS_ENABLED"] = "false"

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402

from familyos import blobstore, db, jobs  # noqa: E402
from familyos.main import create_app, register_job_handlers  # noqa: E402
from familyos.settings import settings  # noqa: E402


@pytest.fixture(scope="session")
async def database():
    conn = await asyncpg.connect(settings.database_url)
    await conn.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    await conn.close()
    pool = await db.connect()
    await db.migrate()
    register_job_handlers()
    yield pool
    await db.close()


@pytest.fixture(autouse=True)
async def clean(database):
    tables = await database.fetch(
        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename <> 'schema_migrations'")
    await database.execute("TRUNCATE " + ", ".join(t["tablename"] for t in tables) + " CASCADE")
    blobstore.use(blobstore.LocalBlobStore(settings.blob_dir))
    jobs.reset_for_test()
    yield


@pytest.fixture
async def client(database):
    app = create_app(with_lifespan=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


class Family:
    """A household with two guardians, a grandparent and two children."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def setup(self, name: str = "The Arun family", guardian_email: str = "amma@example.com"):
        r = await self.client.post("/v1/households", json={
            "name": name, "guardian": {"display_name": "Amma", "email": guardian_email}})
        assert r.status_code == 201, r.text
        body = r.json()
        self.household = body["household"]
        self.amma = body["credentials"]["member"]
        self.amma_token = body["credentials"]["token"]
        self.appa, self.appa_token = await self._add("Appa", "guardian", "appa@example.com")
        self.paati, self.paati_token = await self._add("Paati", "adult", "paati@example.com")
        self.older, _ = await self._add("Older one", "child", None)
        self.younger, _ = await self._add("Younger one", "child", None)
        return self

    async def _add(self, name, role, email):
        r = await self.client.post("/v1/household/members", headers=self.h("amma"),
                                   json={"display_name": name, "role": role, "email": email})
        assert r.status_code == 201, r.text
        body = r.json()
        return (body["member"], body["token"]) if "token" in body else (body, None)

    def h(self, who: str) -> dict:
        return {"Authorization": f"Bearer {getattr(self, who + '_token')}"}

    @property
    def inbound(self) -> str:
        return self.household["inbound_address"]

    async def upload(self, who: str, data: bytes, filename="notice.pdf", visibility="private", subjects=(), **kw):
        return await self.client.post("/v1/artifacts", headers=self.h(who),
                                      files={"file": (filename, data, kw.get("content_type", "application/pdf"))},
                                      data={"visibility": visibility, "subject_member_ids": [s["id"] for s in subjects]})

    async def consent(self, child, who="amma"):
        r = await self.client.post("/v1/consents", headers=self.h(who), json={
            "subject_member_id": child["id"], "notice_version": "2026-09", "verification_method": "account_holder"})
        assert r.status_code == 201, r.text
        return r.json()


@pytest.fixture
async def family(client):
    return await Family(client).setup()


PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def pdf(tag: str) -> bytes:
    return PDF + tag.encode()
