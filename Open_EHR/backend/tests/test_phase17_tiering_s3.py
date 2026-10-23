"""Phase 17 (BD-63) — real S3 cold tier for the raw-zone lake.

Two layers:

- UNGATED contract tests: :class:`app.tiering.S3ColdStore` against a
  faithful in-memory stand-in for the boto3 S3 client (same
  put/get/head + ClientError shapes boto3 raises), plus the
  published-config backend selection and a full tiering lifecycle
  (move -> hash-verified retrieve-by-id -> tamper refused) run
  against the S3 store through the tiering job + endpoint.
- LIVE drill (env-gated, the live-DB pattern): the same lifecycle
  against a REAL S3 endpoint (MinIO in this environment). Skips
  unless OPENCARE_TEST_S3_ENDPOINT is set; credentials arrive only
  via AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY.
"""
import hashlib
import io
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import lake, tiering
from app.main import create_app
from app.models import LakeRawEntry

from .conftest import ADMIN, VIEWER

NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# In-memory boto3 S3 client stand-in (boto3's real shapes)
# ---------------------------------------------------------------------------

class FakeS3Client:
    def __init__(self):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.buckets: set[str] = set()

    def create_bucket(self, Bucket):
        self.buckets.add(Bucket)
        return {}

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")
        return {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.buckets.add(Bucket)
        self.objects[(Bucket, Key)] = bytes(Body)
        return {}

    def get_object(self, Bucket, Key):
        try:
            data = self.objects[(Bucket, Key)]
        except KeyError:
            raise ClientError({"Error": {"Code": "NoSuchKey"}},
                              "GetObject")
        return {"Body": io.BytesIO(data)}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {}


def _s3_store(fake=None):
    fake = fake or FakeS3Client()
    return tiering.S3ColdStore(bucket="opencare-cold", client=fake), fake


# ---------------------------------------------------------------------------
# S3ColdStore contract (ungated)
# ---------------------------------------------------------------------------

def test_s3_store_put_get_exists_roundtrip():
    store, fake = _s3_store()
    payload = b"MSH|^~\\&|S3|payload-\xe2\x9c\x93"
    key = hashlib.sha256(payload).hexdigest()
    ref = store.put(key, payload, metadata={"raw_entry_id": 7,
                                            "payload_type": "hl7"})
    assert ref == f"cold://{key}"
    assert store.exists(ref) is True
    assert store.exists(f"cold://{'0' * 64}") is False
    assert store.get(ref) == payload
    # The object landed under the content-addressed key and the
    # metadata sidecar is a sibling object, like the filesystem store.
    assert fake.objects[("opencare-cold", key)] == payload
    meta = store.read_metadata(ref)
    assert meta["raw_entry_id"] == 7
    assert meta["content_hash"] == key
    assert meta["original_bytes"] == len(payload)


def test_s3_store_missing_object_raises_oserror():
    store, _ = _s3_store()
    with pytest.raises(OSError):
        store.get(f"cold://{'a' * 64}")


def test_s3_store_satisfies_coldstore_protocol():
    store, _ = _s3_store()
    assert isinstance(store, tiering.ColdStore)


# ---------------------------------------------------------------------------
# Published-config backend selection
# ---------------------------------------------------------------------------

@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENCARE_COLD_DIR", str(tmp_path / "cold"))
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, tmp_path=tmp_path)


def _publish_tiering(client, payload):
    r = client.post("/studio/config/integration/tiering", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/tiering/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _seed_old(client, text, age_days=200):
    factory = client.app.state.session_factory
    with factory() as session:
        row = lake.record_raw(session, payload_type="hl7",
                              payload_text=text, source="test")
        entry_id, content_hash = row.id, row.content_hash
        row.received_at = NOW - timedelta(days=age_days)
        session.commit()
    return entry_id, content_hash


def test_config_selects_filesystem_by_default(env):
    factory = env.client.app.state.session_factory
    with factory() as session:
        store = tiering.cold_store_from_config(session)
    assert isinstance(store, tiering.FilesystemColdStore)


def test_config_selects_s3_when_published(env, monkeypatch):
    _publish_tiering(env.client, {
        "enabled": True, "hot_window_days": 90, "classes": [],
        "backend": "s3",
        "s3": {"bucket": "opencare-cold",
               "endpoint_url": "http://127.0.0.1:9000",
               "region": "us-east-1"}})
    factory = env.client.app.state.session_factory
    with factory() as session:
        policy = tiering.tiering_policy(session)
        assert policy["backend"] == "s3"
        assert policy["s3"]["bucket"] == "opencare-cold"
        store = tiering.cold_store_from_config(session)
    assert isinstance(store, tiering.S3ColdStore)
    assert store.bucket == "opencare-cold"


def test_s3_lifecycle_move_retrieve_tamper(env, monkeypatch):
    """Full drill through the job + retrieval endpoint with the S3
    store selected (fake boto3 client): move, hash-verified retrieve,
    tamper refused 500."""
    store, fake = _s3_store()
    monkeypatch.setattr(tiering, "cold_store_from_config",
                        lambda session: store)
    _publish_tiering(env.client, {"enabled": True, "hot_window_days": 30,
                                  "classes": []})
    text = "s3-lifecycle-payload-✓"
    entry_id, content_hash = _seed_old(env.client, text)
    r = env.client.post("/tiering/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 1
    # Bytes are in the (fake) S3 bucket under the content hash.
    assert fake.objects[("opencare-cold", content_hash)] == \
        text.encode("utf-8")
    # The DB row is a stub now.
    factory = env.client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(LakeRawEntry).where(
            LakeRawEntry.id == entry_id)).first()
        assert row.payload_text == ""
        assert row.content_hash == content_hash
    # Retrieve-by-id verifies the hash and serves byte-exact payload.
    r = env.client.get(f"/tiering/cold/{entry_id}", headers=VIEWER)
    assert r.status_code == 200, r.text
    assert r.json()["payload_text"] == text
    assert r.json()["verified"] is True
    # Tamper with the object in the bucket: retrieval refuses (500).
    fake.objects[("opencare-cold", content_hash)] = b"tampered"
    r = env.client.get(f"/tiering/cold/{entry_id}", headers=ADMIN)
    assert r.status_code == 500, r.text


# ---------------------------------------------------------------------------
# LIVE drill against a real S3 endpoint (env-gated)
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    not os.environ.get("OPENCARE_TEST_S3_ENDPOINT"),
    reason="OPENCARE_TEST_S3_ENDPOINT not set — live S3/MinIO drill "
           "not run")
def test_live_s3_drill(tmp_path, monkeypatch):
    endpoint = os.environ["OPENCARE_TEST_S3_ENDPOINT"]
    bucket = os.environ.get("OPENCARE_TEST_S3_BUCKET", "opencare-cold")
    store = tiering.S3ColdStore(bucket=bucket, endpoint_url=endpoint)
    store.ensure_bucket()
    app = create_app(database_url=f"sqlite:///{tmp_path}/live.db")
    with TestClient(app) as client:
        # Publish the S3-backed policy through the real Studio API.
        r = client.post("/studio/config/integration/tiering", json={
            "enabled": True, "hot_window_days": 30, "classes": [],
            "backend": "s3",
            "s3": {"bucket": bucket, "endpoint_url": endpoint}},
            headers=ADMIN)
        assert r.status_code == 201, r.text
        version = r.json()["version"]
        r = client.post(
            f"/studio/config/integration/tiering/versions/{version}"
            "/publish", headers=ADMIN)
        assert r.status_code == 200, r.text
        factory = app.state.session_factory
        with factory() as session:
            assert isinstance(tiering.cold_store_from_config(session),
                              tiering.S3ColdStore)
            row = lake.record_raw(
                session, payload_type="hl7",
                payload_text="live-minio-drill-payload", source="drill")
            entry_id, content_hash = row.id, row.content_hash
            row.received_at = NOW - timedelta(days=120)
            session.commit()
        r = client.post("/tiering/run", headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["moved"] == 1
        # The object is really in MinIO under the content hash.
        assert store.get(f"cold://{content_hash}") == \
            b"live-minio-drill-payload"
        meta = store.read_metadata(f"cold://{content_hash}")
        assert meta["raw_entry_id"] == entry_id
        # Retrieve-by-id through the API, hash-verified.
        r = client.get(f"/tiering/cold/{entry_id}", headers=ADMIN)
        assert r.status_code == 200, r.text
        assert r.json()["payload_text"] == "live-minio-drill-payload"
        assert r.json()["verified"] is True
