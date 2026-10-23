"""Wave A, WF-B1 — credential primary-source verification (PSV).

The register's BLOCKER: StaffCredential was a self-asserted row and
check_privileges treated "row exists + unexpired" as verified, while
StaffPrivilege's docstring claimed the medical staff office had
verified the credentials — the system asserted verification it
never performed.

Proven here:
- the governed `psv` config section is seeded (sources, exclusion
  kinds, re-verification window, demanded exclusion kinds) and a
  legacy published workforce payload gains it via the governed-
  append upgrade path with its other content preserved verbatim;
- PSV events and exclusion checks are recorded through audited,
  capability-gated endpoints validated against the PUBLISHED config
  (unknown source/kind -> 422 naming the code; future verified_on ->
  422; a "verified" outcome without evidence -> 422);
- the computed verification state on _credential_out for every
  state: verified / verification-expired / unverified /
  exclusion-hit;
- the wave gate addition (c): a self-asserted (unverified)
  credential does NOT satisfy check_privileges (the problem names
  it unverified); after a recorded PSV "verified" (+ demanded
  exclusion kinds clear) it DOES; stale PSV names re-verification;
  a "discrepancy" outcome does not satisfy; an exclusion "hit" on
  a required kind names the kind; a missing required exclusion kind
  is a problem; credential expiry itself is still judged exactly as
  before.

Synthetic data only.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import people
from app.configstore import list_config_versions
from app.main import create_app
from app.models import AuditLog, StaffMember
from app.privileging import check_privileges

from .conftest import ADMIN, CLINICIAN, PATIENT

# The server validates verification/exclusion dates against the UTC
# date (the codebase-wide convention; 'verified_on' must not be in
# the future). TODAY must be the UTC date: with a local date this
# whole file fails deterministically whenever local and UTC dates
# differ (e.g. 17:00-24:00 US Pacific, or UTC+ timezones in the
# morning) — found by the Wave A parent gate sweep, 2026-10-21.
# Whether the product should use a facility-local date is a separate
# governed-config question, recorded in the Wave A SQA.
TODAY = datetime.now(timezone.utc).date()
REQUIRED_KINDS = ("licence-board", "oig", "sam")


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        c.app_ref = app
        yield c


def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _physician(client, staff_id, licence_days=365):
    r = client.post("/people/staff", json={
        "staff_id": staff_id, "full_name": f"Dr {staff_id}",
        "job_role": "physician", "unit": "clinic",
        "service_line": "medicine"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    expires = TODAY + timedelta(days=licence_days)
    r = client.post("/people/credentials", json={
        "credential_id": f"cred-{staff_id}", "staff_id": staff_id,
        "type": "medical-licence", "number": f"SYN-{staff_id}",
        "issued_on": (expires - timedelta(days=730)).isoformat(),
        "expires_on": expires.isoformat()}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return f"cred-{staff_id}", expires.isoformat()


def _grant(client, staff_id):
    r = client.post("/teams/privileges", json={
        "staff_id": staff_id, "privilege_code": "attending-of-record"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _verify(client, credential_id, *, days_ago=0, outcome="verified",
            source="state-licence-board", evidence="CONF-1234",
            headers=ADMIN):
    body = {"source_code": source, "method": "direct-source-query",
            "outcome": outcome,
            "verified_on": (TODAY - timedelta(days=days_ago)).isoformat()}
    if evidence is not None:
        body["evidence"] = evidence
    r = client.post(f"/people/credentials/{credential_id}/verifications",
                    json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _exclusion(client, staff_id, kind, outcome="clear", headers=ADMIN,
               **extra):
    r = client.post(f"/people/staff/{staff_id}/exclusion-checks",
                    json={"kind": kind, "outcome": outcome,
                          "checked_on": TODAY.isoformat(),
                          "evidence": f"EV-{kind}", **extra},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _clear_required_exclusions(client, staff_id):
    for kind in REQUIRED_KINDS:
        _exclusion(client, staff_id, kind, "clear")


def _problems(client, staff_id):
    with client.app_ref.state.session_factory() as session:
        staff = session.scalars(select(StaffMember).where(
            StaffMember.staff_id == staff_id)).first()
        assert staff is not None
        return check_privileges(session, staff, ["attending-of-record"])


def _credential_entry(client, credential_id):
    r = client.get("/people/credentials", headers=ADMIN)
    assert r.status_code == 200, r.text
    return next(i for i in r.json()["items"]
                if i["credential_id"] == credential_id)


# ---------------------------------------------------------------------------
# Governed config: seeded psv section + upgrade path for legacy DBs
# ---------------------------------------------------------------------------

def test_psv_config_seeded_with_governed_lists(client):
    cfg = client.get("/people/config", headers=ADMIN).json()
    psv = cfg["psv"]
    source_codes = {s["code"] for s in psv["sources"]}
    assert "state-licence-board" in source_codes
    kind_codes = {k["code"] for k in psv["exclusionKinds"]}
    assert kind_codes == {"licence-board", "npdb", "oig", "sam"}
    assert psv["reverificationDays"] == 730
    assert psv["requireExclusionKinds"] == ["licence-board", "oig", "sam"]


def test_psv_upgrade_path_preserves_legacy_payload(client):
    # Simulate a pre-WF-B1 database: the published workforce payload
    # has NO psv section and carries a site-customised expiry window.
    payload = client.get("/people/config", headers=ADMIN).json()
    payload.pop("psv")
    payload["expiry"] = {"worklistWindowDays": 42, "alertWindowDays": 9}
    _publish(client, "terminology", "workforce", payload)
    assert "psv" not in client.get("/people/config",
                                   headers=ADMIN).json()
    with client.app_ref.state.session_factory() as session:
        people.ensure_people_defaults(session)
    upgraded = client.get("/people/config", headers=ADMIN).json()
    assert upgraded["psv"]["reverificationDays"] == 730
    # Everything else preserved verbatim — the customisation survives.
    assert upgraded["expiry"] == {"worklistWindowDays": 42,
                                   "alertWindowDays": 9}
    # Idempotent: a second ensure publishes no further version.
    with client.app_ref.state.session_factory() as session:
        before = len(list_config_versions(session, "terminology",
                                          "workforce"))
        people.ensure_people_defaults(session)
        after = len(list_config_versions(session, "terminology",
                                         "workforce"))
    assert after == before


# ---------------------------------------------------------------------------
# Endpoints: recording, listing, validation, audit, capability gates
# ---------------------------------------------------------------------------

def test_record_verification_list_and_audit(client):
    credential_id, _ = _physician(client, "staff-w1")
    row = _verify(client, credential_id)
    assert row["credential_id"] == credential_id
    assert row["source_code"] == "state-licence-board"
    assert row["verified_by"] == "dev-admin"  # actor default
    assert row["outcome"] == "verified"
    r = client.get(f"/people/credentials/{credential_id}/verifications",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["evidence"] == "CONF-1234"
    # Capability gates: clinicians read, only workforce.work writes.
    r = client.post(
        f"/people/credentials/{credential_id}/verifications",
        json={"source_code": "state-licence-board",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": TODAY.isoformat(), "evidence": "X"},
        headers=CLINICIAN)
    assert r.status_code == 403
    assert client.get(f"/people/credentials/{credential_id}/verifications",
                      headers=PATIENT).status_code == 403
    with client.app_ref.state.session_factory() as session:
        actions = {row.action for row in
                   session.scalars(select(AuditLog)).all()}
    assert "workforce.credential.verify" in actions


def test_verification_validation_errors(client):
    credential_id, _ = _physician(client, "staff-w2")
    base = {"source_code": "state-licence-board",
            "method": "direct-source-query", "outcome": "verified",
            "verified_on": TODAY.isoformat(), "evidence": "CONF-1"}
    url = f"/people/credentials/{credential_id}/verifications"
    # Unknown credential -> 404.
    assert client.post("/people/credentials/cred-nope/verifications",
                       json=base, headers=ADMIN).status_code == 404
    # Unknown source code -> 422 naming the code.
    r = client.post(url, json={**base, "source_code": "a-friend"},
                    headers=ADMIN)
    assert r.status_code == 422 and "a-friend" in r.json()["detail"]
    # Future verified_on -> 422 naming the field.
    future = (TODAY + timedelta(days=1)).isoformat()
    r = client.post(url, json={**base, "verified_on": future},
                    headers=ADMIN)
    assert r.status_code == 422 and "verified_on" in r.json()["detail"]
    # Malformed date -> 422 naming the field.
    r = client.post(url, json={**base, "verified_on": "not-a-date"},
                    headers=ADMIN)
    assert r.status_code == 422 and "verified_on" in r.json()["detail"]
    # Verified without evidence -> 422 naming the field.
    body = {k: v for k, v in base.items() if k != "evidence"}
    r = client.post(url, json=body, headers=ADMIN)
    assert r.status_code == 422 and "evidence" in r.json()["detail"]
    # Bad outcome / method -> 422.
    r = client.post(url, json={**base, "outcome": "probably-fine"},
                    headers=ADMIN)
    assert r.status_code == 422 and "outcome" in r.json()["detail"]
    r = client.post(url, json={**base, "method": "vibes"}, headers=ADMIN)
    assert r.status_code == 422 and "method" in r.json()["detail"]
    # Evidence is NOT required for a non-verified outcome.
    r = client.post(url, json={**body, "outcome": "unable-to-verify"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text


def test_psv_source_validated_against_published_config(client):
    # Republish the workforce config with a REDUCED source list: a
    # source the default allows is now refused — validation reads
    # the published config, not a hard-coded list.
    credential_id, _ = _physician(client, "staff-w3")
    payload = client.get("/people/config", headers=ADMIN).json()
    payload["psv"]["sources"] = [
        {"code": "only-source", "display": "The only source"}]
    _publish(client, "terminology", "workforce", payload)
    r = client.post(
        f"/people/credentials/{credential_id}/verifications",
        json={"source_code": "state-licence-board",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": TODAY.isoformat(), "evidence": "C-1"},
        headers=ADMIN)
    assert r.status_code == 422
    assert "state-licence-board" in r.json()["detail"]
    r = client.post(
        f"/people/credentials/{credential_id}/verifications",
        json={"source_code": "only-source",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": TODAY.isoformat(), "evidence": "C-1"},
        headers=ADMIN)
    assert r.status_code == 201, r.text


def test_exclusion_check_endpoints_and_validation(client):
    credential_id, _ = _physician(client, "staff-w4")
    row = _exclusion(client, "staff-w4", "oig", "clear",
                     credential_id=credential_id)
    assert row["staff_id"] == "staff-w4" and row["kind"] == "oig"
    r = client.get("/people/staff/staff-w4/exclusion-checks",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["summary"] == {"oig": "clear"}
    url = "/people/staff/staff-w4/exclusion-checks"
    # Unknown staff -> 404; unknown kind -> 422 naming the code.
    assert client.post("/people/staff/staff-nope/exclusion-checks",
                       json={"kind": "oig", "outcome": "clear"},
                       headers=ADMIN).status_code == 404
    r = client.post(url, json={"kind": "fbi-most-wanted",
                               "outcome": "clear"}, headers=ADMIN)
    assert r.status_code == 422 and "fbi-most-wanted" in r.json()["detail"]
    r = client.post(url, json={"kind": "oig", "outcome": "shrug"},
                    headers=ADMIN)
    assert r.status_code == 422 and "outcome" in r.json()["detail"]
    # A credential belonging to someone else -> 422 naming the field.
    other_cred, _ = _physician(client, "staff-w5")
    r = client.post(url, json={"kind": "oig", "outcome": "clear",
                               "credential_id": other_cred},
                    headers=ADMIN)
    assert r.status_code == 422 and "credential_id" in r.json()["detail"]
    # Writes are workforce.work only, and audited.
    assert client.post(url, json={"kind": "oig", "outcome": "clear"},
                       headers=CLINICIAN).status_code == 403
    with client.app_ref.state.session_factory() as session:
        actions = {row.action for row in
                   session.scalars(select(AuditLog)).all()}
    assert "workforce.exclusion.check" in actions


# ---------------------------------------------------------------------------
# _credential_out verification state computation (every state)
# ---------------------------------------------------------------------------

def test_credential_out_states(client):
    # Unverified: a self-asserted credential with no PSV event.
    cred_a, _ = _physician(client, "staff-s1")
    entry = _credential_entry(client, cred_a)
    assert entry["verification"]["state"] == "unverified"
    assert entry["verification"]["exclusions"] == {}

    # Verified: latest PSV verified, inside the window, with the
    # source / verifier / date carried on the block.
    _verify(client, cred_a)
    entry = _credential_entry(client, cred_a)
    block = entry["verification"]
    assert block["state"] == "verified"
    assert block["source"] == "state-licence-board"
    assert block["verified_by"] == "dev-admin"
    assert block["verified_on"] == TODAY.isoformat()

    # Verification-expired: verified, but older than 730 days.
    cred_b, _ = _physician(client, "staff-s2")
    _verify(client, cred_b, days_ago=800)
    assert _credential_entry(client, cred_b)["verification"]["state"] \
        == "verification-expired"

    # Unverified again when the LATEST outcome is a discrepancy.
    cred_c, _ = _physician(client, "staff-s3")
    _verify(client, cred_c, days_ago=10)
    _verify(client, cred_c, days_ago=0, outcome="discrepancy",
            evidence=None)
    assert _credential_entry(client, cred_c)["verification"]["state"] \
        == "unverified"

    # Exclusion-hit wins over a current verified PSV, named distinctly.
    cred_d, _ = _physician(client, "staff-s4")
    _verify(client, cred_d)
    _exclusion(client, "staff-s4", "oig", "hit")
    block = _credential_entry(client, cred_d)["verification"]
    assert block["state"] == "exclusion-hit"
    assert block["exclusions"] == {"oig": "hit"}


# ---------------------------------------------------------------------------
# The gate: check_privileges requires the verified state (wave gate c)
# ---------------------------------------------------------------------------

def test_unverified_credential_does_not_satisfy_check_privileges(client):
    _physician(client, "staff-g1")  # unexpired licence, grant, NO PSV
    _grant(client, "staff-g1")
    problems = _problems(client, "staff-g1")
    assert "no verified medical-licence on file (credential unverified)" \
        in problems
    # The demanded exclusion kinds are also missing.
    for kind in REQUIRED_KINDS:
        assert f"missing exclusion check: {kind}" in problems


def test_verified_credential_satisfies_check_privileges(client):
    credential_id, _ = _physician(client, "staff-g2")
    _grant(client, "staff-g2")
    _verify(client, credential_id)
    _clear_required_exclusions(client, "staff-g2")
    # NPDB is in the governed kind list but NOT demanded by default:
    # no npdb problem appears and the gate passes outright.
    assert _problems(client, "staff-g2") == []


def test_stale_psv_names_reverification(client):
    credential_id, _ = _physician(client, "staff-g3")
    _grant(client, "staff-g3")
    _verify(client, credential_id, days_ago=800)
    _clear_required_exclusions(client, "staff-g3")
    problems = _problems(client, "staff-g3")
    assert "no verified medical-licence on file " \
           "(PSV expired — re-verify)" in problems


def test_discrepancy_outcome_does_not_satisfy(client):
    credential_id, _ = _physician(client, "staff-g4")
    _grant(client, "staff-g4")
    _verify(client, credential_id, days_ago=10)
    _verify(client, credential_id, days_ago=0, outcome="discrepancy",
            evidence=None)
    _clear_required_exclusions(client, "staff-g4")
    problems = _problems(client, "staff-g4")
    assert "no verified medical-licence on file (credential unverified)" \
        in problems


def test_exclusion_hit_on_required_kind_names_kind(client):
    credential_id, _ = _physician(client, "staff-g5")
    _grant(client, "staff-g5")
    _verify(client, credential_id)
    _exclusion(client, "staff-g5", "licence-board", "clear")
    _exclusion(client, "staff-g5", "sam", "clear")
    _exclusion(client, "staff-g5", "oig", "hit")
    problems = _problems(client, "staff-g5")
    assert "no verified medical-licence on file (exclusion hit: oig)" \
        in problems


def test_missing_required_exclusion_kind_is_a_problem(client):
    credential_id, _ = _physician(client, "staff-g6")
    _grant(client, "staff-g6")
    _verify(client, credential_id)
    _exclusion(client, "staff-g6", "licence-board", "clear")
    _exclusion(client, "staff-g6", "oig", "clear")
    # sam never run: the credential verifies, the kind is the problem.
    problems = _problems(client, "staff-g6")
    assert problems == ["missing exclusion check: sam"]


def test_existing_problem_strings_unchanged(client):
    # No credential at all: the pre-WF-B1 string, verbatim.
    r = client.post("/people/staff", json={
        "staff_id": "staff-g7", "full_name": "Dr NoCred",
        "job_role": "physician", "unit": "clinic",
        "service_line": "medicine"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    _grant(client, "staff-g7")
    assert "no medical-licence on file" in _problems(client, "staff-g7")
    # Expired credential: judged exactly as before, expiry named —
    # the verification layer does not mask it.
    _, expires = _physician(client, "staff-g8", licence_days=-5)
    _grant(client, "staff-g8")
    problems = _problems(client, "staff-g8")
    assert f"medical-licence expired on {expires}" in problems
