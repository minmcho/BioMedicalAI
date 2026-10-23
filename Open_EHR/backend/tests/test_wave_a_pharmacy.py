"""Wave A — Pharmacy safety gates (PHARM-B1 / PHARM-M1 / PHARM-M2).

PHARM-B1: dispensing refuses 409 unless the order carries a CURRENT
pharmacist verification (active, bound to the order's current
version, inside the governed validity window); refusals are audited
and write nothing. Verifications can be revoked (reason required),
which returns the order to the verification queue.

PHARM-M1: verification hard-stops on a recorded-allergy conflict, a
monitoring blocker and a high-alert medication — 422 naming each
signal — unless a governed override (reason code from the published
pharmacy-ops list + a note) or a recorded "override" DUR disposition
covers the signal. DUR dispositions are recordable and ride the DUR
queue.

PHARM-M2: the BCMA-lite typed-ID path is retired (404); the one
administration engine is the five-rights BCMA scan, which also
requires the authorizing order's verification to still be current
at administration time.
"""
import datetime as dt

from sqlalchemy import select

from app.models import AuditLog, FhirResource, PharmacyVerification
from tests.conftest import ADMIN, CLINICIAN, VIEWER

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LISINOPRIL = "1049221"
APIXABAN = "1351886"       # restricted + high-alert in the seed
ANC = "751-8"

DOSE = [{
    "text": "1 tablet by mouth once daily",
    "doseAndRate": [{"doseQuantity": {"value": 1, "unit": "tablets"}}],
    "route": {"coding": [{"system": "http://snomed.info/sct",
                          "code": "oral", "display": "Oral"}]},
    "timing": {"repeat": {"frequency": 1, "period": 24, "periodUnit": "h"}},
}]


def _patient(client, pid, birth="1962-03-14"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid, "birthDate": birth,
        "name": [{"family": "WaveA", "given": ["P"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _med_order(client, pid, code=LISINOPRIL,
               display="Lisinopril 10 MG Oral Tablet", dose=None):
    body = {"type": "medication", "code": code, "code_display": display,
            "patient": pid}
    if dose:
        body["dosageInstruction"] = dose
    r = client.post("/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _verify(client, oid, payload=None, headers=CLINICIAN):
    return client.post(f"/pharmacy/verification/{oid}/verify",
                       json=payload or {}, headers=headers)


def _dispense(client, oid, headers=CLINICIAN):
    return client.post("/pharmacy/dispense",
                       json={"medication_request_id": oid}, headers=headers)


def _stock(client, code=LISINOPRIL):
    items = client.get("/pharmacy/stock", headers=CLINICIAN).json()["items"]
    return next(i for i in items if i["code"] == code)["quantity"]


def _audit_rows(client, action):
    factory = client.app.state.session_factory
    with factory() as session:
        return [(row.actor, row.payload) for row in session.scalars(
            select(AuditLog).where(AuditLog.action == action)).all()]


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _effective(client, kind, key):
    out = client.get(f"/studio/config/{kind}/effective/{key}",
                     headers=ADMIN).json()
    return out["payload"] if "payload" in out else out


def _allergy(client, pid, code=LISINOPRIL, display="Lisinopril"):
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"alg-{pid}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": RXNORM, "code": code,
                             "display": display}]},
        "patient": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# PHARM-B1 — the dispense gate
# ---------------------------------------------------------------------------

def test_dispense_unverified_refused_409_audited_zero_writes(client):
    pid = _patient(client, "wa-b1-unverified")
    oid = _med_order(client, pid)
    before = _stock(client)
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "no verification on file" in r.json()["detail"]
    # Zero state change: no dispense row, stock untouched, request
    # still active.
    assert client.get("/pharmacy/dispenses",
                      headers=CLINICIAN).json()["total"] == 0
    assert _stock(client) == before
    req = client.get(f"/fhir/MedicationRequest/{oid}",
                     headers=CLINICIAN).json()
    assert req["status"] == "active"
    # The refusal itself is audited.
    rows = _audit_rows(client, "pharmacy.dispense.refused_unverified")
    assert len(rows) == 1
    assert rows[0][1]["verification_state"] == "none"
    assert rows[0][1]["medication_request_id"] == oid


def test_dispense_after_verify_succeeds_and_queue_shows_current(client):
    pid = _patient(client, "wa-b1-verified")
    oid = _med_order(client, pid)
    queue = client.get("/pharmacy/dispense-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["request"]["id"] == oid][0]
    assert item["verification"]["state"] == "none"
    assert item["verification"]["verified"] is False
    r = _verify(client, oid)
    assert r.status_code == 201, r.text
    queue = client.get("/pharmacy/dispense-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["request"]["id"] == oid][0]
    assert item["verification"]["state"] == "current"
    assert item["verification"]["verified"] is True
    assert item["verification"]["verified_by"] == "dev-clinician"
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text


def test_revoke_requires_reason_and_returns_order_to_queue(client):
    pid = _patient(client, "wa-b1-revoke")
    oid = _med_order(client, pid)
    assert _verify(client, oid).status_code == 201
    # Reason is mandatory.
    r = client.post(f"/pharmacy/verification/{oid}/revoke", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/pharmacy/verification/{oid}/revoke",
                    json={"reason": "Wrong dose confirmed with prescriber"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["verification"]["status"] == "revoked"
    assert r.json()["verification"]["revoked_by"] == "dev-clinician"
    assert r.json()["state"] == "revoked"
    # The order is back on the verification queue...
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    assert any(i["order_id"] == oid for i in queue["items"])
    # ...and dispensing now refuses, naming the revocation.
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "revoked" in r.json()["detail"]
    assert "dev-clinician" in r.json()["detail"]
    assert "Wrong dose confirmed" in r.json()["detail"]
    rows = _audit_rows(client, "pharmacy.verification.revoke")
    assert len(rows) == 1
    # Re-verification makes the order dispensable again (the unique
    # row is refreshed, not duplicated).
    assert _verify(client, oid).status_code == 201
    assert _dispense(client, oid).status_code == 201


def test_dispense_after_order_edit_is_stale_409(client):
    pid = _patient(client, "wa-b1-stale")
    oid = _med_order(client, pid)
    assert _verify(client, oid).status_code == 201
    # Edit the order: any write to the order bumps its
    # FhirResource.version (the FHIR store's update semantics — the
    # generic store exposes no MedicationRequest PUT route, so the
    # edit is applied through the store's own row, exactly the
    # version increment every update path performs). The
    # verification recorded against the old version is stale.
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "MedicationRequest",
            FhirResource.resource_id == oid)).one()
        body = dict(row.body or {})
        body["note"] = [{"text": "Dose clarified by prescriber"}]
        row.body = body
        row.version += 1
        session.commit()
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "order changed since verification" in r.json()["detail"]
    rows = _audit_rows(client, "pharmacy.dispense.refused_unverified")
    assert rows and rows[-1][1]["verification_state"] == "stale"
    # Re-verifying at the new version restores dispensability.
    assert _verify(client, oid).status_code == 201
    assert _dispense(client, oid).status_code == 201


def test_verification_expiry_window_is_governed(client, monkeypatch):
    import app.pharmacy_ops as ops
    pid = _patient(client, "wa-b1-expired")
    oid = _med_order(client, pid)
    payload = _effective(client, "terminology", "pharmacy-ops")
    payload["verificationValidityHours"] = 1
    # The Studio terminology validator requires a concepts list; the
    # seeded pharmacy-ops payload carries none (it is seeded
    # directly) — add the stub concept the Phase 17 tests use.
    payload.setdefault("concepts", [{
        "code": "pharmacy-ops", "display": "Pharmacy operations",
        "active": True}])
    _publish_config(client, "terminology", "pharmacy-ops", payload)
    assert _verify(client, oid).status_code == 201
    # Two hours later the verification is outside the governed window.
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    monkeypatch.setattr(ops, "_now", lambda: future)
    queue = client.get("/pharmacy/dispense-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["request"]["id"] == oid][0]
    assert item["verification"]["state"] == "expired"
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "expired" in r.json()["detail"]


# ---------------------------------------------------------------------------
# PHARM-M1 — verification hard stops, governed override, DUR dispositions
# ---------------------------------------------------------------------------

def test_verify_allergy_conflict_refused_422_naming_allergen(client):
    pid = _patient(client, "wa-m1-allergy")
    oid = _med_order(client, pid)
    _allergy(client, pid)
    r = _verify(client, oid)
    assert r.status_code == 422, r.text
    assert "allergy" in r.json()["detail"].lower()
    assert "Lisinopril" in r.json()["detail"]
    # Nothing was recorded: the order is still on the queue.
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    assert any(i["order_id"] == oid for i in queue["items"])


def test_verify_monitoring_blocker_refused_422(client):
    pid = _patient(client, "wa-m1-monitoring")
    oid = _med_order(client, pid)
    _publish_config(client, "terminology", "medication-monitoring", {
        "name": "Wave A monitoring rules",
        "concepts": [{"code": LISINOPRIL, "active": True,
                      "display": "Lisinopril — monitoring rule"}],
        "rules": [{"medicationCode": LISINOPRIL,
                   "medicationDisplay": "Lisinopril 10 MG Oral Tablet",
                   "action": "block",
                   "requiredLabs": [{"code": ANC,
                                     "display": "Absolute neutrophil count",
                                     "minValue": 1.5}]}]})
    r = _verify(client, oid)
    assert r.status_code == 422, r.text
    assert "Absolute neutrophil count" in r.json()["detail"]


def test_verify_governed_override_recorded_and_audited(client):
    pid = _patient(client, "wa-m1-override")
    oid = _med_order(client, pid)
    _allergy(client, pid)
    r = _verify(client, oid, {
        "override_reason_code": "allergy-cross-reactivity-assessed",
        "override_note": "Allergy was to lisinopril excipient; "
                         "pharmacy reviewed with allergist."})
    assert r.status_code == 201, r.text
    verification = r.json()["verification"]
    assert verification["override_reason_code"] == \
        "allergy-cross-reactivity-assessed"
    assert "excipient" in verification["override_note"]
    overridden = verification["overridden_signals"]
    assert overridden and overridden[0]["type"] == "allergy"
    assert overridden[0]["via"] == "override"
    # Recorded on the row itself.
    factory = client.app.state.session_factory
    with factory() as session:
        row = session.scalars(select(PharmacyVerification).where(
            PharmacyVerification.order_id == oid)).one()
        assert row.override_reason_code == \
            "allergy-cross-reactivity-assessed"
        assert row.status == "active"
        assert row.order_version is not None
    # And in the audit payload.
    rows = _audit_rows(client, "pharmacy.verify")
    assert rows[-1][1]["override_reason_code"] == \
        "allergy-cross-reactivity-assessed"
    # The verified order dispenses.
    assert _dispense(client, oid).status_code == 201


def test_verify_bogus_override_reason_code_refused_422(client):
    pid = _patient(client, "wa-m1-bogus")
    oid = _med_order(client, pid)
    _allergy(client, pid)
    r = _verify(client, oid, {
        "override_reason_code": "because-i-said-so",
        "override_note": "Trust me."})
    assert r.status_code == 422, r.text
    assert "allergy" in r.json()["detail"].lower()
    # A valid code without a note is also refused.
    r = _verify(client, oid, {
        "override_reason_code": "benefit-outweighs-risk"})
    assert r.status_code == 422, r.text


def test_verify_high_alert_without_override_refused_422(client):
    pid = _patient(client, "wa-m1-highalert")
    oid = _med_order(client, pid, APIXABAN,
                     "Apixaban 5 MG Oral Tablet")
    # Restriction acknowledged, but apixaban is high-alert in the
    # seeded formulary: verification still hard-stops.
    r = _verify(client, oid, {"restriction_acknowledged": True})
    assert r.status_code == 422, r.text
    assert "high-alert" in r.json()["detail"]
    # The queue item surfaces the signal + the high-alert flag.
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == oid][0]
    assert item["high_alert"] is True
    assert any(s["type"] == "high-alert"
               for s in item["hard_stop_signals"])
    assert "benefit-outweighs-risk" in \
        client.get("/pharmacy/verification-queue",
                   headers=CLINICIAN).json()["dur_override_reason_codes"]


def test_dur_disposition_satisfies_signal_at_verify(client):
    pid = _patient(client, "wa-m1-disposition")
    oid = _med_order(client, pid)
    _allergy(client, pid)
    # The DUR queue carries the allergy signal with its stable key.
    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    item = [i for i in dur["items"] if i["order_id"] == oid][0]
    signal = [s for s in item["signals"] if s["type"] == "allergy"][0]
    assert signal["signal_key"] == LISINOPRIL
    # A bogus reason code is refused; the governed one records.
    r = client.post(f"/pharmacy/dur/{oid}/disposition", json={
        "signal_type": "allergy", "signal_key": signal["signal_key"],
        "disposition": "override", "reason_code": "not-a-code",
        "note": "Reviewed."}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/pharmacy/dur/{oid}/disposition", json={
        "signal_type": "allergy", "signal_key": signal["signal_key"],
        "disposition": "override",
        "reason_code": "allergy-cross-reactivity-assessed",
        "note": "Allergist reviewed; proceed with monitoring."},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["disposition"]["disposition"] == "override"
    # The disposition rides the DUR queue item.
    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    item = [i for i in dur["items"] if i["order_id"] == oid][0]
    assert any(d["disposition"] == "override"
               and d["signal_type"] == "allergy"
               for d in item["dispositions"])
    # Verify now passes for that signal WITHOUT a payload override,
    # and records that it proceeded on the disposition.
    r = _verify(client, oid)
    assert r.status_code == 201, r.text
    overridden = r.json()["verification"]["overridden_signals"]
    assert overridden and overridden[0]["type"] == "allergy"
    assert overridden[0]["via"] == "disposition"
    rows = _audit_rows(client, "pharmacy.dur.disposition")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# PHARM-M2 — one administration path, verification-checked at scan time
# ---------------------------------------------------------------------------

def _dispensed_bcma_ready(client, pid):
    oid = _med_order(client, pid, dose=DOSE)
    assert _verify(client, oid).status_code == 201
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    return oid, r.json()["dispense"]["id"]


def _payloads(client, pid, dispense_id):
    wb = client.post("/pharmacy/barcodes/wristband",
                     json={"patient_id": pid}, headers=CLINICIAN)
    assert wb.status_code == 200, wb.text
    ml = client.post("/pharmacy/barcodes/med-label",
                     json={"dispense_id": dispense_id}, headers=CLINICIAN)
    assert ml.status_code == 200, ml.text
    return wb.json()["payload"], ml.json()["payload"]


def test_bcma_refuses_when_verification_revoked_after_dispense(client):
    pid = _patient(client, "wa-m2-revoked")
    oid, dispense_id = _dispensed_bcma_ready(client, pid)
    r = client.post(f"/pharmacy/verification/{oid}/revoke",
                    json={"reason": "Order under prescriber review"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    wristband, med_label = _payloads(client, pid, dispense_id)
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wristband, "med_label_payload": med_label},
        headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "verification state: revoked" in r.json()["detail"]
    # The refusal is audited as a refused scan; nothing administered.
    rows = _audit_rows(client, "pharmacy.bcma.scan_refused")
    assert rows and rows[-1][1]["verification_state"] == "revoked"
    assert client.get("/pharmacy/dispenses",
                      headers=CLINICIAN).json()["total"] == 1
    administrations = client.get("/fhir/MedicationAdministration",
                                 headers=CLINICIAN).json()
    assert administrations.get("total", 0) == 0


def test_bcma_happy_path_still_works_with_current_verification(client):
    pid = _patient(client, "wa-m2-happy")
    _, dispense_id = _dispensed_bcma_ready(client, pid)
    wristband, med_label = _payloads(client, pid, dispense_id)
    r = client.post("/pharmacy/bcma/administer", json={
        "wristband_payload": wristband, "med_label_payload": med_label},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    checks = {c["right"]: c["result"]
              for c in r.json()["bcma"]["checks"]}
    assert all(v == "pass" for v in checks.values())


def test_weak_typed_id_administer_path_is_gone(client):
    pid = _patient(client, "wa-m2-weak")
    _, dispense_id = _dispensed_bcma_ready(client, pid)
    r = client.post("/pharmacy/administer", json={
        "wristband_id": pid, "medication_code": LISINOPRIL,
        "dispense_id": dispense_id}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    # And the viewer role cannot verify/dispense around the gates.
    pid2 = _patient(client, "wa-m2-roles")
    oid2 = _med_order(client, pid2)
    assert _verify(client, oid2, headers=VIEWER).status_code == 403
    assert _dispense(client, oid2, headers=VIEWER).status_code == 403
