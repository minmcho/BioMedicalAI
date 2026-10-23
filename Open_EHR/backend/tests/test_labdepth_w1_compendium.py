"""Lab Depth W1 — D1 compendium/directory machinery + D2 benches as
organisational units.

D1: per-test directory attributes ON the published catalogue
(terminology/lab-catalog) — specimen requirements ENFORCED at order
placement (named refusals, zero partial writes), availability + TAT
targets (declared clocks) round-tripping through publish validation,
and age/sex-partitioned reference ranges selected by interpret_value
with fallback to the default range.

D2: the bench registry (terminology/lab-benches), bench-scoped
grants on the shared privileging ledger (lab.bench.<code>), the
bench workload view and shift handover.

Synthetic data only. All fixture dates derive from the UTC date.
The compendium router is registered here (not yet in app.main) —
the coordinator wires main.py from this workstream's integration
note; ensure defaults are invoked the same way main.py will.
"""
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import lab_compendium
from app.lab import interpret_value, select_reference_range
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

GLUCOSE = "2345-7"
HB = "718-7"
CREATININE = "2160-0"
BMP = "51990-0"
CBC = "58410-2"
HIV_VL = "20447-9"
SENDOUT = "SO-METANEPH"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/w1.db")
    app.include_router(lab_compendium.router)
    with app.state.session_factory() as session:
        lab_compendium.ensure_lab_compendium_defaults(session)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1", **demographics):
    body = {"resourceType": "Patient", "id": pid,
            "name": [{"family": "Synthetic", "given": ["W1"]}]}
    body.update(demographics)
    r = client.post("/fhir/Patient", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _adult_birthdate(years=40):
    today = datetime.now(timezone.utc).date()
    try:
        return today.replace(year=today.year - years).isoformat()
    except ValueError:  # Feb 29 -> Feb 28 in a non-leap target year
        return today.replace(year=today.year - years, day=28).isoformat()


def _order(client, code=GLUCOSE, patient="pat-1", expect=201, **extra):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    body.update(extra)
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _order_count(client, patient="pat-1"):
    r = client.get(f"/lab/orders?patient={patient}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["total"]


def _effective_catalog(client):
    r = client.get("/studio/config/terminology/effective/lab-catalog",
                   headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()["payload"]


def _publish(client, payload, expect=201):
    r = client.post("/studio/config/terminology/lab-catalog",
                    json=payload, headers=ADMIN)
    assert r.status_code == expect, r.text
    if expect != 201:
        return r
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/lab-catalog/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return r


def _concept(payload, code):
    return next(c for c in payload["concepts"] if c["code"] == code)


def _grant(client, bench="chemistry", staff_id="dev-clinician", expect=201):
    r = client.post(f"/lab/compendium/benches/{bench}/grants",
                    json={"staff_id": staff_id}, headers=ADMIN)
    assert r.status_code == expect, r.text
    return r.json()


# ---------------------------------------------------------------------------
# D1 — seeded directory attributes
# ---------------------------------------------------------------------------

def test_directory_seeded_on_catalogue(client):
    r = client.get("/lab/compendium/directory", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    tests = {t["code"]: t for t in r.json()["tests"]}
    glucose = tests[GLUCOSE]["directory"]
    assert glucose["performingSection"] == "Chemistry"
    assert glucose["performingSite"] == "Central laboratory"
    assert glucose["method"] == "Hexokinase (enzymatic)"
    assert glucose["specimen"]["container"] == "Serum separator tube"
    assert glucose["specimen"]["specimenType"] == "Serum"
    assert glucose["specimen"]["minimumVolumeMl"] == 3.5
    assert glucose["availability"] == {"mode": "24-7"}
    targets = {(t["priority"], t["clock"]): t["targetMinutes"]
               for t in glucose["tatTargets"]}
    assert targets[("stat", "order-to-result")] == 60
    assert targets[("routine", "order-to-result")] == 240
    assert targets[("routine", "receipt-to-result")] == 180
    # Batched + send-out availability shapes.
    vl = tests[HIV_VL]["directory"]["availability"]
    assert vl["mode"] == "batched"
    assert vl["batchSchedule"]["days"] == ["mon", "wed", "fri"]
    assert vl["batchSchedule"]["times"] == ["08:00"]
    so = tests[SENDOUT]["directory"]["availability"]
    assert so["mode"] == "send-out"
    assert so["referenceLab"] == "Regional reference laboratory"
    # Partitioned ranges ride the directory read model.
    hb = tests[HB]
    assert hb["reference_range"]["low"] == 12.0
    labels = {p["label"] for p in hb["reference_range_partitions"]}
    assert labels == {"Adult male", "Adult female"}


def test_directory_detail_and_unknown_code(client):
    r = client.get(f"/lab/compendium/directory/{BMP}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["panel"] is True
    assert {c["code"] for c in entry["component_entries"]} == {
        GLUCOSE, "3094-0", CREATININE, "2951-2", "2823-3"}
    r = client.get("/lab/compendium/directory/NOPE-1", headers=CLINICIAN)
    assert r.status_code == 404
    assert "NOPE-1" in r.json()["detail"]


# ---------------------------------------------------------------------------
# D1 — order-placement enforcement of SET specimen requirements
# ---------------------------------------------------------------------------

def test_order_refused_wrong_container_zero_writes(client):
    _patient(client)
    before = _order_count(client)
    r = _order(client, expect=422,
               specimen={"container": "EDTA tube (lavender top)"})
    detail = r.json()["detail"]
    assert "specimen container" in detail
    assert "Serum separator tube" in detail
    assert _order_count(client) == before


def test_order_refused_wrong_specimen_type_zero_writes(client):
    _patient(client)
    before = _order_count(client)
    r = _order(client, expect=422, specimen={"specimenType": "Urine"})
    assert "specimen type" in r.json()["detail"]
    assert _order_count(client) == before


def test_order_refused_insufficient_volume_zero_writes(client):
    _patient(client)
    before = _order_count(client)
    r = _order(client, expect=422, specimen={"volumeMl": 1.0})
    detail = r.json()["detail"]
    assert "specimen volume" in detail and "3.5" in detail
    assert _order_count(client) == before


def test_order_refused_nonfasting_when_fasting_required(client):
    payload = _effective_catalog(client)
    _concept(payload, GLUCOSE)["directory"]["specimen"]["fastingRequired"] \
        = True
    _publish(client, payload)
    _patient(client)
    before = _order_count(client)
    r = _order(client, expect=422, specimen={"fasting": False})
    assert "fasting" in r.json()["detail"]
    assert _order_count(client) == before
    # A fasting specimen declared honestly places fine.
    _order(client, specimen={"fasting": True})
    assert _order_count(client) == before + 1


def test_order_panel_components_enforced(client):
    """A panel order is checked against the panel AND its components:
    a volume below every component's minimum refuses, naming the
    requirement, and writes nothing."""
    _patient(client)
    before = _order_count(client)
    r = _order(client, code=BMP, expect=422, specimen={"volumeMl": 0.5})
    assert "specimen volume" in r.json()["detail"]
    assert _order_count(client) == before


def test_order_declared_compliant_specimen_places(client):
    _patient(client)
    _order(client, specimen={"container": "Serum separator tube",
                             "specimenType": "Serum", "volumeMl": 5.0})
    assert _order_count(client) == 1


def test_order_undeclared_specimen_not_judged(client):
    """Design position: enforcement judges DECLARED specimen
    attributes against SET requirements. An order that declares no
    specimen block is not refused — collection owns it downstream."""
    _patient(client)
    _order(client)
    assert _order_count(client) == 1


def test_order_unset_requirement_does_not_block(client):
    """Removing the specimen requirements from the published
    directory leaves nothing to enforce: even a contradictory
    declaration places."""
    payload = _effective_catalog(client)
    _concept(payload, GLUCOSE)["directory"].pop("specimen")
    _publish(client, payload)
    _patient(client)
    _order(client, specimen={"container": "Anything", "volumeMl": 0.1,
                             "specimenType": "Urine"})
    assert _order_count(client) == 1


# ---------------------------------------------------------------------------
# D1 — availability / TAT payload round-trip through publish validation
# ---------------------------------------------------------------------------

def test_availability_and_tat_round_trip_through_publish(client):
    payload = _effective_catalog(client)
    directory = _concept(payload, HIV_VL)["directory"]
    directory["availability"] = {
        "mode": "batched",
        "batchSchedule": {"days": ["tue", "thu"], "times": ["09:30"]}}
    directory["tatTargets"] = [
        {"priority": "routine", "clock": "order-to-result",
         "targetMinutes": 5000},
        {"priority": "routine", "clock": "receipt-to-result",
         "targetMinutes": 4500}]
    _publish(client, payload)
    r = client.get(f"/lab/compendium/directory/{HIV_VL}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    got = r.json()["directory"]
    assert got["availability"]["batchSchedule"]["times"] == ["09:30"]
    targets = {(t["priority"], t["clock"]): t["targetMinutes"]
               for t in got["tatTargets"]}
    assert targets == {("routine", "order-to-result"): 5000,
                       ("routine", "receipt-to-result"): 4500}


@pytest.mark.parametrize("mutate", [
    lambda d: d.update({"availability": {"mode": "sometimes"}}),
    lambda d: d.update({"availability": {"mode": "batched"}}),
    lambda d: d.update({"availability": {"mode": "send-out"}}),
    lambda d: d.update({"tatTargets": [
        {"priority": "routine", "clock": "whenever", "targetMinutes": 60}]}),
    lambda d: d.update({"tatTargets": [
        {"priority": "routine", "clock": "order-to-result",
         "targetMinutes": 60},
        {"priority": "routine", "clock": "order-to-result",
         "targetMinutes": 90}]}),
    lambda d: d.update({"tatTargets": [
        {"priority": "routine", "clock": "order-to-result",
         "targetMinutes": -5}]}),
])
def test_directory_validation_refusals(client, mutate):
    payload = _effective_catalog(client)
    mutate(_concept(payload, GLUCOSE)["directory"])
    r = client.post("/studio/config/terminology/lab-catalog",
                    json=payload, headers=ADMIN)
    assert r.status_code == 422, r.text


def test_partition_validation_refusal(client):
    payload = _effective_catalog(client)
    _concept(payload, HB)["referenceRangePartitions"] = [
        {"label": "Bad", "sex": "male", "ageMinYears": 50,
         "ageMaxYears": 18, "low": 1.0, "high": 2.0}]
    r = client.post("/studio/config/terminology/lab-catalog",
                    json=payload, headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "ageMinYears" in r.json()["detail"]


# ---------------------------------------------------------------------------
# D1 — partitioned reference ranges
# ---------------------------------------------------------------------------

def test_partition_selection_unit():
    hb = {"code": HB, "referenceRange": {"low": 12.0, "high": 15.5,
                                        "criticalLow": 7.0,
                                        "criticalHigh": 20.0},
          "referenceRangePartitions": [
              {"label": "Adult male", "sex": "male", "ageMinYears": 18,
               "low": 13.0, "high": 17.0},
              {"label": "Adult female", "sex": "female",
               "ageMinYears": 18, "low": 12.0, "high": 15.5}]}
    male = {"age_years": 40, "sex": "male"}
    female = {"age_years": 40, "sex": "female"}
    # 12.5 g/dL: low for an adult male, normal for an adult female,
    # normal against the default (fallback) range.
    assert interpret_value(12.5, hb, male) == "L"
    assert interpret_value(12.5, hb, female) == "N"
    assert interpret_value(12.5, hb) == "N"
    assert interpret_value(12.5, hb, {"age_years": None,
                                     "sex": None}) == "N"
    # A child matches no adult partition -> default range.
    assert interpret_value(12.5, hb, {"age_years": 9, "sex": "male"}) == "N"
    # Partition-omitted critical thresholds inherit from the default.
    assert interpret_value(6.0, hb, male) == "LL"
    rng = select_reference_range(hb, male)
    assert rng["low"] == 13.0 and rng["criticalLow"] == 7.0
    assert rng["partition"] == "Adult male"
    # No partitions at all -> behaviour unchanged.
    plain = {"code": "X", "referenceRange": {"low": 1.0, "high": 2.0}}
    assert interpret_value(0.5, plain, male) == "L"
    assert select_reference_range(plain, male) == {"low": 1.0, "high": 2.0}


def _result_hb(client, pid, value=12.5):
    _order(client, code=HB, patient=pid)
    orders = client.get(f"/lab/orders?patient={pid}",
                        headers=CLINICIAN).json()["entry"]
    order_id = orders[0]["resource"]["id"]
    for to_status in ("collected", "received"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": to_status}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": [{"code": HB, "value": value}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["observations"][0]


def test_partition_applies_end_to_end_male(client):
    _patient(client, pid="pat-m", gender="male",
             birthDate=_adult_birthdate(40))
    obs = _result_hb(client, "pat-m")
    coding = obs["interpretation"][0]["coding"][0]
    assert coding["code"] == "L"  # 12.5 is low against 13.0-17.0
    assert obs["referenceRange"][0]["low"]["value"] == 13.0


def test_partition_applies_end_to_end_female_and_fallback(client):
    _patient(client, pid="pat-f", gender="female",
             birthDate=_adult_birthdate(40))
    obs = _result_hb(client, "pat-f")
    assert obs["interpretation"][0]["coding"][0]["code"] == "N"
    assert obs["referenceRange"][0]["low"]["value"] == 12.0
    # No demographics on the chart -> default range, pre-D1 behaviour.
    _patient(client, pid="pat-x")
    obs = _result_hb(client, "pat-x")
    assert obs["interpretation"][0]["coding"][0]["code"] == "N"
    assert obs["referenceRange"][0]["low"]["value"] == 12.0


# ---------------------------------------------------------------------------
# D2 — bench registry, grants, workload, handover
# ---------------------------------------------------------------------------

def test_bench_registry_seeded(client):
    r = client.get("/lab/compendium/benches", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    benches = {b["code"]: b for b in r.json()["benches"]}
    assert {"chemistry", "haematology", "microbiology", "blood-bank",
            "pathology", "sendout"} <= set(benches)
    chem = benches["chemistry"]
    assert chem["name"] == "Chemistry bench"
    assert chem["section"] == "Core laboratory"
    assert chem["location"] == "facility-main"
    assert chem["supervisor_staff_id"] == "staff-201"
    assert chem["hours"]["opensAt"] == "00:00"
    assert chem["grant_code"] == "lab.bench.chemistry"
    assert chem["caller_has_grant"] is False
    assert chem["grants"] == []


def test_bench_workload_requires_bench_grant(client):
    r = client.get("/lab/compendium/benches/chemistry/workload",
                   headers=CLINICIAN)
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    assert "lab.bench.chemistry" in detail and "dev-clinician" in detail
    # A grant for a DIFFERENT bench does not open this one.
    _grant(client, bench="haematology")
    r = client.get("/lab/compendium/benches/chemistry/workload",
                   headers=CLINICIAN)
    assert r.status_code == 403
    # The bench's own grant opens it.
    _grant(client, bench="chemistry")
    r = client.get("/lab/compendium/benches/chemistry/workload",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["open_total"] == 0


def test_bench_workload_counts(client):
    _grant(client, bench="chemistry")
    _grant(client, bench="haematology")
    _patient(client)
    _order(client, code=GLUCOSE)
    _order(client, code=BMP)
    _order(client, code=CBC)
    chem = client.get("/lab/compendium/benches/chemistry/workload",
                      headers=CLINICIAN).json()
    assert chem["counts_by_status"] == {"ordered": 2}
    assert chem["open_total"] == 2
    assert {i["code"] for i in chem["open_items"]} == {GLUCOSE, BMP}
    haem = client.get("/lab/compendium/benches/haematology/workload",
                      headers=CLINICIAN).json()
    assert haem["counts_by_status"] == {"ordered": 1}
    assert haem["open_total"] == 1


def test_bench_unknown_bench_404(client):
    r = client.get("/lab/compendium/benches/nope/workload",
                   headers=CLINICIAN)
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


def test_bench_grant_revoke_restores_refusal(client):
    grant = _grant(client, bench="chemistry")
    assert grant["privilege"] == "lab.bench.chemistry"
    assert grant["service_line"] == "laboratory"
    r = client.post(
        f"/lab/compendium/benches/chemistry/grants/{grant['grant_id']}/revoke",
        json={"reason": "rotation ended"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "revoked"
    r = client.get("/lab/compendium/benches/chemistry/workload",
                   headers=CLINICIAN)
    assert r.status_code == 403


def test_bench_grant_admin_only(client):
    r = client.post("/lab/compendium/benches/chemistry/grants",
                    json={"staff_id": "dev-clinician"}, headers=CLINICIAN)
    assert r.status_code == 403
    r = client.post("/lab/compendium/benches/chemistry/grants",
                    json={"staff_id": "dev-clinician"}, headers=CONFIG)
    assert r.status_code == 201


def test_handover_flow(client):
    _patient(client)
    _order(client, code=GLUCOSE)
    # Creating a note is bench work: no grant -> named refusal.
    r = client.post("/lab/compendium/benches/chemistry/handover",
                    json={"note": "Two stat samples pending"},
                    headers=CLINICIAN)
    assert r.status_code == 403
    assert "lab.bench.chemistry" in r.json()["detail"]
    _grant(client, bench="chemistry")
    # Empty note refuses (server validation, named).
    r = client.post("/lab/compendium/benches/chemistry/handover",
                    json={"note": "  "}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/lab/compendium/benches/chemistry/handover",
                    json={"note": "Two stat samples pending"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    note = r.json()
    assert note["status"] == "open" and note["author"] == "dev-clinician"
    # Handover list: open items + the note.
    r = client.get("/lab/compendium/benches/chemistry/handover",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    view = r.json()
    assert view["open_total"] == 1
    assert view["open_notes"] == 1
    assert view["notes"][0]["note_id"] == note["note_id"]
    # Acknowledge is a bench work action on the same grant.
    r = client.post(
        f"/lab/compendium/benches/chemistry/handover/"
        f"{note['note_id']}/acknowledge",
        json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "acknowledged"
    assert r.json()["acknowledged_by"] == "dev-clinician"
    # Second acknowledgement refuses by name.
    r = client.post(
        f"/lab/compendium/benches/chemistry/handover/"
        f"{note['note_id']}/acknowledge",
        json={}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "already acknowledged" in r.json()["detail"]
    # A note id on the wrong bench is a 404.
    r = client.post(
        f"/lab/compendium/benches/haematology/handover/"
        f"{note['note_id']}/acknowledge",
        json={}, headers=ADMIN)
    assert r.status_code == 404


def test_handover_list_requires_bench_grant(client):
    r = client.get("/lab/compendium/benches/chemistry/handover",
                   headers=VIEWER)
    assert r.status_code == 403  # viewer holds lab.read but no bench grant
    r = client.get("/lab/compendium/benches/chemistry/handover",
                   headers=ADMIN)
    assert r.status_code == 200  # administrators pass the bench gate
