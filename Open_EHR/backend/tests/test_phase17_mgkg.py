"""Phase 17 (BD-08 residual) — weight-based (mg/kg) dose checking.

Phase 15 built the paediatrics pack but named the residual honestly:
"weight-based mg/kg dose checking is not expressible in the current
monitoring vocabulary and is not claimed." Phase 17 closes it by
extending the vocabulary of the generic pharmacy monitoring hook
(app.pharmacy.medication_monitoring_check — the hook every specialty
pack's rules run through):

- a monitoring rule may carry a `weightBasedDosing` section: dose-
  per-kg bounds (min/max mg/kg/dose and/or mg/kg/day), flat or per
  age band. Rule CONTENT is governed config (published
  terminology/medication-monitoring); only the vocabulary is code.
- evaluation uses the patient's recorded weight (FHIR Observation,
  LOINC 29463-7, released results only), the order's structured
  doseQuantity and timing, and the Patient birthDate for age bands.
- violations BLOCK (409 at dispense) or FLAG (warning) through the
  same mechanism the other packs prove; the verification queue
  carries the explicit evaluation and the DUR queue signals it.
- missing weight (or dose / birthDate) produces an explicit
  CANNOT-EVALUATE result that blocks/flags like a violation —
  never a silent pass.

Proven twice: with directly published config (flat + banded rules,
block + flag actions), and through the specialty-paediatrics pack,
which now publishes mg/kg rules for paracetamol as governed config.
Synthetic patients only.
"""
import json
from pathlib import Path

from .conftest import ADMIN, CLINICIAN

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"
LOINC = "http://loinc.org"
WEIGHT = "29463-7"
PARACETAMOL = "paracetamol"

FLAT_RULE = {
    "medicationCode": PARACETAMOL, "medicationDisplay": "Paracetamol",
    "action": "block",
    "weightBasedDosing": {"doseUnit": "mg", "minMgPerKgPerDose": 10,
                          "maxMgPerKgPerDose": 15, "maxMgPerKgPerDay": 60},
}
BANDED_RULE = {
    "medicationCode": PARACETAMOL, "medicationDisplay": "Paracetamol",
    "action": "block",
    "weightBasedDosing": {"doseUnit": "mg", "ageBands": [
        {"ageBand": "1-4y", "minAgeYears": 1, "maxAgeYears": 5,
         "minMgPerKgPerDose": 10, "maxMgPerKgPerDose": 15,
         "maxMgPerKgPerDay": 60}]},
}


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _publish_formulary_with_paracetamol(client):
    effective = client.get(
        "/studio/config/terminology/effective/formulary",
        headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    if not any(c.get("code") == PARACETAMOL for c in payload["concepts"]):
        payload["concepts"].append({
            "code": PARACETAMOL, "active": True,
            "display": "Paracetamol 250 MG Oral Tablet (local synthetic code)",
            "system": "urn:opencare:local-formulary",
            "stockTracked": True, "initialStock": 50,
            "formularyStatus": "formulary"})
    _publish(client, "terminology", "formulary", payload)


def _publish_monitoring(client, rule):
    _publish(client, "terminology", "medication-monitoring", {
        "name": "Medication monitoring rules — Phase 17 mg/kg tests",
        "system": "urn:opencare:medication-monitoring",
        "concepts": [{"code": PARACETAMOL, "active": True,
                      "display": "Paracetamol — mg/kg rule"}],
        "rules": [rule]})


def _setup(client, rule=FLAT_RULE):
    _publish_formulary_with_paracetamol(client)
    _publish_monitoring(client, rule)


def _patient(client, pid, birth="2023-01-15"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["P17"]}],
        "birthDate": birth, "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _weight(client, pid, kg):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": WEIGHT}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": kg, "unit": "kg"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _dosed_order(client, pid, dose_mg, per_day=None):
    # Wave A (CHART-B3): structured sigs must be complete against
    # the published medication-sig vocabulary (text + route too).
    instruction = {"text": f"{dose_mg} mg by mouth",
                   "route": {"coding": [{"code": "oral",
                                         "display": "Oral"}]},
                   "doseAndRate": [
                       {"doseQuantity": {"value": dose_mg, "unit": "mg"}}]}
    if per_day is not None:
        instruction["timing"] = {"repeat": {
            "frequency": per_day, "period": 1, "periodUnit": "d"}}
    else:
        # CHART-B3: a structured sig needs timing (or an as-needed
        # flag); once-daily is the neutral default here.
        instruction["timing"] = {"repeat": {
            "frequency": 1, "period": 1, "periodUnit": "d"}}
    r = client.post("/orders", json={
        "type": "medication", "code": PARACETAMOL, "patient": pid,
        "dosageInstruction": [instruction]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _dispense(client, oid):
    # Wave A (PHARM-B1): dispensing requires a current verification.
    # Record one first (tolerantly — mg/kg BLOCK scenarios refuse at
    # verify too, and the dispense attempt still surfaces the
    # monitoring refusal these tests assert, as before).
    client.post(f"/pharmacy/verification/{oid}/verify", json={},
                headers=CLINICIAN)
    return client.post("/pharmacy/dispense",
                       json={"medication_request_id": oid},
                       headers=CLINICIAN)


def _queue_item(client, oid):
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    for item in queue["items"]:
        if item["order_id"] == oid:
            return item
    raise AssertionError(f"order {oid} not in verification queue")


# ------------------------------------------------------- direct config

def test_over_mgkg_dose_blocked_and_surfaced(client):
    _setup(client)
    _patient(client, "mgkg-over")          # age ~3, band/flat rules
    _weight(client, "mgkg-over", 15)
    oid = _dosed_order(client, "mgkg-over", 500)   # 33.33 mg/kg/dose

    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "violation"
    assert evaluation["weight_kg"] == 15
    assert evaluation["mg_per_kg_per_dose"] == 33.333
    assert any(b.get("rule_type") == "weight-based"
               for b in item["monitoring_blockers"])

    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    dur_item = [i for i in dur["items"] if i["order_id"] == oid][0]
    assert any(s["type"] == "monitoring-block" and "mg/kg" in s["detail"]
               for s in dur_item["signals"])

    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "mg/kg" in r.json()["detail"]
    assert "above the published maximum" in r.json()["detail"]


def test_in_range_dose_passes(client):
    _setup(client)
    _patient(client, "mgkg-ok")
    _weight(client, "mgkg-ok", 15)
    oid = _dosed_order(client, "mgkg-ok", 200)     # 13.33 mg/kg/dose

    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "within-range"
    assert item["monitoring_blockers"] == []

    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 49
    assert r.json()["monitoring_warnings"] == []


def test_missing_weight_is_cannot_evaluate_never_silent_pass(client):
    _setup(client)
    _patient(client, "mgkg-noweight")
    oid = _dosed_order(client, "mgkg-noweight", 200)

    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "cannot-evaluate"
    assert "body-weight" in evaluation["problem"]
    assert any(b.get("rule_type") == "weight-based"
               for b in item["monitoring_blockers"])

    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "cannot be evaluated" in r.json()["detail"]
    assert "body-weight" in r.json()["detail"]


def test_per_day_bound_blocks_when_single_dose_in_range(client):
    _setup(client)
    _patient(client, "mgkg-day")
    _weight(client, "mgkg-day", 15)
    # 13.33 mg/kg/dose is in range, but 6 doses/day = 80 mg/kg/day.
    oid = _dosed_order(client, "mgkg-day", 200, per_day=6)
    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "violation"
    assert evaluation["mg_per_kg_per_day"] == 80.0
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "mg/kg/day" in r.json()["detail"]
    assert "above the published maximum" in r.json()["detail"]


def test_flag_action_warns_instead_of_blocking(client):
    _setup(client, rule={**FLAT_RULE, "action": "flag"})
    _patient(client, "mgkg-flag")
    _weight(client, "mgkg-flag", 15)
    oid = _dosed_order(client, "mgkg-flag", 500)
    item = _queue_item(client, oid)
    assert item["monitoring_blockers"] == []
    assert any(w.get("rule_type") == "weight-based"
               for w in item["monitoring_warnings"])
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert any(w.get("rule_type") == "weight-based"
               for w in r.json()["monitoring_warnings"])


def test_banded_rule_scopes_by_age(client):
    _setup(client, rule=BANDED_RULE)
    _patient(client, "mgkg-child")                    # ~3 years old
    _weight(client, "mgkg-child", 15)
    oid = _dosed_order(client, "mgkg-child", 500)
    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "violation"
    assert evaluation["age_band"] == "1-4y"
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text

    # An adult is outside every published band: the paediatric rule
    # does not govern them — explicitly not-applicable, and even a
    # large absolute dose is not blocked by THIS rule.
    _patient(client, "mgkg-adult", birth="1990-01-01")
    _weight(client, "mgkg-adult", 70)
    oid = _dosed_order(client, "mgkg-adult", 2000)
    item = _queue_item(client, oid)
    (evaluation,) = item["mgkg_evaluations"]
    assert evaluation["status"] == "not-applicable"
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text


# ------------------------------------------------------------ the pack

def _enable_pack(client, name):
    inst = client.post(f"/studio/packs/{name}/install",
                       headers=ADMIN).json()
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def test_paediatrics_pack_publishes_mgkg_rules_and_they_fire(client):
    manifest = json.loads(
        (PACKS_DIR / "specialty-paediatrics.pack.json").read_text())
    assert manifest["version"] == "1.1.0"
    _enable_pack(client, "specialty-paediatrics")

    # The pack's mg/kg rule is live governed config, not code.
    effective = client.get(
        "/studio/config/terminology/effective/medication-monitoring",
        headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    rule = next(r for r in payload["rules"]
                if r["medicationCode"] == PARACETAMOL)
    assert rule["action"] == "block"
    assert rule["weightBasedDosing"]["ageBands"]

    _patient(client, "mgkg-pack")
    _weight(client, "mgkg-pack", 15)
    oid = _dosed_order(client, "mgkg-pack", 500)   # 33.33 mg/kg/dose
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "mg/kg" in r.json()["detail"]

    oid = _dosed_order(client, "mgkg-pack", 200)   # 13.33 mg/kg/dose
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 79           # pack initialStock 80
