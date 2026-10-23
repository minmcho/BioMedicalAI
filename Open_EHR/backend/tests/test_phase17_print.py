"""Phase 17, WS-COMP — BD-56 secure printing (hold / release / expire).

Lifecycle: a confidential job is HELD on creation, only its owner
may release it (wrong-owner release refused 403 and audited), an
unreleased job past its configured expiry EXPIRES and can then
never be released, and a non-confidential job is not held.
"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.print_queue import PrintJob
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER


def _job(client, headers=CLINICIAN, **overrides):
    body = {"document_type": "statement", "document_ref": "Patient/pat-1",
            "title": "Patient statement", "confidential": True}
    body.update(overrides)
    r = client.post("/print-queue/jobs", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def _backdate_expiry(client, job_id: str) -> None:
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(PrintJob).where(
            PrintJob.job_id == job_id)).first()
        assert row is not None
        row.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        session.commit()


def test_confidential_job_is_held_with_expiry(client):
    job = _job(client)
    assert job["status"] == "held"
    assert job["owner"] == "dev-clinician"
    assert job["confidential"] is True
    assert job["expires_at"] is not None
    pol = client.get("/print-queue/policy", headers=CLINICIAN)
    assert pol.status_code == 200 and pol.json()["hold_enabled"] is True


def test_non_confidential_job_is_not_held(client):
    job = _job(client, confidential=False)
    assert job["status"] == "ready"
    assert job["expires_at"] is None


def test_wrong_owner_release_refused_and_audited(client):
    job = _job(client)  # owner dev-clinician
    r = client.post(f"/print-queue/jobs/{job['job_id']}/release", headers=CONFIG)
    assert r.status_code == 403, r.text
    r = client.post(f"/print-queue/jobs/{job['job_id']}/release", headers=ADMIN)
    assert r.status_code == 403, r.text
    # Still held afterwards — the refusal changed nothing.
    got = client.get(f"/print-queue/jobs/{job['job_id']}", headers=CLINICIAN)
    assert got.json()["status"] == "held"
    # The refusal is in the hash-chained audit (via Studio audit view).
    audit = client.get("/studio/audit?limit=200", headers=ADMIN)
    assert audit.status_code == 200, audit.text
    entries = audit.json()
    if isinstance(entries, dict):
        entries = entries.get("items", [])
    actions = [e.get("action") for e in entries]
    assert "printqueue.release.refused" in actions


def test_owner_release_happy_path_and_double_release_refused(client):
    job = _job(client)
    r = client.post(f"/print-queue/jobs/{job['job_id']}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "released"
    assert out["released_by"] == "dev-clinician"
    assert out["print_authorised"] is True
    r = client.post(f"/print-queue/jobs/{job['job_id']}/release", headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_unreleased_job_expires_and_cannot_be_released(client):
    job = _job(client)
    _backdate_expiry(client, job["job_id"])
    r = client.post("/print-queue/expire-due", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert job["job_id"] in r.json()["expired"]
    got = client.get(f"/print-queue/jobs/{job['job_id']}", headers=CLINICIAN)
    assert got.json()["status"] == "expired"
    r = client.post(f"/print-queue/jobs/{job['job_id']}/release", headers=CLINICIAN)
    assert r.status_code == 409, r.text


def test_expiry_also_applies_on_read(client):
    job = _job(client)
    _backdate_expiry(client, job["job_id"])
    got = client.get(f"/print-queue/jobs/{job['job_id']}", headers=CLINICIAN)
    assert got.json()["status"] == "expired"


def test_job_listing_is_owner_scoped_for_clinicians(client):
    mine = _job(client)
    other = _job(client, headers=ADMIN, document_ref="Patient/pat-9")
    listing = client.get("/print-queue/jobs", headers=CLINICIAN).json()
    ids = {j["job_id"] for j in listing["items"]}
    assert mine["job_id"] in ids
    assert other["job_id"] not in ids
    assert client.get("/print-queue/jobs", headers=VIEWER).status_code == 200
