"""Phase 16, WS-G1 (BD-47) — EMPI merge governance.

Beyond Phase 14's dedup candidates (which never merge): a governed
merge/unmerge workflow — steward proposes with per-field choices, a
SECOND steward approves, execute requires explicit confirm:true and
retires the source (active:false + replaced-by link) after storing
both snapshots + a cross-reference retaining BOTH patients'
identifiers, and unmerge restores both patients byte-identically.
Every step is audited; there is no auto-merge path.

Synthetic data only.
"""
from sqlalchemy import select

from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

MRN = "urn:opencare:mrn"
CHOICES = {"name": "source", "telecom": "source",
           "birthDate": "survivor", "gender": "survivor"}


def _patient(client, pid, mrn, family="Smith", given="Anna",
             dob="1980-03-04", phone="555-0100", gender="female"):
    body = {
        "resourceType": "Patient", "id": pid, "active": True,
        "identifier": [{"system": MRN, "value": mrn}],
        "name": [{"family": family, "given": [given]}],
        "gender": gender, "birthDate": dob,
        "telecom": [{"system": "phone", "value": phone}],
    }
    r = client.post("/fhir/Patient", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _pair(client, src="empi-src", surv="empi-surv"):
    """Source Anna Smith (MRN-EMPI-001) vs survivor Anne Smyth
    (MRN-EMPI-002): same DOB/gender, differing name spelling + phone."""
    _patient(client, src, "MRN-EMPI-001")
    _patient(client, surv, "MRN-EMPI-002", family="Smyth", given="Anne",
             phone="555-9999")
    return src, surv


def _get_patient(client, pid):
    r = client.get(f"/fhir/Patient/{pid}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _propose(client, src, surv, headers=ADMIN, choices=CHOICES):
    r = client.post("/empi/requests",
                    json={"sourceId": src, "survivorId": surv,
                          "fieldChoices": choices},
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def _audit_actions(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [row.action for row in session.scalars(
            select(AuditLog).order_by(AuditLog.id.asc())).all()]


def _executed_pair(client):
    """A fully executed merge of the standard pair; returns
    (src, surv, request, before_src, before_surv)."""
    src, surv = _pair(client)
    before_src = _get_patient(client, src)
    before_surv = _get_patient(client, surv)
    req = _propose(client, src, surv)
    r = client.post(f"/empi/requests/{req['id']}/approve",
                    json={}, headers=CONFIG)
    assert r.status_code == 200, r.text
    r = client.post(f"/empi/requests/{req['id']}/execute",
                    json={"confirm": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return src, surv, req, before_src, before_surv


# ---------------------------------------------------------------------------
# Full round trip
# ---------------------------------------------------------------------------

def test_full_merge_unmerge_round_trip(client):
    src, surv, req, before_src, before_surv = _executed_pair(client)

    # Survivor carries the chosen fields (name + telecom from source,
    # birthDate + gender kept from survivor).
    after_surv = _get_patient(client, surv)
    assert after_surv["name"] == before_src["name"]
    assert after_surv["telecom"] == before_src["telecom"]
    assert after_surv["birthDate"] == before_surv["birthDate"]
    assert after_surv["gender"] == before_surv["gender"]
    assert after_surv["active"] is True

    # Source is inactive + linked replaced-by the survivor.
    after_src = _get_patient(client, src)
    assert after_src["active"] is False
    assert after_src["link"] == [
        {"other": {"reference": f"Patient/{surv}"}, "type": "replaced-by"}]

    # The cross-reference retains BOTH MRNs and both snapshots, and a
    # merged source's identifier still resolves to the survivor.
    r = client.get("/empi/cross-reference",
                   params={"identifier": "MRN-EMPI-001"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1
    xref = r.json()["items"][0]
    assert xref["merge_id"] == req["id"]
    assert xref["survivor_patient_id"] == surv
    assert {i["value"] for i in xref["source_identifiers"]} == {"MRN-EMPI-001"}
    assert {i["value"] for i in xref["survivor_identifiers"]} == {"MRN-EMPI-002"}
    assert xref["source_snapshot"]["identifier"] == before_src["identifier"]
    assert xref["survivor_snapshot"]["identifier"] == before_surv["identifier"]
    # The survivor's MRN resolves to the same cross-reference too.
    r = client.get("/empi/cross-reference",
                   params={"identifier": "MRN-EMPI-002"}, headers=ADMIN)
    assert r.json()["total"] == 1

    # Unmerge restores BOTH patients byte-identically.
    r = client.post(f"/empi/requests/{req['id']}/unmerge",
                    json={}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "reversed"
    assert _get_patient(client, src) == before_src
    assert _get_patient(client, surv) == before_surv

    # The audit chain contains the four governed actions, in order.
    actions = [a for a in _audit_actions(client)
               if a.startswith("empi.merge.")]
    assert actions == ["empi.merge.propose", "empi.merge.approve",
                       "empi.merge.execute", "empi.merge.unmerge"]


def test_self_approve_and_self_reject_refused(client):
    src, surv = _pair(client)
    req = _propose(client, src, surv)  # proposed by dev-admin
    r = client.post(f"/empi/requests/{req['id']}/approve",
                    json={}, headers=ADMIN)
    assert r.status_code in (403, 409), r.text
    r = client.post(f"/empi/requests/{req['id']}/reject",
                    json={}, headers=ADMIN)
    assert r.status_code in (403, 409), r.text
    # A second steward CAN approve; the request is then executable.
    r = client.post(f"/empi/requests/{req['id']}/approve",
                    json={}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved"
    assert r.json()["approved_by"] == "dev-configurator"
    assert r.json()["proposed_by"] == "dev-admin"


def test_execute_requires_explicit_confirm(client):
    src, surv = _pair(client)
    req = _propose(client, src, surv)
    client.post(f"/empi/requests/{req['id']}/approve",
                json={}, headers=CONFIG)
    for body in ({}, {"confirm": False}, {"confirm": "true"}):
        r = client.post(f"/empi/requests/{req['id']}/execute",
                        json=body, headers=ADMIN)
        assert r.status_code == 422, (body, r.text)
    # Nothing was merged by the refused attempts.
    assert _get_patient(client, src)["active"] is True
    r = client.post(f"/empi/requests/{req['id']}/execute",
                    json={"confirm": True}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["request"]["status"] == "executed"
    assert r.json()["request"]["executed_at"] is not None


def test_execute_without_approval_refused_and_reject_is_terminal(client):
    src, surv = _pair(client)
    req = _propose(client, src, surv)
    # Proposed (not approved): execute is refused even with confirm.
    r = client.post(f"/empi/requests/{req['id']}/execute",
                    json={"confirm": True}, headers=ADMIN)
    assert r.status_code == 409, r.text
    # Second steward rejects; rejected is terminal for execute/approve.
    r = client.post(f"/empi/requests/{req['id']}/reject",
                    json={"note": "not duplicates"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "rejected"
    r = client.post(f"/empi/requests/{req['id']}/execute",
                    json={"confirm": True}, headers=ADMIN)
    assert r.status_code == 409, r.text
    r = client.post(f"/empi/requests/{req['id']}/approve",
                    json={}, headers=CONFIG)
    assert r.status_code == 409, r.text
    assert _get_patient(client, src)["active"] is True
    assert "empi.merge.reject" in _audit_actions(client)


def test_unmerge_only_from_executed(client):
    src, surv = _pair(client)
    req = _propose(client, src, surv)
    r = client.post(f"/empi/requests/{req['id']}/unmerge",
                    json={}, headers=ADMIN)
    assert r.status_code == 409, r.text
    client.post(f"/empi/requests/{req['id']}/approve",
                json={}, headers=CONFIG)
    client.post(f"/empi/requests/{req['id']}/execute",
                json={"confirm": True}, headers=ADMIN)
    r = client.post(f"/empi/requests/{req['id']}/unmerge",
                    json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    # A second unmerge is refused — the reversal already happened.
    r = client.post(f"/empi/requests/{req['id']}/unmerge",
                    json={}, headers=ADMIN)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Validation + capability gates
# ---------------------------------------------------------------------------

def test_propose_validation(client):
    src, surv = _pair(client)
    # Same patient both sides -> 422.
    r = client.post("/empi/requests",
                    json={"sourceId": src, "survivorId": src,
                          "fieldChoices": {}}, headers=ADMIN)
    assert r.status_code == 422, r.text
    # Unknown patient -> 404.
    r = client.post("/empi/requests",
                    json={"sourceId": src, "survivorId": "no-such",
                          "fieldChoices": {}}, headers=ADMIN)
    assert r.status_code == 404, r.text
    # Field choice outside source|survivor -> 422.
    r = client.post("/empi/requests",
                    json={"sourceId": src, "survivorId": surv,
                          "fieldChoices": {"name": "merge"}}, headers=ADMIN)
    assert r.status_code == 422, r.text
    # A second open request for the same pair -> 409.
    _propose(client, src, surv)
    r = client.post("/empi/requests",
                    json={"sourceId": src, "survivorId": surv,
                          "fieldChoices": {}}, headers=ADMIN)
    assert r.status_code == 409, r.text


def test_steward_capability_gate(client):
    src, surv = _pair(client)
    assert client.post("/empi/requests",
                       json={"sourceId": src, "survivorId": surv,
                             "fieldChoices": {}},
                       headers=VIEWER).status_code == 403
    assert client.get("/empi/requests",
                      headers=VIEWER).status_code == 403
    assert client.get("/empi/cross-reference",
                      params={"identifier": "MRN-EMPI-001"},
                      headers=VIEWER).status_code == 403


# ---------------------------------------------------------------------------
# Candidates — Phase 14 dedup outputs + manual pairs, never a merge
# ---------------------------------------------------------------------------

def test_candidates_from_dedup_scan_and_manual_pair(client):
    src, surv = _pair(client)
    # Phase 14 scan flags the pair (same DOB 40 + exact name is NOT
    # met — surnames differ — but DOB + family-only do not apply
    # either; use an exact duplicate for the scan assertion).
    _patient(client, "empi-dup", "MRN-EMPI-003")  # exact twin of src
    r = client.post("/pipelines/mpi-dedup-scan/run", json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/empi/candidates", json={}, headers=ADMIN)
    assert r.status_code == 200, r.text
    pairs = {frozenset((i["patient_a"], i["patient_b"]))
             for i in r.json()["items"]}
    assert frozenset((src, "empi-dup")) in pairs
    assert r.json()["source"] == "dedup-scan"
    # Manual candidate creation for the src/surv pair.
    r = client.post("/empi/candidates",
                    json={"sourceId": src, "survivorId": surv},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["merge_performed"] is False
    assert r.json()["candidate"]["patient_a"] in (src, surv)
    # Candidates never merge: both patients are untouched.
    assert _get_patient(client, src)["active"] is True
    assert _get_patient(client, surv)["active"] is True
    # A request can be proposed straight from the scanned candidate.
    cand = next(i for i in client.post(
        "/empi/candidates", json={}, headers=ADMIN).json()["items"]
        if frozenset((i["patient_a"], i["patient_b"]))
        == frozenset((src, "empi-dup")))
    r = client.post("/empi/requests",
                    json={"candidateId": cand["id"],
                          "sourceId": src, "survivorId": "empi-dup",
                          "fieldChoices": {"name": "survivor"}},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["candidate_id"] == cand["id"]
