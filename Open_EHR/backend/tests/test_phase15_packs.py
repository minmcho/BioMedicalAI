"""Phase 15 — specialty pack depth (BD-03, BD-05, BD-06, BD-08).

Structural proof for the ten packs in scope (the seven BD-03 packs
brought to the ophthalmology/psychiatry exemplar depth + the three
new packs: transplant, dialysis, paediatrics): each installs as a
draft, enables through the governed lifecycle with its declared
configuration applied, its order sets place, its forms validate and
its registry lists — the Phase 9 structural pattern.

Deep proofs, one per scope item:
- BD-03 monitoring-hook rules (the psychiatry clozapine pattern,
  evaluated by the generic pharmacy gate): cardiology digoxin BLOCK,
  diabetes metformin FLAG, gastroenterology methotrexate BLOCK,
  pulmonology theophylline BLOCK, obgyn labetalol FLAG, orthopedics
  apixaban FLAG.
- BD-03 geriatrics governed `rule` config: nitrofurantoin verified
  at CrCl < 30 opens a prescribing-review Task via the
  pharmacy.dur.screened rules-engine event.
- BD-05 transplant: tacrolimus BLOCK (trough / creatinine /
  potassium) and the donor–recipient linkage form round-trip.
- BD-06 dialysis: the dialysis-session slot type books through the
  portal self-scheduling machinery at 240 minutes, the session
  flowsheet round-trips, and every adequacy order-set code places on
  the lab spine (/lab/orders).
- BD-08 paediatrics: gentamicin BLOCK (the part of paediatric safety
  the monitoring vocabulary honestly expresses — lab gating; mg/kg
  weight-based dosing is NOT expressible and is not claimed), and
  enabling the pack publishes the Paediatric Unit so the census
  shows paediatric beds as their own unit class.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tests.conftest import ADMIN, CLINICIAN, PATIENT

PACKS_DIR = Path(__file__).resolve().parent.parent / "packs"
LOINC = "http://loinc.org"

PACKS_15 = [
    "specialty-cardiology",
    "specialty-diabetes-endo",
    "specialty-gastroenterology",
    "specialty-geriatrics",
    "specialty-pulmonology",
    "specialty-obgyn",
    "specialty-orthopedics",
    "specialty-transplant",
    "specialty-dialysis",
    "specialty-paediatrics",
]


def _manifest(name):
    return json.loads((PACKS_DIR / f"{name}.pack.json").read_text())


def _patient(client, pid, **extra):
    body = {"resourceType": "Patient", "id": pid,
            "name": [{"family": "Synth", "given": ["Phase15"]}]}
    body.update(extra)
    r = client.post("/fhir/Patient", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _install(client, name):
    r = client.post(f"/studio/packs/{name}/install", headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


def _enable(client, name):
    inst = _install(client, name)
    iid = inst["id"]
    assert client.post(f"/studio/installations/{iid}/approve",
                       headers=ADMIN).status_code == 200
    assert client.post(f"/studio/installations/{iid}/attach",
                       headers=ADMIN).status_code == 200
    r = client.post(f"/studio/installations/{iid}/enable",
                    json={"parameters": {}}, headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


def _active_med_order(client, pid, code):
    r = client.post("/orders", json={
        "type": "medication", "code": code, "patient": pid},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


def _dispense(client, oid):
    # Wave A (PHARM-B1): dispensing requires a current verification.
    # Record one first (tolerantly — scenarios whose monitoring rule
    # BLOCKs at verify time still reach the dispense gate, whose
    # monitoring refusal these tests assert, exactly as before).
    # Wave A (PHARM-M1): a HIGH-ALERT medication (apixaban in these
    # fixtures) additionally requires the governed override at
    # verify time; the override fields are inert when no hard-stop
    # signal exists, so they are safe to send for every order here.
    client.post(f"/pharmacy/verification/{oid}/verify", json={
        "restriction_acknowledged": True,
        "override_reason_code": "benefit-outweighs-risk",
        "override_note": "Pack fixture: governed verify override."},
        headers=CLINICIAN)
    return client.post("/pharmacy/dispense",
                       json={"medication_request_id": oid},
                       headers=CLINICIAN)


def _observation(client, pid, code, value, when=None, unit="mg/dL"):
    when = when or datetime.now(timezone.utc)
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "effectiveDateTime": when.isoformat(),
        "valueQuantity": {"value": value, "unit": unit}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _now():
    return datetime.now(timezone.utc)


def _answer_for(item):
    type_ = item["type"]
    if type_ == "boolean":
        value = {"valueBoolean": True}
    elif type_ == "integer":
        value = {"valueInteger": 1}
    elif type_ == "decimal":
        value = {"valueDecimal": 1.5}
    elif type_ == "date":
        value = {"valueDate": "2026-01-01"}
    elif type_ == "dateTime":
        value = {"valueDateTime": "2026-01-01T09:00:00Z"}
    elif type_ == "time":
        value = {"valueTime": "09:00:00"}
    elif type_ in ("choice", "open-choice"):
        options = item.get("answerOption") or []
        code = ((options[0].get("valueCoding") or {}).get("code")
                if options else "x")
        value = {"valueCoding": {"code": code}}
    else:
        value = {"valueString": "synthetic"}
    return {"linkId": item["linkId"], "answer": [value]}


def _form_items(client, form_key):
    r = client.get(f"/studio/config/form/effective/{form_key}",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]["item"]


def _submit_form(client, form_key, pid):
    items = _form_items(client, form_key)
    answers = [_answer_for(i) for i in items
               if i.get("required")
               and i["type"] not in ("group", "display")]
    r = client.post("/flowsheets", json={
        "form": form_key, "patient": pid, "answers": answers},
        headers=CLINICIAN)
    assert r.status_code == 201, (form_key, r.text)
    return r


# ---------------------------------------------------------------------------
# Structure: all ten packs — lifecycle, order sets, forms, registries
# ---------------------------------------------------------------------------

def test_ten_packs_install_enable_and_apply_declared_config(client):
    names = {p["name"] for p in
             client.get("/studio/packs", headers=ADMIN).json()}
    for name in PACKS_15:
        assert name in names
        inst = _install(client, name)
        assert inst["status"] == "installed_draft"
        # A pack never self-enables: enable from draft is a 409.
        r = client.post(f"/studio/installations/{inst['id']}/enable",
                        json={"parameters": {}}, headers=ADMIN)
        assert r.status_code == 409
        iid = inst["id"]
        assert client.post(f"/studio/installations/{iid}/approve",
                           headers=ADMIN).status_code == 200
        assert client.post(f"/studio/installations/{iid}/attach",
                           headers=ADMIN).status_code == 200
        r = client.post(f"/studio/installations/{iid}/enable",
                        json={"parameters": {}}, headers=ADMIN)
        assert r.status_code == 200, r.text
        out = r.json()
        manifest = _manifest(name)
        applied = {(a["kind"], a["key"]) for a in out["applied_configuration"]}
        declared = {(c["kind"], c["key"]) for c in manifest["configuration"]}
        assert applied == declared, (name, applied, declared)
        assert out["status"] == "enabled"


@pytest.fixture()
def all_enabled(client):
    for name in PACKS_15:
        _enable(client, name)
    _patient(client, "p15-patient")
    return client


def test_every_pack_order_set_places(all_enabled):
    sets = {s["key"]: s for s in
            all_enabled.get("/orders/sets", headers=CLINICIAN).json()["sets"]}
    for name in PACKS_15:
        manifest = _manifest(name)
        set_keys = [c["key"] for c in manifest["configuration"]
                    if c["kind"] == "orderset"]
        assert set_keys, f"{name} declares no order set"
        for key in set_keys:
            assert key in sets, f"{name}: order set {key} not published"
            r = all_enabled.post("/orders/from-set", json={
                "set": key, "patient": "p15-patient"}, headers=CLINICIAN)
            assert r.status_code == 201, (name, key, r.text)
            body = r.json()
            assert body["total"] >= 1
            assert all(o["status"] == "draft" for o in body["orders"])


def test_every_pack_form_validates(all_enabled):
    for name in PACKS_15:
        manifest = _manifest(name)
        form_keys = [c["key"] for c in manifest["configuration"]
                     if c["kind"] == "form"]
        assert form_keys, f"{name} declares no form"
        for key in form_keys:
            _submit_form(all_enabled, key, "p15-patient")


def test_every_pack_registry_is_listed(all_enabled):
    registries = {e["key"] for e in
                  all_enabled.get("/pophealth/registries",
                                  headers=CLINICIAN).json()}
    for name in PACKS_15:
        manifest = _manifest(name)
        cohort_keys = [c["key"] for c in manifest["configuration"]
                       if c["kind"] == "cohort"]
        assert cohort_keys, f"{name} declares no registry cohort"
        for key in cohort_keys:
            assert key in registries, f"{name}: registry {key} not listed"


def test_bd03_pack_versions_bumped(client):
    for name in PACKS_15[:7]:
        assert _manifest(name)["version"] == "1.1.0", name


# ---------------------------------------------------------------------------
# BD-03 deep: monitoring-hook rules fire (psychiatry deep-test style)
# ---------------------------------------------------------------------------

def test_cardiology_digoxin_block(client):
    _enable(client, "specialty-cardiology")
    _patient(client, "card-1")
    oid = _active_med_order(client, "card-1", "digoxin")

    # No monitoring labs on file -> BLOCKED, nothing dispensed.
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "Medication monitoring block" in r.json()["detail"]
    assert "Digoxin" in r.json()["detail"]

    # Potassium below the rule's minimum (3.5) -> still blocked.
    _observation(client, "card-1", "2823-3", 3.0, unit="mmol/L")
    _observation(client, "card-1", "10535-3", 1.0, unit="ng/mL")
    r = _dispense(client, oid)
    assert r.status_code == 409
    assert "below the required minimum" in r.json()["detail"]

    # Current in-range potassium + level -> dispenses, stock moves.
    _observation(client, "card-1", "2823-3", 4.4,
                 when=_now() + timedelta(hours=1), unit="mmol/L")
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 59  # 60 published by the pack - 1
    assert r.json()["monitoring_warnings"] == []


def test_diabetes_metformin_flag(client):
    _enable(client, "specialty-diabetes-endo")
    _patient(client, "diab-1")
    oid = _active_med_order(client, "diab-1", "847232")

    # No creatinine within 180 days: FLAG — dispenses with the warning.
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    warnings = r.json()["monitoring_warnings"]
    assert {w["lab_code"] for w in warnings} == {"2160-0"}

    # With a current creatinine, a second order dispenses clean.
    _observation(client, "diab-1", "2160-0", 1.0)
    oid2 = _active_med_order(client, "diab-1", "847232")
    r = _dispense(client, oid2)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []


def test_gastroenterology_methotrexate_block(client):
    _enable(client, "specialty-gastroenterology")
    _patient(client, "gi-1")
    oid = _active_med_order(client, "gi-1", "methotrexate")

    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "Methotrexate" in r.json()["detail"]

    # ALT on file but ANC below the 1.5 minimum -> still blocked.
    _observation(client, "gi-1", "1742-6", 22, unit="U/L")
    _observation(client, "gi-1", "751-8", 1.2, unit="10^3/uL")
    r = _dispense(client, oid)
    assert r.status_code == 409
    assert "below the required minimum" in r.json()["detail"]

    # In-range ANC (newer result) -> dispenses.
    _observation(client, "gi-1", "751-8", 2.6,
                 when=_now() + timedelta(hours=1), unit="10^3/uL")
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 39


def test_pulmonology_theophylline_block(client):
    _enable(client, "specialty-pulmonology")
    _patient(client, "pulm-1")
    oid = _active_med_order(client, "pulm-1", "theophylline")

    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "Theophylline" in r.json()["detail"]

    # A level above the 20 mg/L maximum -> still blocked.
    _observation(client, "pulm-1", "4049-8", 24.0, unit="mg/L")
    r = _dispense(client, oid)
    assert r.status_code == 409
    assert "above the allowed maximum" in r.json()["detail"]

    # An in-range level -> dispenses.
    _observation(client, "pulm-1", "4049-8", 12.0,
                 when=_now() + timedelta(hours=1), unit="mg/L")
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 39


def test_obgyn_labetalol_flag(client):
    _enable(client, "specialty-obgyn")
    _patient(client, "ob-1")
    oid = _active_med_order(client, "ob-1", "labetalol")

    # No BP / ALT on file: FLAG — dispenses with both warnings.
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    warnings = r.json()["monitoring_warnings"]
    assert {w["lab_code"] for w in warnings} == {"8480-6", "1742-6"}

    _observation(client, "ob-1", "8480-6", 128, unit="mmHg")
    _observation(client, "ob-1", "1742-6", 20, unit="U/L")
    oid2 = _active_med_order(client, "ob-1", "labetalol")
    r = _dispense(client, oid2)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []


def test_orthopedics_apixaban_flag(client):
    _enable(client, "specialty-orthopedics")
    _patient(client, "ortho-1")
    oid = _active_med_order(client, "ortho-1", "1351886")

    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    warnings = r.json()["monitoring_warnings"]
    assert {w["lab_code"] for w in warnings} == {"718-7", "2160-0"}

    _observation(client, "ortho-1", "718-7", 13.5, unit="g/dL")
    _observation(client, "ortho-1", "2160-0", 0.9)
    oid2 = _active_med_order(client, "ortho-1", "1351886")
    r = _dispense(client, oid2)
    assert r.status_code == 201, r.text
    assert r.json()["monitoring_warnings"] == []


def test_geriatrics_nitrofurantoin_dur_rule_opens_task(client):
    _enable(client, "specialty-geriatrics")
    # 76-year-old man, creatinine 3.0, 70 kg -> CrCl ~20.6 (< 30).
    _patient(client, "ger-1", birthDate="1950-06-01", gender="male")
    _observation(client, "ger-1", "2160-0", 3.0)
    _observation(client, "ger-1", "29463-7", 70, unit="kg")
    oid = _active_med_order(client, "ger-1", "nitrofurantoin")

    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    fired = {(f["event"], f["rule"]) for f in r.json()["rule_firings"]}
    assert ("pharmacy.dur.screened",
            "geriatrics-nitrofurantoin-renal-review") in fired
    tasks = client.get("/fhir/Task", headers=CLINICIAN).json()
    descriptions = [e["resource"].get("description", "")
                    for e in tasks.get("entry", [])]
    assert any("Geriatric prescribing review" in d for d in descriptions)

    # The rule is medication-specific: verifying a different drug for
    # the same low-CrCl patient does NOT fire the geriatrics rule.
    oid2 = _active_med_order(client, "ger-1", "1049221")
    r = client.post(f"/pharmacy/verification/{oid2}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    fired2 = {(f["event"], f["rule"]) for f in r.json()["rule_firings"]}
    assert ("pharmacy.dur.screened",
            "geriatrics-nitrofurantoin-renal-review") not in fired2


# ---------------------------------------------------------------------------
# BD-05 deep: transplant immunosuppression monitoring + donor linkage
# ---------------------------------------------------------------------------

def test_transplant_tacrolimus_block(client):
    _enable(client, "specialty-transplant")
    _patient(client, "tx-1")
    oid = _active_med_order(client, "tx-1", "tacrolimus")

    # No trough / creatinine / potassium on file -> BLOCKED.
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "Medication monitoring block" in r.json()["detail"]
    assert "Tacrolimus" in r.json()["detail"]

    # A supratherapeutic trough (> 20) -> still blocked.
    _observation(client, "tx-1", "77347-1", 25.0, unit="ng/mL")
    _observation(client, "tx-1", "2160-0", 1.1)
    _observation(client, "tx-1", "2823-3", 4.6, unit="mmol/L")
    r = _dispense(client, oid)
    assert r.status_code == 409
    assert "above the allowed maximum" in r.json()["detail"]

    # In-range trough (newer) -> dispenses, stock moves.
    _observation(client, "tx-1", "77347-1", 8.0,
                 when=_now() + timedelta(hours=1), unit="ng/mL")
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 39
    assert r.json()["monitoring_warnings"] == []


def test_transplant_donor_linkage_form_round_trip(client):
    _enable(client, "specialty-transplant")
    _patient(client, "tx-2")
    answers = [
        {"linkId": "donor-type",
         "answer": [{"valueCoding": {"code": "living-related"}}]},
        {"linkId": "donor-reference",
         "answer": [{"valueString": "DONOR-SYNTH-001"}]},
        {"linkId": "organ", "answer": [{"valueCoding": {"code": "kidney"}}]},
        {"linkId": "crossmatch-result",
         "answer": [{"valueCoding": {"code": "negative"}}]},
        {"linkId": "consent-confirmed", "answer": [{"valueBoolean": True}]},
        {"linkId": "linkage-verified-by",
         "answer": [{"valueString": "tx-coordinator-1"}]},
    ]
    r = client.post("/flowsheets", json={
        "form": "transplant-donor-linkage", "patient": "tx-2",
        "answers": answers}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    view = client.get("/flowsheets/transplant-donor-linkage",
                      params={"patient": "tx-2"}, headers=CLINICIAN).json()
    assert view["total"] == 1
    entry = view["entries"][0]["answers"]
    assert entry["donor-reference"]["value"] == "DONOR-SYNTH-001"
    assert entry["crossmatch-result"]["value"] == "negative"


# ---------------------------------------------------------------------------
# BD-06 deep: dialysis slot + flowsheet + adequacy labs on the lab spine
# ---------------------------------------------------------------------------

def test_dialysis_slot_and_flowsheet_round_trip(client):
    _enable(client, "specialty-dialysis")
    _patient(client, "pat-1")  # the dev portal patient

    # The slot type rides the published slot-types catalogue: the
    # portal self-scheduling surface now offers dialysis sessions...
    cfg = client.get("/portal/self-scheduling/config",
                     headers=PATIENT).json()
    assert "dialysis-session" in cfg["slot_types"]
    assert "telehealth" in cfg["slot_types"]  # Phase 9 append preserved

    # ...and a request books a 240-minute appointment (duration from
    # the slot-type concept, the telehealth pattern).
    r = client.post("/portal/appointments/request", json={
        "slot_type": "dialysis-session", "start": "2026-11-09T08:00:00Z"},
        headers=PATIENT)
    assert r.status_code == 201, r.text
    appt = r.json()["appointment"]
    start = datetime.fromisoformat(appt["start"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(appt["end"].replace("Z", "+00:00"))
    assert (end - start).total_seconds() == 240 * 60

    # Session flowsheet round-trip.
    _patient(client, "dia-1")
    answers = [
        {"linkId": "session-start",
         "answer": [{"valueDateTime": "2026-11-09T08:00:00Z"}]},
        {"linkId": "duration-minutes", "answer": [{"valueInteger": 240}]},
        {"linkId": "pre-weight-kg", "answer": [{"valueDecimal": 72.5}]},
        {"linkId": "post-weight-kg", "answer": [{"valueDecimal": 70.1}]},
        {"linkId": "intradialytic-hypotension",
         "answer": [{"valueBoolean": False}]},
    ]
    r = client.post("/flowsheets", json={
        "form": "dialysis-session-flowsheet", "patient": "dia-1",
        "answers": answers}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    view = client.get("/flowsheets/dialysis-session-flowsheet",
                      params={"patient": "dia-1"}, headers=CLINICIAN).json()
    assert view["total"] == 1
    entry = view["entries"][0]["answers"]
    assert entry["pre-weight-kg"]["value"] == 72.5
    assert entry["post-weight-kg"]["value"] == 70.1


def test_dialysis_adequacy_labs_place_on_lab_spine(client):
    _enable(client, "specialty-dialysis")
    _patient(client, "dia-2")
    manifest = _manifest("specialty-dialysis")
    adequacy = next(c for c in manifest["configuration"]
                    if c["key"] == "dialysis-adequacy-labs")
    codes = [o["code"] for o in adequacy["payload"]["orders"]]
    assert codes  # the set is not empty
    for code in codes:
        r = client.post("/lab/orders", json={
            "code": code, "patient": "dia-2"}, headers=CLINICIAN)
        assert r.status_code == 201, (code, r.text)


# ---------------------------------------------------------------------------
# BD-08 deep: paediatric monitoring + paediatric unit in the census
# ---------------------------------------------------------------------------

def test_paediatrics_gentamicin_block(client):
    _enable(client, "specialty-paediatrics")
    _patient(client, "paed-1")
    oid = _active_med_order(client, "paed-1", "gentamicin")

    # No trough / creatinine within 7 days -> BLOCKED.
    r = _dispense(client, oid)
    assert r.status_code == 409, r.text
    assert "Gentamicin" in r.json()["detail"]

    # Supratherapeutic trough (> 2.0 mg/L) -> still blocked.
    _observation(client, "paed-1", "35668-3", 3.1, unit="mg/L")
    _observation(client, "paed-1", "2160-0", 0.4)
    r = _dispense(client, oid)
    assert r.status_code == 409
    assert "above the allowed maximum" in r.json()["detail"]

    # In-range trough (newer) -> dispenses.
    _observation(client, "paed-1", "35668-3", 0.8,
                 when=_now() + timedelta(hours=1), unit="mg/L")
    r = _dispense(client, oid)
    assert r.status_code == 201, r.text
    assert r.json()["stock_after"] == 49


def test_paediatrics_unit_appears_in_census_when_enabled(client):
    # Before the pack: no paediatric unit in locations or census.
    before = client.get("/inpatient/census", headers=CLINICIAN).json()
    assert "unit-paeds" not in {u["code"] for u in before["units"]}
    assert before["totals"]["beds_total"] == 18  # 12 med-surg + 6 ICU

    _enable(client, "specialty-paediatrics")

    locations = client.get("/inpatient/locations",
                           headers=CLINICIAN).json()["locations"]
    by_code = {loc["code"]: loc for loc in locations}
    assert by_code["unit-paeds"]["type"] == "unit"
    paed_beds = [loc for loc in locations
                 if loc.get("parent") == "unit-paeds"]
    assert len(paed_beds) == 4

    # The census shows paediatric beds as their own unit class.
    census = client.get("/inpatient/census", headers=CLINICIAN).json()
    units = {u["code"]: u for u in census["units"]}
    assert units["unit-paeds"]["beds_total"] == 4
    assert units["unit-paeds"]["available"] == 4
    assert census["totals"]["beds_total"] == 22
