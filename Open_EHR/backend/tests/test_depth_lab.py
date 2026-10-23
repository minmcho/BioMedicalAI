"""Depth pass — laboratory completion (Workstream 3).

Proves the laboratory surfaces the parent audit found missing:
virology (qualitative NAAT results, viral-load trends, the
qualitative screen -> confirm reflex), serology + toxicology
benches, cytology on the pathology bench, send-outs / reference
laboratory, the POCT programme (device QC + operator competency on
the Phase 14 registry, enforcement config-flipped), delta-check
comparison windows, stewardship duplicate-order warnings, bench
worklists, and the post-analytical governance read surface.

All patients, devices and values are synthetic.
"""
from datetime import date, timedelta

from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

BMP = "51990-0"
CBC = "58410-2"
CREATININE = "2160-0"
POTASSIUM = "2823-3"
A1C = "4548-4"


# ---------------------------------------------------------------------------
# Helpers (mirroring tests/test_lab.py + test_phase6b_lab.py)
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["Depth"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, patient="pat-1", expect=201, **extra):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    body.update(extra)
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _transition(client, order_id, to_status, expect=200):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _to_received(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")


def _results(client, order_id, results, expect=201, **extra):
    body = {"results": results}
    body.update(extra)
    r = client.post(f"/lab/orders/{order_id}/results", json=body,
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _release_order(client, code, results, patient="pat-1"):
    """Place -> receive -> result -> verify -> release; returns
    (order, outcome)."""
    order = _order(client, code, patient=patient).json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"], results).json()
    report_id = outcome["report"]["id"]
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return order, outcome


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _catalog(client):
    return client.get("/lab/catalog", headers=CLINICIAN).json()["catalog"]


def _queue_item(client, report_id):
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    for item in queue["items"]:
        if item["report"]["id"] == report_id:
            return item
    return None


# ---------------------------------------------------------------------------
# 1. Seeding: the new benches + configs are published configuration
# ---------------------------------------------------------------------------

def test_new_benches_and_configs_seeded(client):
    catalog = _catalog(client)
    concepts = {c["code"]: c for c in catalog["concepts"]}
    # Virology
    assert concepts["94500-6"]["bench"] == "virology"
    assert concepts["94500-6"]["qualitative"] is True
    assert concepts["RVP-LOCAL"]["panel"] is True
    assert concepts["RVP-LOCAL"]["bench"] == "virology"
    assert concepts["20447-9"]["bench"] == "virology"
    # Serology + toxicology
    assert concepts["SER-HBSAG"]["bench"] == "serology"
    assert concepts["UDS-LOCAL"]["bench"] == "toxicology"
    assert set(concepts["UDS-LOCAL"]["components"]) == {
        "TOX-COC", "TOX-OPI", "TOX-THC", "TOX-AMP", "TOX-BZO"}
    # Cytology rides the pathology bench with its flag
    assert concepts["10524-7"]["bench"] == "pathology"
    assert concepts["10524-7"]["cytology"] is True
    # Send-outs
    assert concepts["SO-METANEPH"]["bench"] == "sendout"
    # Reflex rules: the numeric rule is untouched; the qualitative
    # HIV screen -> confirm rule joins it.
    rules = {r["id"]: r for r in catalog["reflexRules"]}
    assert rules["tsh-low-add-ft4"]["when"] == "below-low"
    assert rules["hiv-reactive-add-diff"]["when"] == "qualitative-match"
    assert rules["hiv-reactive-add-diff"]["matchValue"] == "Reactive"
    assert rules["hiv-reactive-add-diff"]["addCode"] == "HIV-DIFF"
    # Stewardship + delta windows are configuration
    assert catalog["stewardship"]["duplicateCheckEnabled"] is True
    assert catalog["stewardship"]["duplicateWindowHours"] == 24
    assert concepts[CREATININE]["delta"]["windowHours"] == 72
    assert "windowHours" not in concepts[POTASSIUM]["delta"]
    # Send-out laboratories + POCT programme are published configs
    labs = client.get("/studio/config/terminology/effective/sendout-labs",
                      headers=CLINICIAN)
    assert labs.status_code == 200, labs.text
    by_code = {l["code"]: l for l in labs.json()["payload"]["labs"]}
    assert by_code["ref-regional"]["tests"] == ["SO-METANEPH"]
    assert by_code["ref-specialty"]["tatHours"] == 240
    programme = client.get(
        "/studio/config/terminology/effective/poct-programme",
        headers=CLINICIAN)
    assert programme.status_code == 200, programme.text
    payload = programme.json()["payload"]
    assert payload["poctDeviceTypes"] == ["poct-analyser"]
    # Enforcement ships OFF (auto-verify precedent)
    assert payload["enforcement"] == {
        "blockUncompetentOperator": False, "blockMissingDailyQc": False}


# ---------------------------------------------------------------------------
# 2. Virology: NAAT results, the reflex, viral-load trends
# ---------------------------------------------------------------------------

def test_respiratory_panel_results_flow(client):
    _patient(client)
    order = _order(client, "RVP-LOCAL").json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"], [
        {"code": "94500-6", "value": "Not detected"},
        {"code": "VIR-INFA", "value": "Detected"},
        {"code": "VIR-INFB", "value": "Not detected"},
        {"code": "VIR-RSV", "value": "Not detected"},
    ]).json()
    assert outcome["critical"] is False
    assert _queue_item(client, outcome["report"]["id"]) is not None
    # A value outside the coded set is refused (typed validation)
    order2 = _order(client, "94500-6").json()
    _to_received(client, order2["id"])
    _results(client, order2["id"],
             [{"code": "94500-6", "value": "Positive-ish"}], expect=422)


def test_hiv_screen_reflex_creates_confirmation_order(client):
    _patient(client)
    order = _order(client, "HIV-AGAB").json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": "HIV-AGAB", "value": "Reactive"}]).json()
    assert len(outcome["reflex_orders"]) == 1
    reflex = client.get(f"/lab/orders/{outcome['reflex_orders'][0]}",
                        headers=CLINICIAN).json()
    codes = [c["code"] for c in
             reflex["order"]["code"]["coding"]]
    assert codes == ["HIV-DIFF"]
    # A non-reactive screen reflexes nothing
    order2 = _order(client, "HIV-AGAB").json()
    _to_received(client, order2["id"])
    outcome2 = _results(client, order2["id"],
                        [{"code": "HIV-AGAB", "value": "Nonreactive"}]).json()
    assert outcome2["reflex_orders"] == []


def test_viral_load_trend_counts_released_only(client):
    _patient(client)
    _release_order(client, "20447-9",
                   [{"code": "20447-9", "value": 85000}])
    _release_order(client, "20447-9",
                   [{"code": "20447-9", "value": 1200}])
    # A third result, entered but NOT released, must not appear
    order3 = _order(client, "20447-9").json()
    _to_received(client, order3["id"])
    _results(client, order3["id"], [{"code": "20447-9", "value": 60}])
    r = client.get("/lab/trends",
                   params={"patient_id": "pat-1", "code": "20447-9"},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 2
    assert [p["value"] for p in body["points"]] == [85000.0, 1200.0]
    assert body["points"][0]["log10"] == 4.929
    assert body["bench"] == "virology"
    # Unknown analyte / patient are honest errors
    assert client.get("/lab/trends",
                      params={"patient_id": "pat-1", "code": "NOPE"},
                      headers=CLINICIAN).status_code == 422
    assert client.get("/lab/trends",
                      params={"patient_id": "ghost", "code": "20447-9"},
                      headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# 3. Serology + toxicology benches
# ---------------------------------------------------------------------------

def test_serology_and_toxicology_benches(client):
    _patient(client)
    _release_order(client, "SER-HBSAG",
                   [{"code": "SER-HBSAG", "value": "Nonreactive"}])
    order = _order(client, "UDS-LOCAL").json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"], [
        {"code": "TOX-COC", "value": "Negative"},
        {"code": "TOX-OPI", "value": "Positive"},
        {"code": "TOX-THC", "value": "Negative"},
        {"code": "TOX-AMP", "value": "Negative"},
        {"code": "TOX-BZO", "value": "Negative"},
    ]).json()
    assert outcome["report"]["status"] == "preliminary"
    # Ethanol is the bench's numeric analyte
    _release_order(client, "TOX-ETOH",
                   [{"code": "TOX-ETOH", "value": 12}])


# ---------------------------------------------------------------------------
# 4. Bench worklists
# ---------------------------------------------------------------------------

def test_bench_worklist_filter(client):
    _patient(client)
    bmp = _order(client, BMP).json()
    cbc = _order(client, CBC).json()
    covid = _order(client, "94500-6").json()

    def ids(params):
        bundle = client.get("/lab/orders", params=params,
                            headers=CLINICIAN).json()
        return [e["resource"]["id"] for e in bundle["entry"]]

    assert ids({"bench": "virology"}) == [covid["id"]]
    assert ids({"bench": "chemistry"}) == [bmp["id"]]
    assert ids({"bench": "haematology"}) == [cbc["id"]]
    assert set(ids({})) == {bmp["id"], cbc["id"], covid["id"]}


# ---------------------------------------------------------------------------
# 5. Delta-check comparison window
# ---------------------------------------------------------------------------

def _creatinine_delta_flags(client, value):
    order = _order(client, CREATININE).json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": CREATININE, "value": value}]).json()
    item = _queue_item(client, outcome["report"]["id"])
    assert item is not None
    return [f for f in item["delta_flags"] if f["code"] == CREATININE]


def test_delta_window_seeded_flags_and_republish_unflags(client):
    _patient(client)
    # Baseline creatinine 1.0, released
    _release_order(client, CREATININE,
                   [{"code": CREATININE, "value": 1.0}])
    # +0.5 within the seeded 72h window, over the 0.3 absolute delta
    flags = _creatinine_delta_flags(client, 1.5)
    assert len(flags) == 1
    assert flags[0]["window_hours"] == 72
    assert flags[0]["previous_value"] == 1.0
    # Potassium has NO window in the seed: its pct delta still flags
    _release_order(client, POTASSIUM,
                   [{"code": POTASSIUM, "value": 4.0}])
    order = _order(client, POTASSIUM).json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": POTASSIUM, "value": 5.0}]).json()
    item = _queue_item(client, outcome["report"]["id"])
    k_flags = [f for f in item["delta_flags"] if f["code"] == POTASSIUM]
    assert len(k_flags) == 1 and k_flags[0]["window_hours"] is None
    # Republish the catalog with a ZERO-hour creatinine window: the
    # same change no longer flags (the baseline is outside the
    # window) — the window is configuration, not code.
    catalog = _catalog(client)
    for concept in catalog["concepts"]:
        if concept["code"] == CREATININE:
            concept["delta"] = {"absolute": 0.3, "windowHours": 0}
    _publish_config(client, "terminology", "lab-catalog", catalog)
    assert _creatinine_delta_flags(client, 1.9) == []


# ---------------------------------------------------------------------------
# 6. Governance surface + auto-verify flip
# ---------------------------------------------------------------------------

def test_governance_surface_reports_the_off_state(client):
    r = client.get("/lab/governance", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["auto_verify"]["enabled"] is False
    assert "DISABLED" in body["auto_verify"]["state_label"]
    rules = {rule["code"]: rule for rule in body["auto_verify"]["rules"]}
    assert rules["2345-7"]["low"] == 65.0 and rules["2345-7"]["high"] == 110.0
    assert {rule["id"] for rule in body["reflex_rules"]} == {
        "tsh-low-add-ft4", "hiv-reactive-add-diff"}
    deltas = {d["code"]: d for d in body["delta_checks"]}
    assert deltas[CREATININE]["config"]["windowHours"] == 72
    assert body["config"]["key"] == "lab-catalog"
    assert body["stewardship"]["duplicateWindowHours"] == 24
    # Patients never see the governance surface
    assert client.get("/lab/governance",
                      headers=PATIENT).status_code == 403


def test_auto_verify_flip_and_governance_readback(client):
    _patient(client)
    # Seeded OFF: an in-range glucose draft awaits a human
    order = _order(client, "2345-7").json()
    _to_received(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": "2345-7", "value": 100}]).json()
    assert outcome["auto_verified"] is False
    # Flip it ON by republishing the catalog
    catalog = _catalog(client)
    catalog["autoVerify"] = {"enabled": True}
    _publish_config(client, "terminology", "lab-catalog", catalog)
    order2 = _order(client, "2345-7").json()
    _to_received(client, order2["id"])
    outcome2 = _results(client, order2["id"],
                        [{"code": "2345-7", "value": 100}]).json()
    assert outcome2["auto_verified"] is True
    # Auto-verified is NOT released: verified, invisible until release
    assert outcome2["report"]["status"] == "final"
    detail = client.get(f"/lab/orders/{order2['id']}",
                        headers=CLINICIAN).json()
    assert detail["order"]["status"] == "verified"
    # The governance surface reads the activity back from the audit
    body = client.get("/lab/governance", headers=CLINICIAN).json()
    assert body["auto_verify"]["enabled"] is True
    recent = body["auto_verify"]["recent_auto_verified"]
    assert any(entry["order_id"] == order2["id"] for entry in recent)


# ---------------------------------------------------------------------------
# 7. Stewardship: duplicate-order warnings
# ---------------------------------------------------------------------------

def test_duplicate_order_warning_and_stewardship_switch(client):
    _patient(client)
    first = _order(client, BMP)
    assert "warnings" not in first.json()
    second = _order(client, BMP).json()
    warnings = second.get("warnings") or []
    assert len(warnings) == 1
    assert warnings[0]["type"] == "duplicate-order"
    assert warnings[0]["existing_order_id"] == first.json()["id"]
    # The stewardship worklist shows the pair
    body = client.get("/lab/stewardship/duplicates",
                      headers=CLINICIAN).json()
    assert body["total"] == 1
    assert body["groups"][0]["code"] == BMP
    assert len(body["groups"][0]["orders"]) == 2
    # Cancelling both clears the pair; a fresh order warns about
    # nothing once the earlier orders are finished
    _transition(client, first.json()["id"], "cancelled")
    _transition(client, second["id"], "cancelled")
    third = _order(client, BMP).json()
    assert "warnings" not in third
    # The check itself is configuration: publish it off and even an
    # active duplicate stays silent
    _order(client, BMP)
    catalog = _catalog(client)
    catalog["stewardship"] = {"duplicateCheckEnabled": False,
                              "duplicateWindowHours": 24}
    _publish_config(client, "terminology", "lab-catalog", catalog)
    fifth = _order(client, BMP).json()
    assert "warnings" not in fifth


# ---------------------------------------------------------------------------
# 8. Send-outs / reference laboratory
# ---------------------------------------------------------------------------

def _collected_sendout(client, code="SO-METANEPH"):
    order = _order(client, code).json()
    _transition(client, order["id"], "collected")
    return order


def test_sendout_full_lifecycle(client):
    _patient(client)
    order = _collected_sendout(client)
    queue = client.get("/lab/sendouts", headers=CLINICIAN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["sendout"]["status"] == "awaiting-dispatch"
    assert item["reference_lab"]["code"] == "ref-regional"
    assert item["reference_lab"]["tat_hours"] == 120
    # Ship it
    r = client.post("/lab/sendouts/dispatch",
                    json={"order_ids": [order["id"]],
                          "tracking_ref": "SYN-TRACK-1"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    manifest = r.json()["manifest"]
    assert manifest["lab"] == "ref-regional"
    assert manifest["orders"] == [order["id"]]
    detail = client.get(f"/lab/orders/{order['id']}",
                        headers=CLINICIAN).json()
    assert detail["order"]["status"] == "in-process"
    # The reference lab's result files as a draft for verification
    r = client.post(f"/lab/sendouts/{order['id']}/receive-result",
                    json={"results": [{"code": "SO-METANEPH", "value": 42}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["sendout"]["status"] == "resulted"
    assert _queue_item(client, r.json()["report"]["id"]) is not None
    # TAT by laboratory
    tat = client.get("/lab/sendouts/tat", headers=CLINICIAN).json()
    regional = [l for l in tat["laboratories"]
                if l["lab"] == "ref-regional"][0]
    assert regional["resulted"] == 1
    assert regional["within_tat_pct"] == 100.0
    assert regional["avg_hours"] is not None and regional["avg_hours"] < 1


def test_sendout_guards(client):
    _patient(client)
    # An in-house order cannot be dispatched or resulted as send-out
    bmp = _order(client, BMP).json()
    _transition(client, bmp["id"], "collected")
    r = client.post("/lab/sendouts/dispatch",
                    json={"order_ids": [bmp["id"]]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/sendouts/{bmp['id']}/receive-result",
                    json={"results": []}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # A send-out order cannot be resulted before dispatch
    order = _collected_sendout(client)
    r = client.post(f"/lab/sendouts/{order['id']}/receive-result",
                    json={"results": [{"code": "SO-METANEPH", "value": 1}]},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # Double dispatch conflicts
    client.post("/lab/sendouts/dispatch",
                json={"order_ids": [order["id"]]}, headers=CLINICIAN)
    r = client.post("/lab/sendouts/dispatch",
                    json={"order_ids": [order["id"]]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # A manifest never mixes reference laboratories
    other = _collected_sendout(client, code="SO-VLCFA")
    third = _collected_sendout(client)
    r = client.post("/lab/sendouts/dispatch",
                    json={"order_ids": [third["id"], other["id"]]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 9. Cytology
# ---------------------------------------------------------------------------

CYTO_ANSWERS = [
    {"linkId": link_id, "answer": [{"valueString": value}]}
    for link_id, value in {
        "specimenType": "Cervical smear (Pap)",
        "adequacy": "Satisfactory",
        "category": "Negative",
        "interpretation": "Negative for intraepithelial lesion or "
                          "malignancy",
    }.items()
]


def _pathology_to_histology(client, code):
    order = _order(client, code).json()
    for step in ("collected", "accessioned", "grossed", "histology"):
        _transition(client, order["id"], step)
    return order


def test_cytology_flow_and_bench_guards(client):
    _patient(client)
    form = client.get("/lab/pathology/cytology-form", headers=CLINICIAN)
    assert form.status_code == 200, form.text
    assert form.json()["form"]["title"] == "Cytology report"
    order = _pathology_to_histology(client, "10524-7")
    # Required items are enforced by the form machinery
    r = client.post(f"/lab/pathology/orders/{order['id']}/cytology-report",
                    json={"answers": CYTO_ANSWERS[:1]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # The surgical synoptic endpoint refuses a cytology case
    r = client.post(f"/lab/pathology/orders/{order['id']}/report",
                    json={"answers": []}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/pathology/orders/{order['id']}/cytology-report",
                    json={"answers": CYTO_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    categories = r.json()["report"]["category"][0]["coding"]
    assert categories[0]["code"] == "CP"
    assert r.json()["order"]["status"] == "reported"
    # Sign-out is the bench's shared endpoint
    r = client.post(f"/lab/pathology/reports/{report_id}/sign",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "signed-out"
    # And the cytology endpoint refuses a surgical case
    surgical = _pathology_to_histology(client, "11529-5")
    r = client.post(
        f"/lab/pathology/orders/{surgical['id']}/cytology-report",
        json={"answers": CYTO_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# 10. POCT programme: QC, competency, enforcement
# ---------------------------------------------------------------------------

def _poct_device(client, key="poc-glu-1"):
    r = client.post("/devices", json={
        "device_key": key, "device_type": "poct-analyser",
        "name": "Synthetic POCT glucose meter", "location": "Clinic"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    return key


def test_poct_qc_tracking(client):
    _poct_device(client)
    body = client.get("/lab/poct/devices", headers=CLINICIAN).json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["qc_current"] is False
    assert item["qc_today"] == {"L1": None, "L2": None}
    # Validation: unknown device 404, unpublished level 422
    r = client.post("/lab/poct/qc", json={
        "device_key": "ghost", "level": "L1", "result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 404, r.text
    r = client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L9", "result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # Both levels pass -> the device is QC-current; a later failure
    # supersedes the pass
    r = client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L1", "result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 201 and r.json()["qc_current"] is False
    r = client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L2", "result": "pass"},
        headers=CLINICIAN)
    assert r.json()["qc_current"] is True
    r = client.post("/lab/poct/qc", json={
        "device_key": "poc-glu-1", "level": "L1", "result": "fail"},
        headers=CLINICIAN)
    assert r.json()["qc_current"] is False
    listing = client.get("/lab/poct/qc", params={"device_key": "poc-glu-1"},
                         headers=CLINICIAN).json()
    assert listing["total"] == 3


def test_poct_competency_lifecycle(client):
    today = date.today()
    r = client.post("/lab/poct/competency", json={
        "staff_id": "staff-001", "test_code": A1C,
        "method": "Direct observation of testing",
        "assessed_on": today.isoformat()}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    record = r.json()["competency"]
    assert record["status"] == "current"
    # Expiry is the assessment date + the published 12-month window
    try:
        expected_expiry = today.replace(year=today.year + 1)
    except ValueError:  # 29 Feb -> 28 Feb next year
        expected_expiry = today.replace(year=today.year + 1, day=28)
    assert record["expires_on"] == expected_expiry.isoformat()
    # An assessment 13 months old is expired at read (computed,
    # never stored)
    old = today - timedelta(days=400)
    r = client.post("/lab/poct/competency", json={
        "staff_id": "staff-002", "test_code": A1C,
        "method": "Blind sample testing",
        "assessed_on": old.isoformat()}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["competency"]["status"] == "expired"
    # Typed validation: unknown test, future date, missing method
    for bad in ({"staff_id": "s", "test_code": "NOPE", "method": "x",
                 "assessed_on": today.isoformat()},
                {"staff_id": "s", "test_code": A1C, "method": "x",
                 "assessed_on": (today + timedelta(days=1)).isoformat()},
                {"staff_id": "s", "test_code": A1C,
                 "assessed_on": today.isoformat()}):
        r = client.post("/lab/poct/competency", json=bad, headers=CLINICIAN)
        assert r.status_code == 422, r.text
    # The overview surfaces the expired record honestly
    overview = client.get("/lab/poct/overview", headers=CLINICIAN).json()
    assert overview["enforcement"]["block_uncompetent_operator"] is False
    assert "TRACKING ONLY" in overview["enforcement"]["state_label"]
    assert any(c["staff_id"] == "staff-002"
               for c in overview["competency_expired"])


def _publish_poct_workflow(client):
    workflow = client.get("/lab/catalog", headers=CLINICIAN).json()["workflow"]
    _publish_config(client, "workflow", "lab-order-flow-poct", workflow)


def _poct_a1c_attempt(client, **extra):
    order = _order(client, A1C, poct=True).json()
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "received")
    body = {"results": [{"code": A1C, "value": 7.4}]}
    body.update(extra)
    return client.post(f"/lab/orders/{order['id']}/results", json=body,
                       headers=CLINICIAN)


def test_poct_enforcement_flips(client):
    _patient(client)
    _poct_device(client)
    _publish_poct_workflow(client)
    # Seeded OFF: entry succeeds with no competency and no QC
    r = _poct_a1c_attempt(client)
    assert r.status_code == 201, r.text
    assert r.json()["report"]["status"] == "final"
    # Publish enforcement ON (both blocks)
    programme = client.get(
        "/studio/config/terminology/effective/poct-programme",
        headers=ADMIN).json()["payload"]
    programme["enforcement"] = {"blockUncompetentOperator": True,
                                "blockMissingDailyQc": True}
    _publish_config(client, "terminology", "poct-programme", programme)
    # No competency + no device named -> refused before any write
    r = _poct_a1c_attempt(client)
    assert r.status_code == 409, r.text
    assert "competency" in r.json()["detail"]
    # Record competency for the entering operator (the dev clinician
    # signs in as username "dev-clinician", one of the operator
    # identities the competency check resolves)
    today = date.today()
    r = client.post("/lab/poct/competency", json={
        "staff_id": "dev-clinician", "test_code": A1C,
        "method": "Direct observation of testing",
        "assessed_on": today.isoformat()}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Competent but no device named -> the QC block demands one
    r = _poct_a1c_attempt(client)
    assert r.status_code == 422, r.text
    # Device named but QC not run -> 409
    r = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 409, r.text
    assert "QC" in r.json()["detail"]
    # Run the day's QC at both levels -> entry succeeds, and the
    # report carries the device as its instrument
    for level in ("L1", "L2"):
        client.post("/lab/poct/qc", json={
            "device_key": "poc-glu-1", "level": level, "result": "pass"},
            headers=CLINICIAN)
    r = _poct_a1c_attempt(client, device_key="poc-glu-1")
    assert r.status_code == 201, r.text
    exts = r.json()["report"].get("extension") or []
    assert any(e.get("url") == "urn:opencare:extension/instrument"
               and e.get("valueString") == "poc-glu-1" for e in exts)
    # Flip enforcement back OFF -> entry is free again
    programme["enforcement"] = {"blockUncompetentOperator": False,
                                "blockMissingDailyQc": False}
    _publish_config(client, "terminology", "poct-programme", programme)
    r = _poct_a1c_attempt(client)
    assert r.status_code == 201, r.text


def test_poct_overview_devices_due_qc(client):
    _poct_device(client)
    overview = client.get("/lab/poct/overview", headers=CLINICIAN).json()
    assert overview["devices_total"] == 1
    assert overview["devices_due_qc"] == ["poc-glu-1"]
    assert overview["programme_configured"] is True
