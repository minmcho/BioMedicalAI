"""Platform Pass, Workstream 5 — hot/cold tiering of the raw-zone lake.

Tiering is governed by a published Studio config (kind ``integration``,
key ``tiering``) and NEVER runs while disabled. When enabled, entries
older than their (possibly per-class) hot window have their payload
bytes moved to a filesystem cold store behind an S3-shaped interface —
content-addressed by SHA-256 — a ``tiered-cold`` lineage link is
appended, ``payload_text`` is emptied with ``content_hash`` preserved
as the verification anchor, and retrieval re-verifies the hash before
serving a single byte. Synthetic data only.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import lake, tiering
from app.main import create_app
from app.models import AuditLog, LakeLineageLink, LakeRawEntry

from .conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

NOW = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cold_dir = tmp_path / "cold-store"
    cold_dir.mkdir()
    monkeypatch.setenv("OPENCARE_COLD_DIR", str(cold_dir))
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(tiering.router)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, app=app, cold_dir=cold_dir,
                              tmp_path=tmp_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _publish(client, payload):
    r = client.post("/studio/config/integration/tiering", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/integration/tiering/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _seed(client, *, payload_type="hl7", payload_text="payload",
          age_days=0):
    """One raw entry via lake.record_raw, backdated by age_days."""
    factory = client.app.state.session_factory
    with factory() as session:
        row = lake.record_raw(session, payload_type=payload_type,
                              payload_text=payload_text, source="test")
        entry_id = row.id
        content_hash = row.content_hash
        # received_at is column-defaulted at flush; assign after flush.
        row.received_at = NOW - timedelta(days=age_days)
        session.commit()
    return entry_id, content_hash


def _row(client, entry_id):
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(LakeRawEntry).where(
            LakeRawEntry.id == entry_id)).first()
        assert row is not None
        return SimpleNamespace(
            id=row.id, payload_type=row.payload_type,
            payload_text=row.payload_text, content_hash=row.content_hash,
            received_at=row.received_at, disposition=row.disposition)


def _links(client, entry_id):
    factory = client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(LakeLineageLink).where(
            LakeLineageLink.raw_entry_id == entry_id)).all()
        return [{"curated_ref": r.curated_ref, "link_kind": r.link_kind}
                for r in rows]


def _cold_path(cold_dir, content_hash):
    return cold_dir / content_hash[:2] / content_hash[2:4] / content_hash


def _run_job(client, **kwargs):
    factory = client.app.state.session_factory
    with factory() as session:
        result = tiering.run_tiering(session, now=NOW, **kwargs)
        session.commit()
        return result


# ---------------------------------------------------------------------------
# Policy: disabled by default, job is a no-op, endpoint refuses 409
# ---------------------------------------------------------------------------

def test_default_policy_is_disabled(env):
    assert tiering.default_tiering_payload() == {
        "enabled": False, "hot_window_days": 90, "classes": []}
    factory = env.client.app.state.session_factory
    with factory() as session:
        assert tiering.tiering_policy(session)["enabled"] is False


def test_disabled_job_is_noop_and_endpoint_409(env):
    entry_id, content_hash = _seed(
        env.client, payload_text="old but policy off", age_days=365)
    # Direct job call: exact disabled shape, nothing moves.
    result = _run_job(env.client)
    assert result == {"ran": False, "reason": "disabled"}
    row = _row(env.client, entry_id)
    assert row.payload_text == "old but policy off"
    assert row.content_hash == content_hash
    assert _links(env.client, entry_id) == []
    # Operator endpoint refuses with 409 while disabled.
    r = env.client.post("/tiering/run", headers=ADMIN)
    assert r.status_code == 409, r.text
    assert _row(env.client, entry_id).payload_text == "old but policy off"


# ---------------------------------------------------------------------------
# The move itself
# ---------------------------------------------------------------------------

def test_old_entry_moves_young_entry_untouched(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 90,
                          "classes": []})
    young_id, _ = _seed(env.client, payload_text="young-payload",
                        age_days=10)
    old_text = "MSH|^~\\&|OLD|payload-that-is-old"
    old_id, old_hash = _seed(env.client, payload_text=old_text,
                             age_days=200)
    r = env.client.post("/tiering/run", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["moved"] == 1
    assert body["skipped_young"] == 1
    assert body["skipped_already"] == 0
    assert body["bytes_moved"] == len(old_text.encode("utf-8"))
    # Young entry fully untouched.
    young = _row(env.client, young_id)
    assert young.payload_text == "young-payload"
    assert _links(env.client, young_id) == []
    # Old entry: payload emptied, hash + disposition + timestamp intact.
    old = _row(env.client, old_id)
    assert old.payload_text == ""
    assert old.content_hash == old_hash
    assert old.disposition == "received"
    assert old.received_at is not None
    # Lineage link is the tiering record, with a cold:// ref.
    links = _links(env.client, old_id)
    assert len(links) == 1
    assert links[0]["link_kind"] == "tiered-cold"
    assert links[0]["curated_ref"] == f"cold://{old_hash}"
    # Cold object bytes are the original payload, byte-exact, and the
    # sidecar carries the tiering metadata.
    obj = _cold_path(env.cold_dir, old_hash)
    assert obj.is_file()
    assert obj.read_bytes() == old_text.encode("utf-8")
    import json
    sidecar = json.loads(
        (obj.parent / (obj.name + ".meta.json")).read_text())
    assert sidecar["raw_entry_id"] == old_id
    assert sidecar["payload_type"] == "hl7"
    assert sidecar["content_hash"] == old_hash
    assert sidecar["original_bytes"] == len(old_text.encode("utf-8"))
    assert sidecar["tiered_at"]


def test_retrieval_returns_exact_payload_verified(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    text = "retrieval-payload-✓-12345"
    entry_id, content_hash = _seed(env.client, payload_text=text,
                                   age_days=60)
    assert env.client.post("/tiering/run", headers=ADMIN).status_code == 200
    r = env.client.get(f"/tiering/cold/{entry_id}", headers=VIEWER)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == entry_id
    assert body["content_hash"] == content_hash
    assert body["payload_text"] == text
    assert body["verified"] is True


def test_retrieval_404_for_untiered_and_missing(env):
    entry_id, _ = _seed(env.client, payload_text="still hot", age_days=1)
    assert env.client.get(f"/tiering/cold/{entry_id}",
                          headers=ADMIN).status_code == 404
    assert env.client.get("/tiering/cold/999999",
                          headers=ADMIN).status_code == 404


def test_tampered_cold_object_refused_500(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    entry_id, content_hash = _seed(env.client, payload_text="integrity!",
                                   age_days=90)
    assert env.client.post("/tiering/run", headers=ADMIN).status_code == 200
    obj = _cold_path(env.cold_dir, content_hash)
    obj.write_bytes(b"tampered-bytes")
    r = env.client.get(f"/tiering/cold/{entry_id}", headers=ADMIN)
    assert r.status_code == 500, r.text
    assert "ntegrity" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Class overrides
# ---------------------------------------------------------------------------

def test_class_override_moves_globally_young_entry(env):
    # Global window 90d; the hl7 class overrides to 10d. A 30-day-old
    # hl7 entry is young globally but OLD under its class -> moves.
    # A 30-day-old device-reading entry has no class -> stays hot.
    _publish(env.client, {
        "enabled": True, "hot_window_days": 90,
        "classes": [{"payload_type": "hl7", "hot_window_days": 10}]})
    hl7_id, _ = _seed(env.client, payload_type="hl7",
                      payload_text="hl7-30d", age_days=30)
    dev_id, _ = _seed(env.client, payload_type="device-reading",
                      payload_text="reading-30d", age_days=30)
    result = _run_job(env.client)
    assert result["moved"] == 1
    assert result["skipped_young"] == 1
    assert _row(env.client, hl7_id).payload_text == ""
    assert _row(env.client, dev_id).payload_text == "reading-30d"


def test_class_override_keeps_globally_old_entry_hot(env):
    # Vice versa: global window 10d; the hl7 class overrides to 365d.
    # A 30-day-old hl7 entry is old globally but YOUNG under its
    # class -> stays hot; a 30-day-old reading (no class) moves.
    _publish(env.client, {
        "enabled": True, "hot_window_days": 10,
        "classes": [{"payload_type": "hl7", "hot_window_days": 365}]})
    hl7_id, _ = _seed(env.client, payload_type="hl7",
                      payload_text="hl7-30d", age_days=30)
    dev_id, _ = _seed(env.client, payload_type="device-reading",
                      payload_text="reading-30d", age_days=30)
    result = _run_job(env.client)
    assert result["moved"] == 1
    assert result["skipped_young"] == 1
    assert _row(env.client, hl7_id).payload_text == "hl7-30d"
    assert _row(env.client, dev_id).payload_text == ""


def test_wildcard_class_window_applies(env):
    _publish(env.client, {
        "enabled": True, "hot_window_days": 90,
        "classes": [{"payload_type": "*", "hot_window_days": 5}]})
    entry_id, _ = _seed(env.client, payload_type="device-reading",
                        payload_text="wildcard-20d", age_days=20)
    result = _run_job(env.client)
    assert result["moved"] == 1
    assert _row(env.client, entry_id).payload_text == ""


# ---------------------------------------------------------------------------
# Idempotency, audit, status
# ---------------------------------------------------------------------------

def test_second_run_is_idempotent(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    entry_id, content_hash = _seed(env.client, payload_text="move-me",
                                   age_days=100)
    first = env.client.post("/tiering/run", headers=ADMIN)
    assert first.status_code == 200 and first.json()["moved"] == 1
    second = env.client.post("/tiering/run", headers=ADMIN)
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["moved"] == 0
    assert body["bytes_moved"] == 0
    assert body["skipped_already"] == 1
    # Still exactly one lineage link, and retrieval still verifies.
    assert len(_links(env.client, entry_id)) == 1
    r = env.client.get(f"/tiering/cold/{entry_id}", headers=ADMIN)
    assert r.status_code == 200 and r.json()["payload_text"] == "move-me"


def test_audit_rows_written_for_moves(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    entry_id, content_hash = _seed(env.client, payload_text="audited",
                                   age_days=100)
    assert env.client.post("/tiering/run", headers=ADMIN).status_code == 200
    factory = env.client.app.state.session_factory
    with factory() as session:
        rows = session.scalars(select(AuditLog).where(
            AuditLog.action == "lake.tiering.moved")).all()
        assert len(rows) == 1
        assert rows[0].entity == f"lake-raw:{entry_id}"
        assert rows[0].payload["content_hash"] == content_hash
        assert rows[0].payload["bytes"] == len(b"audited")
        assert rows[0].payload["cold_ref"] == f"cold://{content_hash}"


def test_status_counts_hot_cold_and_bytes(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    _seed(env.client, payload_text="hot-one", age_days=1)
    _seed(env.client, payload_text="cold-one-12345", age_days=100)
    before = env.client.get("/tiering/status", headers=VIEWER)
    assert before.status_code == 200, before.text
    body = before.json()
    assert body["policy"]["enabled"] is True
    assert body["policy"]["hot_window_days"] == 30
    assert body["counts"] == {"hot_rows": 2, "cold_rows": 0,
                              "cold_bytes": 0}
    assert env.client.post("/tiering/run", headers=ADMIN).status_code == 200
    after = env.client.get("/tiering/status", headers=VIEWER).json()
    assert after["counts"]["hot_rows"] == 1
    assert after["counts"]["cold_rows"] == 1
    assert after["counts"]["cold_bytes"] == len(b"cold-one-12345")


# ---------------------------------------------------------------------------
# Role gates
# ---------------------------------------------------------------------------

def test_role_gates(env):
    _publish(env.client, {"enabled": True, "hot_window_days": 30,
                          "classes": []})
    # pipelines.run is admin/configurator only.
    assert env.client.post("/tiering/run",
                           headers=VIEWER).status_code == 403
    assert env.client.post("/tiering/run",
                           headers=CLINICIAN).status_code == 403
    # The configurator (config-token) holds pipelines.run and may run.
    assert env.client.post("/tiering/run",
                           headers=CONFIG).status_code == 200
    # devices.read includes viewer: status + retrieval surface readable.
    assert env.client.get("/tiering/status",
                          headers=VIEWER).status_code == 200
    entry_id, _ = _seed(env.client, payload_text="gate-check", age_days=1)
    # Unauthenticated / patient-role callers get no read either.
    assert env.client.get("/tiering/status").status_code in (401, 403)
    assert env.client.get(f"/tiering/cold/{entry_id}").status_code in (401, 403)
