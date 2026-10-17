"""Phase 7 — Revenue cycle completion (app.revenue): HIM/coding,
X12 835 remittance posting, denials workqueue + rebill, patient
statements + recorded payments.

Money math is asserted EXACTLY against a synthetic 835 fixture whose
segment counts are computed here (a wrong SE count must 422 and post
nothing). All amounts are synthetic.
"""
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

SCT = "http://snomed.info/sct"
CPT = "http://www.ama-assn.org/go/cpt"
LOINC = "http://loinc.org"


# ---------------------------------------------------------------------------
# Chart / claim helpers (mirroring tests/test_phase4a_billing.py)
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Rev"]}],
        "gender": "female", "birthDate": "1980-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def _chart(client, pid, eid, cond_code, proc_code=None, obs=False):
    _patient(client, pid)
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": "2026-01-10T09:00:00Z",
                   "end": "2026-01-10T10:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/fhir/Condition", json={
        "resourceType": "Condition", "id": f"cond-{eid}",
        "code": {"coding": [{"system": SCT, "code": cond_code,
                             "display": "Synthetic condition"}]},
        "subject": {"reference": f"Patient/{pid}"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    if proc_code:
        r = client.post("/fhir/Procedure", json={
            "resourceType": "Procedure", "id": f"proc-{eid}",
            "status": "completed",
            "code": {"coding": [{"system": CPT, "code": proc_code,
                                 "display": "Synthetic procedure"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"}}, headers=ADMIN)
        assert r.status_code == 201, r.text
    if obs:
        r = client.post("/fhir/Observation", json={
            "resourceType": "Observation", "id": f"obs-{eid}",
            "status": "final",
            "code": {"coding": [{"system": LOINC, "code": "2345-7",
                                 "display": "Glucose"}]},
            "subject": {"reference": f"Patient/{pid}"},
            "encounter": {"reference": f"Encounter/{eid}"},
            "effectiveDateTime": "2026-01-10T09:15:00Z",
            "valueQuantity": {"value": 110, "unit": "mg/dL"}}, headers=ADMIN)
        assert r.status_code == 201, r.text


def _claim(client, eid, submit=True):
    r = client.post("/billing/claims/generate", json={"encounter_id": eid},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    claim = r.json()
    if submit:
        for target in ("ready", "submitted"):
            r = client.post(f"/billing/claims/{claim['id']}/transition",
                            json={"to_status": target}, headers=ADMIN)
            assert r.status_code == 200, r.text
        claim = r.json()
    return claim


def _get_claim(client, claim_id):
    r = client.get(f"/billing/claims/{claim_id}", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 835 fixture builder — SE count computed, never hand-counted
# ---------------------------------------------------------------------------

def _835(clp_blocks, check="CHK-778899", total="302.00"):
    segments = [
        "ST*835*0001",
        "BHT*0019*00*REF123*20261017*1200",
        f"BPR*I*{total}*C*ACH",
        "N1*PR*SYNTHETIC PAYER*****46*SYNTH-PAYER",
        f"TRN*1*{check}*123456789",
    ]
    for block in clp_blocks:
        segments.extend(block)
    segments.append(f"SE*{len(segments) + 1}*0001")
    envelope = [
        "ISA*00*          *00*          *ZZ*OPENCARE       "
        "*ZZ*SYNTHPAYER     *261017*1200*^*00501*000000001*0*T*:",
        "GS*HP*OPENCARE*SYNTHPAYER*20261017*1200*1*X*005010X221A1",
    ]
    return "".join(f"{s}~\n" for s in envelope + segments
                   + ["GE*1*1", "IEA*1*000000001"])


def _clp(claim_id, status, charge, paid, resp, extra=()):
    return [f"CLP*{claim_id}*{status}*{charge}*{paid}*{resp}**MB*R-{claim_id[:6]}",
            *extra]


def _post835(client, text, headers=ADMIN):
    return client.post("/billing/remittances", content=text,
                       headers={**headers, "Content-Type": "text/plain"})


# ---------------------------------------------------------------------------
# HIM / coding
# ---------------------------------------------------------------------------

def test_coding_queue_validation_and_record(client):
    _chart(client, "pat-1", "enc-1", "44054006", proc_code="93005")
    queue = client.get("/billing/coding/queue", headers=CLINICIAN).json()
    assert [i["encounter_id"] for i in queue["items"]] == ["enc-1"]
    assert queue["items"][0]["reason"] == "finished"
    # Role gates: coding.work is admin/configurator; viewer cannot read.
    r = client.post("/billing/coding/enc-1", json={
        "diagnoses": [{"code": "E11.9"}]}, headers=CLINICIAN)
    assert r.status_code == 403
    assert client.get("/billing/coding/queue", headers=VIEWER).status_code == 403
    # Unknown code -> 422 naming it; the set is the authority.
    r = client.post("/billing/coding/enc-1", json={
        "diagnoses": [{"code": "X99.9"}]}, headers=CONFIG)
    assert r.status_code == 422 and "X99.9" in r.json()["detail"]
    # Systems + displays resolve from the published value sets.
    r = client.post("/billing/coding/enc-1", json={
        "diagnoses": [{"code": "E11.9"}],
        "procedures": [{"code": "93005"}, {"code": "99214"}]}, headers=CONFIG)
    assert r.status_code == 201, r.text
    record = r.json()
    assert record["diagnoses"][0]["system"] == "http://hl7.org/fhir/sid/icd-10-cm"
    assert record["diagnoses"][0]["display"] == \
        "Type 2 diabetes mellitus without complications"
    assert record["procedures"][1]["system"] == "http://www.ama-assn.org/go/cpt"
    assert record["coded_by"] == "dev-configurator"
    # Coded encounters leave the queue; a second POST 409s; PUT amends.
    queue = client.get("/billing/coding/queue", headers=ADMIN).json()
    assert queue["total"] == 0
    r = client.post("/billing/coding/enc-1", json={
        "diagnoses": [{"code": "E11.9"}]}, headers=CONFIG)
    assert r.status_code == 409
    r = client.put("/billing/coding/enc-1", json={
        "diagnoses": [{"code": "E11.9"}, {"code": "I10"}],
        "procedures": [{"code": "93005"}]}, headers=CONFIG)
    assert r.status_code == 200, r.text
    assert [p["code"] for p in r.json()["procedures"]] == ["93005"]
    assert [d["code"] for d in r.json()["diagnoses"]] == ["E11.9", "I10"]


def test_coding_queue_includes_signed_note_encounters(client):
    _patient(client, "pat-1")
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": "enc-visit", "status": "in-progress",
        "subject": {"reference": "Patient/pat-1"},
        "period": {"start": "2026-01-10T09:00:00Z"}}, headers=ADMIN)
    assert r.status_code == 201, r.text
    queue = client.get("/billing/coding/queue", headers=ADMIN).json()
    assert queue["total"] == 0  # neither finished nor signed-noted yet
    r = client.post("/notes", json={
        "note_type": "progress", "patient": "pat-1",
        "encounter": {"reference": "Encounter/enc-visit"},
        "text": "Synthetic visit note."}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note_id = r.json()["id"]
    queue = client.get("/billing/coding/queue", headers=ADMIN).json()
    assert queue["total"] == 0  # a DRAFT note does not queue the encounter
    r = client.post(f"/notes/{note_id}/sign", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    queue = client.get("/billing/coding/queue", headers=ADMIN).json()
    assert [i["encounter_id"] for i in queue["items"]] == ["enc-visit"]
    assert queue["items"][0]["reason"] == "signed-note"
    assert queue["items"][0]["signed_notes"] == 1


def test_claim_generation_consumes_coding_record(client):
    _chart(client, "pat-9", "enc-9", "44054006", proc_code="93005", obs=True)
    r = client.post("/billing/coding/enc-9", json={
        "diagnoses": [{"code": "E11.9"}],
        "procedures": [{"code": "93005"}, {"code": "99214"}]}, headers=CONFIG)
    assert r.status_code == 201, r.text
    claim = _claim(client, "enc-9", submit=False)
    # The coded diagnoses ARE the diagnosis section.
    assert claim["diagnosis"][0]["diagnosisCodeableConcept"]["coding"][0] == {
        "system": "http://hl7.org/fhir/sid/icd-10-cm", "code": "E11.9",
        "display": "Type 2 diabetes mellitus without complications"}
    lines = {item["productOrService"]["coding"][0]["code"]: item
             for item in claim["item"]}
    # 93005 charged ONCE (the coded line replaces the chart line);
    # 99214 exists only as a coded line, priced from the fee schedule.
    assert sum(1 for i in claim["item"]
               if i["productOrService"]["coding"][0]["code"] == "93005") == 1
    assert lines["99214"]["unitPrice"]["value"] == 180.00
    source = {e["url"]: e.get("valueString")
              for e in lines["99214"]["extension"]}
    assert source["urn:opencare:extension/charge-source"] == "CodingRecord/enc-9"
    # 55 (problem eval) + 45 (ECG, coded) + 12 (glucose obs) + 180 (99214).
    assert claim["total"] == {"value": 292.00, "currency": "USD"}


def test_require_coding_flip_blocks_uncoded_claim_generation(client):
    _chart(client, "pat-1", "enc-1", "44054006")
    _chart(client, "pat-2", "enc-2", "44054006")
    # Default (flag absent): uncoded generation works — Phase 4A behaviour.
    assert _claim(client, "enc-1", submit=False)["status"] == "draft"
    # Publish claim-lifecycle v2 with requireCoding: true.
    eff = client.get("/studio/config/workflow/effective/claim-lifecycle",
                     headers=ADMIN).json()
    payload = dict(eff["payload"])
    payload["requireCoding"] = True
    r = client.post("/studio/config/workflow/claim-lifecycle",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/workflow/claim-lifecycle/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/billing/claims/generate",
                    json={"encounter_id": "enc-2"}, headers=ADMIN)
    assert r.status_code == 409
    assert "has not been coded" in r.json()["detail"]
    # Coding the encounter unblocks generation.
    r = client.post("/billing/coding/enc-2", json={
        "diagnoses": [{"code": "E11.9"}]}, headers=CONFIG)
    assert r.status_code == 201, r.text
    assert _claim(client, "enc-2", submit=False)["status"] == "draft"
    # The flag itself is validated: a non-boolean value cannot publish.
    bad = dict(payload)
    bad["requireCoding"] = "yes"
    r = client.post("/studio/config/workflow/claim-lifecycle",
                    json=bad, headers=ADMIN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 835 remittance posting
# ---------------------------------------------------------------------------

def _three_claims(client):
    """A: 112.00 paid in full. B: 240.00 partial. C: 52.00 denied.
    D: 45.00 submitted, pended (unmapped CLP02=5)."""
    _chart(client, "pat-1", "enc-a", "44054006", proc_code="93005", obs=True)
    a = _claim(client, "enc-a")
    _chart(client, "pat-2", "enc-b", "55822004", proc_code="20610")
    b = _claim(client, "enc-b")
    _chart(client, "pat-3", "enc-c", "195967001", obs=True)
    c = _claim(client, "enc-c")
    # D gets its own patient: charge capture bills the patient's whole
    # problem list, so a second encounter for pat-1 would re-bill
    # pat-1's diabetes (the Phase 4A attribution rule).
    _chart(client, "pat-4", "enc-d", "38341003")
    d = _claim(client, "enc-d")
    assert a["total"]["value"] == 112.00 and b["total"]["value"] == 240.00
    assert c["total"]["value"] == 52.00 and d["total"]["value"] == 45.00
    return a, b, c, d


def _full_835(a, b, c, d):
    return _835([
        _clp(a["id"], "1", "112.00", "112.00", "0.00", extra=[
            "SVC*HC:93005*45.00*45.00", "SVC*HC:2345-7*12.00*12.00"]),
        _clp(b["id"], "1", "240.00", "180.00", "20.00", extra=[
            "SVC*HC:20610*210.00*180.00", "CAS*CO*45*30.00",
            "CAS*PR*1*20.00"]),
        _clp(c["id"], "4", "52.00", "0.00", "0.00", extra=[
            "CAS*CO*197*52.00"]),
        _clp("claim-ghost", "1", "10.00", "10.00", "0.00"),
        _clp(d["id"], "5", "45.00", "0.00", "0.00"),
    ])


def test_835_posting_math_statuses_and_exceptions(client):
    a, b, c, d = _three_claims(client)
    r = _post835(client, _full_835(a, b, c, d))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch"]["check_number"] == "CHK-778899"
    assert body["batch"]["total_amount"] == 302.00
    assert body["batch"]["posted"] == 3
    assert body["batch"]["exceptions"] == 2
    reasons = {e["claim_ref"]: e["reason"] for e in body["exceptions"]}
    assert "unknown claim" in reasons["claim-ghost"]
    assert "unmapped CLP02" in reasons[d["id"]]
    posted = {p["claim_id"]: p for p in body["posted"]}
    assert posted[a["id"]]["to_status"] == "paid"
    assert posted[a["id"]]["balance_after"] == 0.00
    # B: 240 - 180 paid - 30 contractual (CO) - 20 patient resp = 10 left.
    assert posted[b["id"]]["to_status"] == "partially-paid"
    assert posted[b["id"]]["balance_after"] == 10.00
    assert posted[c["id"]]["to_status"] == "denied"
    # Stored claims reflect the posting; D is untouched.
    assert _get_claim(client, a["id"])["status"] == "paid"
    assert _get_claim(client, b["id"])["status"] == "partially-paid"
    assert _get_claim(client, c["id"])["status"] == "denied"
    assert _get_claim(client, d["id"])["status"] == "submitted"
    history = _get_claim(client, c["id"])["statusHistory"]
    assert any(h["to"] == "denied" and "Remittance" in (h["note"] or "")
               for h in history)
    # Batches + exceptions are queryable.
    batches = client.get("/billing/remittances", headers=ADMIN).json()
    assert batches["total"] == 1 and batches["items"][0]["posted"] == 3
    exceptions = client.get("/billing/remittances/exceptions",
                            headers=ADMIN).json()
    assert exceptions["total"] == 2
    # Statement math for pat-2 (claim B) is exact.
    stmt = client.get("/billing/statements/pat-2", headers=ADMIN).json()
    line = stmt["claims"][0]
    assert line["paid"] == 180.00 and line["adjustments"] == 30.00
    assert line["patient_responsibility"] == 20.00
    assert line["patient_due"] == 20.00 and line["balance"] == 10.00


def test_835_duplicate_check_repost_is_all_exceptions(client):
    a, b, c, d = _three_claims(client)
    text = _full_835(a, b, c, d)
    assert _post835(client, text).status_code == 201
    r = _post835(client, text)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch"]["posted"] == 0
    assert body["batch"]["exceptions"] == 5
    dup = [e for e in body["exceptions"]
           if "duplicate remittance" in e["reason"]]
    assert len(dup) == 3  # A, B, C — the ghost/unmapped repeat as before
    assert _get_claim(client, a["id"])["status"] == "paid"


def test_835_malformed_files_post_nothing(client):
    a, _, _, _ = _three_claims(client)
    # Wrong SE count.
    text = _835([_clp(a["id"], "1", "112.00", "112.00", "0.00")], total="112.00")
    tampered = text.replace(f"SE*{_se_count(text)}", "SE*99")
    r = _post835(client, tampered)
    assert r.status_code == 422 and "SE count" in r.json()["detail"]
    # Not an 835 at all.
    r = _post835(client, "ST*837*0001~\nSE*2*0001~\n")
    assert r.status_code == 422 and "not '835'" in r.json()["detail"]
    # A non-numeric amount names its segment.
    bad_amount = _835([_clp(a["id"], "1", "112.00", "abc", "0.00")],
                      total="112.00")
    r = _post835(client, bad_amount)
    assert r.status_code == 422 and "CLP" in r.json()["detail"]
    # Nothing posted through any of it.
    assert _get_claim(client, a["id"])["status"] == "submitted"
    batches = client.get("/billing/remittances", headers=ADMIN).json()
    assert batches["total"] == 0


def _se_count(text):
    for segment in text.split("~"):
        segment = segment.strip()
        if segment.startswith("SE*"):
            return segment.split("*")[1]
    raise AssertionError("no SE segment")


def test_835_against_draft_claim_is_an_exception(client):
    _chart(client, "pat-1", "enc-1", "44054006")
    draft = _claim(client, "enc-1", submit=False)
    r = _post835(client, _835(
        [_clp(draft["id"], "1", "55.00", "55.00", "0.00")], total="55.00"))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["batch"]["posted"] == 0 and body["batch"]["exceptions"] == 1
    assert "submitted claim" in body["exceptions"][0]["reason"]
    assert _get_claim(client, draft["id"])["status"] == "draft"


# ---------------------------------------------------------------------------
# Denials workqueue + rebill
# ---------------------------------------------------------------------------

def _denied_claim(client, pid="pat-3", eid="enc-c"):
    _chart(client, pid, eid, "195967001", obs=True)
    claim = _claim(client, eid)
    r = _post835(client, _835(
        [_clp(claim["id"], "4", "52.00", "0.00", "0.00",
              extra=["CAS*CO*197*52.00"])],
        check="CHK-DENY", total="0.00"))
    assert r.status_code == 201, r.text
    assert _get_claim(client, claim["id"])["status"] == "denied"
    return claim


def test_denials_queue_reasons_assign_note_and_appeal_resolution(client):
    claim = _denied_claim(client)
    queue = client.get("/billing/denials", headers=ADMIN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["claim"]["id"] == claim["id"] and item["kind"] == "denied"
    assert item["reasons"] == [{
        "group": "CO", "reason": "197", "amount": 52.00, "level": "claim",
        "line_code": None, "category": "authorisation",
        "display": "Precertification/authorization/notification absent",
        "check_number": "CHK-DENY"}]
    assert "appealed" in queue["outcomes"]
    # Assign + note.
    r = client.post(f"/billing/denials/{claim['id']}/assign",
                    json={"assignee": "rev-cycle-1"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["work"]["assignee"] == "rev-cycle-1"
    r = client.post(f"/billing/denials/{claim['id']}/note",
                    json={"note": "Auth was filed late; appealing."},
                    headers=ADMIN)
    assert r.json()["work"]["notes"][0]["text"] == \
        "Auth was filed late; appealing."
    # Resolving as appealed BEFORE appealing is refused — the
    # workqueue records the lifecycle, it never forks it.
    r = client.post(f"/billing/denials/{claim['id']}/resolve",
                    json={"outcome": "appealed"}, headers=ADMIN)
    assert r.status_code == 422 and "has not been appealed" in r.json()["detail"]
    # Appeal through the real Phase 4A flow (once, with a note).
    r = client.post(f"/billing/claims/{claim['id']}/transition",
                    json={"to_status": "appealed",
                          "note": "Auth obtained retroactively"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post(f"/billing/denials/{claim['id']}/resolve",
                    json={"outcome": "appealed"}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["work"]["outcome"] == "appealed"
    # Resolved items leave the default queue.
    queue = client.get("/billing/denials", headers=ADMIN).json()
    assert queue["total"] == 0
    queue = client.get("/billing/denials?include_resolved=true",
                       headers=ADMIN).json()
    assert queue["total"] == 1
    # Unknown outcomes are refused against the published list.
    r = client.post(f"/billing/denials/{claim['id']}/resolve",
                    json={"outcome": "ignored"}, headers=ADMIN)
    assert r.status_code == 422


def test_rebill_creates_linked_corrected_claim_original_preserved(client):
    claim = _denied_claim(client, pid="pat-4", eid="enc-r")
    before = _get_claim(client, claim["id"])
    r = client.post(f"/billing/claims/{claim['id']}/rebill", headers=ADMIN)
    assert r.status_code == 201, r.text
    new_claim = r.json()
    assert new_claim["id"] != claim["id"]
    assert new_claim["status"] == "draft"
    assert new_claim["total"] == before["total"]
    assert new_claim["appealCount"] == 0
    rebill_of = {e["url"]: e.get("valueString")
                 for e in new_claim.get("extension") or []}
    assert rebill_of["urn:opencare:extension/rebill-of"] == claim["id"]
    assert new_claim["statusHistory"][-1]["note"] == \
        f"Rebill (corrected claim) of Claim/{claim['id']}"
    # The original is byte-preserved: still denied, remittance intact.
    after = _get_claim(client, claim["id"])
    assert after["status"] == "denied"
    assert after["statusHistory"] == before["statusHistory"]
    assert after["appealCount"] == 0
    # Resolving as rebilled requires the real corrected claim.
    r = client.post(f"/billing/denials/{claim['id']}/resolve",
                    json={"outcome": "rebilled"}, headers=ADMIN)
    assert r.status_code == 422
    r = client.post(f"/billing/denials/{claim['id']}/resolve",
                    json={"outcome": "rebilled",
                          "outcome_claim_id": new_claim["id"]}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["work"]["outcome_claim_id"] == new_claim["id"]
    # Rebilling a non-denied claim is refused.
    r = client.post(f"/billing/claims/{new_claim['id']}/rebill", headers=ADMIN)
    assert r.status_code == 409


# ---------------------------------------------------------------------------
# Statements + recorded patient payments
# ---------------------------------------------------------------------------

def _partial_claim_for_pat1(client):
    _chart(client, "pat-1", "enc-p", "55822004", proc_code="20610")
    claim = _claim(client, "enc-p")
    r = _post835(client, _835(
        [_clp(claim["id"], "1", "240.00", "180.00", "20.00", extra=[
            "SVC*HC:20610*210.00*180.00", "CAS*CO*45*30.00",
            "CAS*PR*1*20.00"])], check="CHK-P", total="180.00"))
    assert r.status_code == 201, r.text
    return claim


def test_statement_and_recorded_patient_payments(client):
    claim = _partial_claim_for_pat1(client)
    stmt = client.get("/billing/statements/pat-1", headers=PATIENT).json()
    assert stmt["totals"]["patient_due"] == 20.00
    assert stmt["totals"]["patient_paid"] == 0.00
    assert "no card processor" in stmt["disclaimer"]
    # Overpayment and unknown methods are refused.
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 25.00, "method": "card"}, headers=PATIENT)
    assert r.status_code == 422 and "exceeds" in r.json()["detail"]
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 5.00, "method": "bitcoin"}, headers=PATIENT)
    assert r.status_code == 422 and "bitcoin" in r.json()["detail"]
    # The patient pays part; staff records the rest.
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 12.50, "method": "card"}, headers=PATIENT)
    assert r.status_code == 201, r.text
    assert r.json()["recorded_by"] == "dev-patient"
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 7.50, "method": "cash"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    stmt = client.get("/billing/statements/pat-1", headers=ADMIN).json()
    assert stmt["totals"]["patient_paid"] == 20.00
    assert stmt["totals"]["patient_due"] == 0.00
    payments = client.get(f"/billing/claims/{claim['id']}/patient-payments",
                          headers=ADMIN).json()
    assert payments["total"] == 2
    # Nothing left to collect.
    r = client.post(f"/billing/claims/{claim['id']}/patient-payments",
                    json={"amount": 0.01, "method": "card"}, headers=ADMIN)
    assert r.status_code == 422
    # Scoping: another patient's statement is not the patient's to see,
    # and the viewer role has no billing view at all.
    _patient(client, "pat-2")
    assert client.get("/billing/statements/pat-2",
                      headers=PATIENT).status_code == 403
    assert client.get("/billing/statements/pat-1",
                      headers=VIEWER).status_code == 403
    assert client.get("/billing/statements/pat-2",
                      headers=ADMIN).status_code == 200
