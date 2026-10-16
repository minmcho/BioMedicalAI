"""Phase 4A — Billing / claims (app.billing).

Charge capture from a Studio-config fee schedule (unknown codes priced 0
and flagged, never dropped), server-side totals, the config-driven claim
lifecycle, the one-time appeal rule, X12 837P export structure + total
reconciliation, and the billing role model (admin manages, clinician
views, patient sees only their own claims, viewer denied by default).
"""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
CPT = "http://www.ama-assn.org/go/cpt"
LOINC = "http://loinc.org"
SCT = "http://snomed.info/sct"


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Bill"]}],
        "gender": "female", "birthDate": "1980-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _encounter(client, eid="enc-1", pid="pat-1"):
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": "2026-01-10T09:00:00Z", "end": "2026-01-10T10:00:00Z"}},
        headers=ADMIN)
    assert r.status_code == 201, r.text


def _condition(client, cid="cond-1", pid="pat-1", code="44054006"):
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": cid,
        "code": {"coding": [{"system": SCT, "code": code,
                             "display": "Type 2 diabetes mellitus"}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _procedure(client, prid="proc-1", pid="pat-1", code="93005", enc="enc-1"):
    body = {"resourceType": "Procedure", "id": prid, "status": "completed",
            "code": {"coding": [{"system": CPT, "code": code,
                                 "display": "Electrocardiogram"}]},
            "subject": {"reference": f"Patient/{pid}"}}
    if enc:
        body["encounter"] = {"reference": f"Encounter/{enc}"}
    r = client.post("/fhir/Procedure", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text


def _observation(client, oid="obs-1", pid="pat-1", code="2345-7", enc="enc-1"):
    body = {"resourceType": "Observation", "id": oid, "status": "final",
            "code": {"coding": [{"system": LOINC, "code": code,
                                 "display": "Glucose"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "effectiveDateTime": "2026-01-10T09:15:00Z",
            "valueQuantity": {"value": 110, "unit": "mg/dL"}}
    if enc:
        body["encounter"] = {"reference": f"Encounter/{enc}"}
    r = client.post("/fhir/Observation", json=body, headers=ADMIN)
    assert r.status_code == 201, r.text


def _chart(client, pid="pat-1", eid="enc-1"):
    _patient(client, pid)
    _encounter(client, eid, pid)
    _condition(client, f"cond-{pid}", pid)
    _procedure(client, f"proc-{pid}", pid, enc=eid)
    _observation(client, f"obs-{pid}", pid, enc=eid)


def _generate(client, eid="enc-1", headers=ADMIN):
    return client.post("/billing/claims/generate",
                       json={"encounter_id": eid}, headers=headers)


def _line(claim, code):
    for item in claim["item"]:
        coding = item["productOrService"]["coding"][0]
        if coding.get("code") == code:
            return item
    raise AssertionError(f"no line for code {code} in {claim['item']}")


def _priced_flag(item):
    for ext in item["extension"]:
        if ext["url"].endswith("/priced"):
            return ext["valueBoolean"]
    raise AssertionError("line has no priced extension")


# ---------------------------------------------------------------------------
# Charge capture + fee schedule
# ---------------------------------------------------------------------------

def test_generate_claim_prices_lines_and_totals_server_side(client):
    _chart(client)
    r = _generate(client)
    assert r.status_code == 201, r.text
    claim = r.json()
    assert claim["resourceType"] == "Claim" and claim["status"] == "draft"
    assert claim["patient"]["reference"] == "Patient/pat-1"
    # Fee schedule prices: diabetes evaluation 55 + ECG 45 + glucose 12.
    assert _line(claim, "44054006")["unitPrice"]["value"] == 55.00
    assert _line(claim, "93005")["unitPrice"]["value"] == 45.00
    assert _line(claim, "2345-7")["unitPrice"]["value"] == 12.00
    assert all(_priced_flag(item) for item in claim["item"])
    assert claim["total"] == {"value": 112.00, "currency": "USD"}
    assert claim["appealCount"] == 0
    assert claim["statusHistory"][0]["to"] == "draft"
    # Diagnosis section mirrors the problem list.
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0]["code"] == "44054006"
    # The claim is a real stored resource with patient scoping projections.
    got = client.get(f"/fhir/Claim/{claim['id']}", headers=ADMIN)
    assert got.status_code == 200 and got.json()["total"]["value"] == 112.00


def test_unpriced_code_is_flagged_and_priced_zero_never_dropped(client):
    _chart(client)
    _observation(client, "obs-mystery", code="9999-9")  # not in the fee schedule
    r = _generate(client)
    assert r.status_code == 201, r.text
    claim = r.json()
    mystery = _line(claim, "9999-9")
    assert mystery["unitPrice"]["value"] == 0.0
    assert mystery["net"]["value"] == 0.0
    assert _priced_flag(mystery) is False
    assert claim["total"]["value"] == 112.00  # priced lines only


def test_unlinked_resources_within_encounter_period_are_attributed(client):
    _patient(client)
    _encounter(client)
    _condition(client)
    _procedure(client, enc=None)  # no encounter ref, but performed in-period
    client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": "proc-in", "status": "completed",
        "code": {"coding": [{"system": CPT, "code": "93005", "display": "ECG"}]},
        "subject": {"reference": "Patient/pat-1"},
        "performedDateTime": "2026-01-10T09:30:00Z"}, headers=ADMIN)
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": "proc-out", "status": "completed",
        "code": {"coding": [{"system": CPT, "code": "71046", "display": "CXR"}]},
        "subject": {"reference": "Patient/pat-1"},
        "performedDateTime": "2026-03-01T09:30:00Z"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    claim = _generate(client).json()
    codes = [i["productOrService"]["coding"][0]["code"] for i in claim["item"]]
    assert "93005" in codes          # in-period, attributed
    assert "71046" not in codes      # out-of-period, not attributed


def test_generate_unknown_encounter_404_and_no_billable_items_422(client):
    assert _generate(client, "enc-nope").status_code == 404
    _patient(client, "pat-lonely")
    _encounter(client, "enc-lonely", "pat-lonely")
    r = _generate(client, "enc-lonely")
    assert r.status_code == 422 and "No billable items" in r.json()["detail"]


def test_fee_schedule_is_studio_config_and_drives_pricing(client):
    _chart(client)
    eff = client.get("/studio/config/terminology/effective/fee-schedule",
                     headers=ADMIN)
    assert eff.status_code == 200
    payload = eff.json()["payload"]
    for concept in payload["concepts"]:
        if concept["code"] == "93005":
            concept["unitPrice"] = 50.00
    r = client.post("/studio/config/terminology/fee-schedule", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/fee-schedule/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    claim = _generate(client).json()
    assert _line(claim, "93005")["unitPrice"]["value"] == 50.00
    assert claim["total"]["value"] == 117.00


# ---------------------------------------------------------------------------
# Lifecycle (config-driven) + appeal
# ---------------------------------------------------------------------------

def _to(client, claim_id, target, headers=ADMIN, **extra):
    return client.post(f"/billing/claims/{claim_id}/transition",
                       json={"to_status": target, **extra}, headers=headers)


def test_claim_lifecycle_legal_and_illegal_transitions(client):
    _chart(client)
    claim = _generate(client).json()
    cid = claim["id"]
    assert _to(client, cid, "submitted").status_code == 409   # draft -/-> submitted
    assert _to(client, cid, "bogus").status_code == 422       # unknown state
    r = _to(client, cid, "ready")
    assert r.status_code == 200 and r.json()["status"] == "ready"
    assert _to(client, cid, "paid").status_code == 409        # ready -/-> paid
    assert _to(client, cid, "submitted").status_code == 200
    assert _to(client, cid, "appealed").status_code == 409    # appeal only from denied
    r = _to(client, cid, "paid")
    assert r.status_code == 200 and r.json()["status"] == "paid"
    assert _to(client, cid, "denied").status_code == 409      # paid is terminal
    history = r.json()["statusHistory"]
    assert [h["to"] for h in history] == ["draft", "ready", "submitted", "paid"]


def test_appeal_requires_note_and_may_happen_once(client):
    _chart(client)
    cid = _generate(client).json()["id"]
    _to(client, cid, "ready")
    _to(client, cid, "submitted")
    assert _to(client, cid, "denied").status_code == 200
    # No note -> 422, claim stays denied.
    r = _to(client, cid, "appealed")
    assert r.status_code == 422 and "note" in r.json()["detail"]
    r = _to(client, cid, "appealed", note="Medical records attached; ECG was medically necessary.")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "appealed" and r.json()["appealCount"] == 1
    appeal_entry = [h for h in r.json()["statusHistory"] if h["to"] == "appealed"][0]
    assert "ECG was medically necessary" in appeal_entry["note"]
    # Resubmit, denied again — a second appeal is refused, history preserved.
    assert _to(client, cid, "submitted").status_code == 200
    assert _to(client, cid, "denied").status_code == 200
    r = _to(client, cid, "appealed", note="Second appeal attempt.")
    assert r.status_code == 409 and "already been appealed" in r.json()["detail"]
    got = client.get(f"/billing/claims/{cid}", headers=ADMIN).json()
    assert got["appealCount"] == 1 and got["status"] == "denied"
    assert len([h for h in got["statusHistory"] if h["to"] == "appealed"]) == 1


def test_claim_workflow_config_drives_the_lifecycle(client):
    _chart(client)
    cid = _generate(client).json()["id"]
    eff = client.get("/studio/config/workflow/effective/claim-lifecycle",
                     headers=ADMIN).json()
    payload = eff["payload"]
    payload["transitions"].append({"from": "draft", "to": "submitted"})
    r = client.post("/studio/config/workflow/claim-lifecycle", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert client.post(
        f"/studio/config/workflow/claim-lifecycle/versions/{version}/publish",
        headers=ADMIN).status_code == 200
    # The config change makes draft -> submitted legal.
    assert _to(client, cid, "submitted").status_code == 200


# ---------------------------------------------------------------------------
# X12 837P export
# ---------------------------------------------------------------------------

def _segments(text):
    return [s for s in text.replace("\n", "").split("~") if s.strip()]


def test_837_export_structure_and_total_reconciliation(client):
    _chart(client)
    _observation(client, "obs-mystery", code="9999-9")
    cid = _generate(client).json()["id"]
    # A draft cannot be exported.
    r = client.get(f"/billing/claims/{cid}/export/837", headers=ADMIN)
    assert r.status_code == 409 and "submitted" in r.json()["detail"]
    _to(client, cid, "ready")
    _to(client, cid, "submitted")
    r = client.get(f"/billing/claims/{cid}/export/837", headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    segments = _segments(r.text)
    assert segments[0].startswith("ISA*")
    assert segments[1].startswith("GS*HC*")
    assert segments[2].startswith("ST*837*0001")
    assert segments[-2].startswith("GE*")
    assert segments[-1].startswith("IEA*")
    # SE segment count reconciles with the actual ST..SE segment count.
    st_index = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
    se_index = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
    se_count = int(segments[se_index].split("*")[1])
    assert se_count == se_index - st_index + 1
    # CLM total == sum of SV1 amounts == the claim total (incl. the $0 line).
    clm = next(s for s in segments if s.startswith("CLM*"))
    clm_total = float(clm.split("*")[2])
    sv1_amounts = [float(s.split("*")[2]) for s in segments if s.startswith("SV1*")]
    assert len(sv1_amounts) == 4
    assert abs(sum(sv1_amounts) - clm_total) < 0.001
    assert clm_total == 112.00
    assert any(s.startswith("SV1*HC:9999-9*0.00") for s in segments)
    assert any(s.startswith("HI*ABK:44054006") for s in segments)
    # ISA/IEA interchange control numbers match.
    isa_ctrl = segments[0].split("*")[13]
    assert segments[-1].split("*")[2] == isa_ctrl


# ---------------------------------------------------------------------------
# Roles: view / manage / patient scoping
# ---------------------------------------------------------------------------

def test_claim_visibility_roles(client):
    _chart(client)
    _chart(client, pid="pat-2", eid="enc-2")
    own = _generate(client, "enc-1").json()
    other = _generate(client, "enc-2").json()
    # Viewer is denied by default (403), unauthenticated 401.
    assert client.get("/billing/claims", headers=VIEWER).status_code == 403
    assert client.get(f"/billing/claims/{own['id']}", headers=VIEWER).status_code == 403
    assert client.get("/billing/claims").status_code == 401
    # Clinician views (billing.view) but cannot manage (billing.manage).
    listed = client.get("/billing/claims", headers=CLINICIAN)
    assert listed.status_code == 200 and listed.json()["total"] == 2
    assert _generate(client, "enc-1", headers=CLINICIAN).status_code == 403
    assert _to(client, own["id"], "ready", headers=CLINICIAN).status_code == 403
    # Patient sees ONLY their own claims; the amounts are theirs to see.
    mine = client.get("/billing/claims", headers=PATIENT)
    assert mine.status_code == 200
    entries = mine.json()["entry"]
    assert len(entries) == 1
    assert entries[0]["resource"]["id"] == own["id"]
    assert entries[0]["resource"]["total"]["value"] == 112.00
    assert client.get(f"/billing/claims/{own['id']}", headers=PATIENT).status_code == 200
    # Another patient's claim is non-disclosed (404), as elsewhere.
    assert client.get(f"/billing/claims/{other['id']}", headers=PATIENT).status_code == 404
    scoped = client.get("/billing/claims", params={"patient": "pat-2"},
                        headers=PATIENT)
    assert scoped.status_code == 403
    # Configurator manages the lifecycle.
    assert _to(client, own["id"], "ready", headers=CONFIG).status_code == 200
    # Status filter works for staff.
    ready = client.get("/billing/claims", params={"status": "ready"}, headers=ADMIN)
    assert ready.json()["total"] == 1


def test_viewer_can_be_granted_billing_view_via_permissions_config(client):
    _chart(client)
    _generate(client)
    assert client.get("/billing/claims", headers=VIEWER).status_code == 403
    payload = {"capabilities": {"billing.view": {"viewer": True}}}
    r = client.post("/studio/config/permissions/role-matrix", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    assert client.post(
        f"/studio/config/permissions/role-matrix/versions/{version}/publish",
        headers=ADMIN).status_code == 200
    assert client.get("/billing/claims", headers=VIEWER).status_code == 200
    # ...but manage is still denied.
    assert _generate(client, "enc-1", headers=VIEWER).status_code == 403


def test_billing_writes_are_audited_and_chain_verifies(client):
    _chart(client)
    cid = _generate(client).json()["id"]
    _to(client, cid, "ready")
    _to(client, cid, "submitted")
    client.get(f"/billing/claims/{cid}/export/837", headers=ADMIN)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(select(AuditLog)).all()]
        assert "billing.claim.generate" in actions
        assert actions.count("billing.claim.transition") == 2
        assert "billing.claim.export837" in actions
        assert verify_chain(session) is True
