"""LAB-D12 (Lab Depth pass, W6) — microbiology workup depth.

Structured Gram stain + blood-culture critical call, plating record
gate, colony workup, multiple isolates with ID method/confidence,
AST computed from governed breakpoint tables (standard + VERSION
stamped — the same MIC reads differently under two versions),
expert rules with named traces, selective reporting (released vs
lab view), the CLSI M39 antibiogram (first-isolate dedup,
minimum-count suppression) and the LAB-m3 MDRO alert into Infection
Prevention.

The depth router is included on the test app directly: main.py
wiring is the coordinator's step (see the W6 INTEGRATION NOTE), and
including the router here exercises exactly the module the
coordinator will wire. Governed content (breakpoints, rules,
organism MDRO flags) is published through the real Studio API —
nothing is seeded behind the tests' backs.

Synthetic data only."""
import pytest
from fastapi.testclient import TestClient

from app import lab_micro
from app.main import create_app
from tests.conftest import ADMIN, CLINICIAN

BLOOD_CULTURE = "87040-1"
URINE_CULTURE = "87086-6"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    app.include_router(lab_micro.router)
    with TestClient(app) as c:
        yield c


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["W6"]}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _order(client, code=BLOOD_CULTURE, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, order_id, to_status, expect=200):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _plating(client, order_id, culture_type="blood", expect=201):
    r = client.post(f"/lab/micro-depth/orders/{order_id}/plating", json={
        "media": ["blood-agar", "chocolate-agar"],
        "streak_pattern": "four-quadrant",
        "incubation": {"temperature_c": 35, "atmosphere": "co2",
                       "duration_hours": 24},
        "culture_type": culture_type}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _collected(client, code=BLOOD_CULTURE, patient="pat-1"):
    _patient(client, patient)
    order = _order(client, code, patient)
    _transition(client, order["id"], "collected")
    return order


def _gram(client, order_id, morphology="gram-negative-rods", expect=201):
    r = client.post(f"/lab/micro-depth/orders/{order_id}/gram-stain",
                    json={"morphology": morphology, "quantity": "moderate",
                          "wbc": "10-25", "epithelial_cells": "<10",
                          "adequacy": "adequate"}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _isolate(client, order_id, organism="escherichia-coli",
             method="maldi-tof", confidence=98.0, expect=201):
    payload = {"organism_code": organism, "id_method": method}
    if confidence is not None:
        payload["id_confidence"] = confidence
    r = client.post(f"/lab/micro-depth/orders/{order_id}/isolates",
                    json=payload, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()["isolate_id"]


def _publish_breakpoints(client, tables):
    return _publish(client, "terminology", "micro-breakpoints", {
        "name": "Test breakpoints",
        "concepts": [{"code": "CLSI", "display": "CLSI M100",
                      "active": True}],
        "tables": tables})


def _cipro_table(version, s_max, r_min):
    return {"standard": "CLSI", "version": version, "entries": [
        {"organism": "escherichia-coli", "antibiotic": "ciprofloxacin",
         "mic_s_max": s_max, "mic_r_min": r_min},
        {"organism": "escherichia-coli", "antibiotic": "meropenem",
         "mic_s_max": 1, "mic_r_min": 4},
        {"organism": "staphylococcus-aureus", "antibiotic": "oxacillin",
         "mic_s_max": 2, "mic_r_min": 4},
        {"organism": "staphylococcus-aureus", "antibiotic": "vancomycin",
         "mic_s_max": 2, "mic_r_min": 16}]}


def _ast(client, order_id, isolate_id, results, expect=201):
    r = client.post(
        f"/lab/micro-depth/orders/{order_id}/isolates/{isolate_id}/ast",
        json={"results": results}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _workup(client, order_id, view="lab"):
    r = client.get(f"/lab/micro-depth/orders/{order_id}/workup",
                   params={"view": view}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()["workup"]


def _finalize(client, order_id, expect=200):
    r = client.post(f"/lab/micro/orders/{order_id}/finalize",
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _full_culture(client, patient="pat-1", organism="escherichia-coli",
                  ast_results=None):
    """Drive one blood culture through the whole depth workup to
    final; returns the order id."""
    order = _collected(client, BLOOD_CULTURE, patient)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"], organism)
    if ast_results:
        _ast(client, order["id"], isolate, ast_results)
    _finalize(client, order["id"])
    return order["id"]


# ---------------------------------------------------------------------------
# Gram stain: structured validation + critical call
# ---------------------------------------------------------------------------

def test_gram_stain_structured_validation(client):
    order = _collected(client)
    _plating(client, order["id"])
    # morphology is a governed enum, not free text
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/gram-stain",
                    json={"morphology": "some rods I saw",
                          "quantity": "moderate", "wbc": "10-25",
                          "epithelial_cells": "<10",
                          "adequacy": "adequate"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # adequacy is required structured content, not an afterthought
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/gram-stain",
                    json={"morphology": "gram-negative-rods",
                          "quantity": "moderate", "wbc": "10-25",
                          "epithelial_cells": "<10"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = _gram(client, order["id"])
    workup = r.json()["workup"]
    gram = workup["gram_stain"]
    assert gram["morphology"] == "gram-negative-rods"
    assert gram["wbc"] == "10-25"
    assert gram["epithelial_cells"] == "<10"
    assert gram["adequacy"] == "adequate"
    assert gram["organisms_present"] is True


def test_positive_blood_culture_gram_opens_critical_call_requiring_readback(client):
    order = _collected(client)
    _plating(client, order["id"], culture_type="blood")
    r = _gram(client, order["id"])
    assert r.json()["critical_call_opened"] is True
    call = r.json()["workup"]["critical_call"]
    assert call["state"] == "pending"
    # notification without read-back is refused and records nothing
    r = client.post(
        f"/lab/micro-depth/orders/{order['id']}/gram-stain/notify",
        json={"notified_to": "Dr Synthetic", "read_back": False},
        headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert _workup(client, order["id"])["critical_call"]["state"] == "pending"
    # notification without a named receiver is refused too
    r = client.post(
        f"/lab/micro-depth/orders/{order['id']}/gram-stain/notify",
        json={"read_back": True}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # acknowledge before notification -> 409
    r = client.post(
        f"/lab/micro-depth/orders/{order['id']}/gram-stain/acknowledge",
        headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # the real call: notified_to + read_back
    r = client.post(
        f"/lab/micro-depth/orders/{order['id']}/gram-stain/notify",
        json={"notified_to": "Dr Synthetic", "read_back": True},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    call = r.json()["workup"]["critical_call"]
    assert call["state"] == "notified"
    assert call["notified_to"] == "Dr Synthetic"
    assert call["read_back"] is True
    r = client.post(
        f"/lab/micro-depth/orders/{order['id']}/gram-stain/acknowledge",
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["workup"]["critical_call"]["state"] == "acknowledged"


def test_negative_gram_stain_opens_no_critical_call(client):
    order = _collected(client)
    _plating(client, order["id"], culture_type="blood")
    r = _gram(client, order["id"], morphology="none-seen")
    assert r.json()["critical_call_opened"] is False
    assert r.json()["workup"]["critical_call"] is None


# ---------------------------------------------------------------------------
# Plating gate
# ---------------------------------------------------------------------------

def test_workup_refuses_to_advance_without_plating_record(client):
    order = _collected(client)
    # a bare status flip to plated is NOT a plating record
    _transition(client, order["id"], "plated")
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/gram-stain",
                    json={"morphology": "gram-negative-rods",
                          "quantity": "few", "wbc": "none",
                          "epithelial_cells": "none",
                          "adequacy": "adequate"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "plating record" in r.json()["detail"]
    # zero partial write: no gram stain was recorded
    assert _workup(client, order["id"])["gram_stain"] is None
    # recording plating now unblocks the workup
    _plating(client, order["id"])
    _gram(client, order["id"])


def test_plating_record_validation_and_status(client):
    order = _collected(client)
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/plating",
                    json={"media": ["pizza-agar"],
                          "streak_pattern": "four-quadrant",
                          "incubation": {"temperature_c": 35,
                                         "atmosphere": "co2",
                                         "duration_hours": 24}},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = _plating(client, order["id"])
    assert r.json()["order"]["status"] == "plated"
    plating = r.json()["workup"]["plating"]
    assert plating["media"] == ["blood-agar", "chocolate-agar"]
    assert plating["incubation"]["atmosphere"] == "co2"
    assert plating["started_at"] is not None


# ---------------------------------------------------------------------------
# Colony workup
# ---------------------------------------------------------------------------

def test_colony_quantitation_urine_bands_and_blood_bottle(client):
    order = _collected(client, URINE_CULTURE)
    _plating(client, order["id"], culture_type="urine")
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/colony",
                    json={"cfu_ml": 150000}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    colony = r.json()["workup"]["colony"]
    assert colony["cfu_ml"] == 150000
    assert colony["cfu_band"].startswith(">=100,000")

    order = _collected(client, BLOOD_CULTURE, patient="pat-2")
    _plating(client, order["id"], culture_type="blood")
    # blood cultures require the bottle set + time-to-positivity
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/colony",
                    json={"growth_amount": "moderate"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/colony",
                    json={"growth_amount": "moderate",
                          "bottle": {"set_id": "SET-SYN-1",
                                     "bottle_type": "aerobic",
                                     "time_to_positivity_hours": 11.5}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    bottle = r.json()["workup"]["colony"]["bottle"]
    assert bottle["set_id"] == "SET-SYN-1"
    assert bottle["time_to_positivity_hours"] == 11.5


# ---------------------------------------------------------------------------
# Isolates: multiple, ID method + confidence
# ---------------------------------------------------------------------------

def test_multiple_isolates_with_independent_ast(client):
    _publish_breakpoints(client, [_cipro_table("M100-34", 0.25, 2)])
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    first = _isolate(client, order["id"], "escherichia-coli")
    second = _isolate(client, order["id"], "staphylococcus-aureus",
                      method="biochemical-panel", confidence=91.0)
    workup = _workup(client, order["id"])
    assert len(workup["isolates"]) == 2
    by_org = {i["organism_code"]: i for i in workup["isolates"]}
    assert by_org["escherichia-coli"]["id_method"] == "maldi-tof"
    assert by_org["escherichia-coli"]["id_confidence"] == 98.0
    _ast(client, order["id"], first, [
        {"antibiotic": "ciprofloxacin", "mic": 0.12,
         "standard": "CLSI", "version": "M100-34"}])
    _ast(client, order["id"], second, [
        {"antibiotic": "oxacillin", "mic": 8,
         "standard": "CLSI", "version": "M100-34"}])
    workup = _workup(client, order["id"])
    by_org = {i["organism_code"]: i for i in workup["isolates"]}
    assert by_org["escherichia-coli"]["ast"][0][
        "final_interpretation"] == "S"
    assert by_org["staphylococcus-aureus"]["ast"][0][
        "final_interpretation"] == "R"


def test_id_confidence_rules_by_method(client):
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    # MALDI-TOF carries a confidence: omitting it is a 422
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/isolates",
                    json={"organism_code": "escherichia-coli",
                          "id_method": "maldi-tof"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # colony morphology carries none: supplying one is a 422
    r = client.post(f"/lab/micro-depth/orders/{order['id']}/isolates",
                    json={"organism_code": "escherichia-coli",
                          "id_method": "colony-morphology",
                          "id_confidence": 50}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # a below-threshold MALDI score is recorded AND flagged
    isolate = _isolate(client, order["id"], confidence=62.0)
    workup = _workup(client, order["id"])
    assert workup["isolates"][0]["id_below_threshold"] is True


# ---------------------------------------------------------------------------
# AST: breakpoint versioning is real
# ---------------------------------------------------------------------------

def test_same_mic_interpreted_differently_under_two_breakpoint_versions(client):
    _publish_breakpoints(client, [
        _cipro_table("M100-34", 0.25, 2),      # MIC 1 -> I
        _cipro_table("M100-35", 0.03, 0.12),   # MIC 1 -> R
    ])
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"])
    _ast(client, order["id"], isolate, [
        {"antibiotic": "ciprofloxacin", "mic": 1,
         "standard": "CLSI", "version": "M100-34"},
        {"antibiotic": "ciprofloxacin", "mic": 1,
         "standard": "CLSI", "version": "M100-35"}])
    ast = _workup(client, order["id"])["isolates"][0]["ast"]
    by_version = {e["version"]: e for e in ast}
    assert by_version["M100-34"]["computed_interpretation"] == "I"
    assert by_version["M100-35"]["computed_interpretation"] == "R"
    # every entry is stamped with its standard + version
    assert by_version["M100-34"]["standard"] == "CLSI"


def test_ast_without_published_breakpoint_is_not_guessed(client):
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"])
    _ast(client, order["id"], isolate, [
        {"antibiotic": "ciprofloxacin", "mic": 1,
         "standard": "CLSI", "version": "M100-34"}])
    entry = _workup(client, order["id"])["isolates"][0]["ast"][0]
    assert entry["computed_interpretation"] is None
    assert entry["final_interpretation"] is None
    assert "No published CLSI M100-34 breakpoint" in \
        entry["interpretation_note"]


def test_zone_diameter_interpretation(client):
    _publish_breakpoints(client, [{"standard": "EUCAST", "version": "14.0",
                                    "entries": [
        {"organism": "escherichia-coli", "antibiotic": "ciprofloxacin",
         "zone_s_min": 25, "zone_r_max": 18}]}])
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"])
    _ast(client, order["id"], isolate, [
        {"antibiotic": "ciprofloxacin", "zone_mm": 30,
         "standard": "EUCAST", "version": "14.0"}])
    entry = _workup(client, order["id"])["isolates"][0]["ast"][0]
    assert entry["computed_interpretation"] == "S"
    assert entry["zone_mm"] == 30


# ---------------------------------------------------------------------------
# Expert rules + selective reporting
# ---------------------------------------------------------------------------

def test_expert_rule_fires_and_names_itself(client):
    _publish_breakpoints(client, [_cipro_table("M100-34", 0.25, 2)])
    _publish(client, "terminology", "micro-expert-rules", {
        "name": "Test expert rules",
        "concepts": [{"code": "intrinsic-resistance",
                      "display": "Intrinsic resistance", "active": True}],
        "rules": [{
            "code": "test-inducible-rule",
            "display": "Inducible resistance pattern (test rule)",
            "organism": "staphylococcus-aureus",
            "antibiotic": "oxacillin",
            "interpretation_in": ["S"],
            "co_result": {"antibiotic": "vancomycin",
                          "interpretation_in": ["R"]},
            "force_to": "R",
            "reason": "Co-result pattern implies inducible resistance "
                      "in this synthetic test rule"}]})
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"], "staphylococcus-aureus")
    _ast(client, order["id"], isolate, [
        {"antibiotic": "oxacillin", "mic": 1,
         "standard": "CLSI", "version": "M100-34"},
        {"antibiotic": "vancomycin", "mic": 32,
         "standard": "CLSI", "version": "M100-34"}])
    ast = {e["antibiotic"]: e
           for e in _workup(client, order["id"])["isolates"][0]["ast"]}
    oxa = ast["oxacillin"]
    assert oxa["computed_interpretation"] == "S"
    assert oxa["final_interpretation"] == "R"
    assert len(oxa["expert_traces"]) == 1
    trace = oxa["expert_traces"][0]
    assert trace["rule_code"] == "test-inducible-rule"
    assert trace["from_interpretation"] == "S"
    assert trace["to_interpretation"] == "R"
    # the co-result itself is untouched by the rule
    assert ast["vancomycin"]["expert_traces"] == []


def test_selective_reporting_suppresses_released_view_only(client):
    _publish_breakpoints(client, [_cipro_table("M100-34", 0.25, 2)])
    _publish(client, "terminology", "micro-selective-reporting", {
        "name": "Test selective rules",
        "concepts": [{"code": "cascade", "display": "Cascade reporting",
                      "active": True}],
        "rules": [{"organism": "escherichia-coli",
                   "antibiotic": "meropenem",
                   "reason": "Cascade: carbapenems reported only when "
                             "first-line agents are resistant"}]})
    order = _collected(client)
    _plating(client, order["id"])
    _gram(client, order["id"])
    isolate = _isolate(client, order["id"])
    _ast(client, order["id"], isolate, [
        {"antibiotic": "ciprofloxacin", "mic": 0.12,
         "standard": "CLSI", "version": "M100-34"},
        {"antibiotic": "meropenem", "mic": 0.5,
         "standard": "CLSI", "version": "M100-34"}])
    lab_ast = {e["antibiotic"]: e for e in
               _workup(client, order["id"], "lab")["isolates"][0]["ast"]}
    # lab view: full set, suppression visible with its reason
    assert lab_ast["meropenem"]["mic"] == 0.5
    assert lab_ast["meropenem"]["suppressed"] is True
    assert "Cascade" in lab_ast["meropenem"]["suppression_reason"]
    assert lab_ast["ciprofloxacin"]["suppressed"] is False
    released_ast = {e["antibiotic"]: e for e in
                    _workup(client, order["id"], "released")
                    ["isolates"][0]["ast"]}
    # released view: the suppressed entry carries no value/interpretation
    assert released_ast["meropenem"]["suppressed"] is True
    assert "mic" not in released_ast["meropenem"]
    assert released_ast["ciprofloxacin"]["final_interpretation"] == "S"


# ---------------------------------------------------------------------------
# Antibiogram: %S math, first-isolate dedup, minimum-count suppression
# ---------------------------------------------------------------------------

def test_antibiogram_math_dedup_and_minimum_count(client):
    _publish_breakpoints(client, [_cipro_table("M100-34", 0.25, 2)])
    _publish(client, "terminology", "micro-workup", {
        **lab_micro.default_workup_vocab_payload(),
        "antibiogramMinIsolates": 2})
    sensitive = [{"antibiotic": "ciprofloxacin", "mic": 0.12,
                  "standard": "CLSI", "version": "M100-34"}]
    resistant = [{"antibiotic": "ciprofloxacin", "mic": 8,
                  "standard": "CLSI", "version": "M100-34"}]
    # pat-1: first isolate S; a SECOND culture for the same patient
    # and organism is a duplicate for the antibiogram and must not
    # move the rate.
    _full_culture(client, "pat-1", ast_results=sensitive)
    _full_culture(client, "pat-1", ast_results=resistant)
    _full_culture(client, "pat-2", ast_results=resistant)
    _full_culture(client, "pat-3", ast_results=sensitive)
    r = client.get("/lab/micro-depth/antibiogram", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    items = {(i["organism"], i["antibiotic"]): i
             for i in r.json()["items"]}
    group = items[("escherichia-coli", "ciprofloxacin")]
    # first isolates only: pat-1 S, pat-2 R, pat-3 S -> 2/3
    assert group["isolates"] == 3
    assert group["susceptible"] == 2
    assert group["pct_s"] == 66.7
    assert group["suppressed"] is False
    # below the governed minimum the %S is suppressed, not published
    r = client.get("/lab/micro-depth/antibiogram",
                   params={"min_isolates": 30}, headers=CLINICIAN)
    group = {(i["organism"], i["antibiotic"]): i
             for i in r.json()["items"]}[
        ("escherichia-coli", "ciprofloxacin")]
    assert group["pct_s"] is None
    assert group["suppressed"] is True
    assert "minimum isolate count" in group["suppression_reason"]


# ---------------------------------------------------------------------------
# LAB-m3: MDRO finalisation alert into Infection Prevention
# ---------------------------------------------------------------------------

def _publish_mdrom_organisms(client):
    concepts = []
    for concept in lab_micro.lab.default_organisms_payload()["concepts"]:
        concept = dict(concept)
        if concept["code"] == "staphylococcus-aureus":
            concept["mdro"] = True
        concepts.append(concept)
    _publish(client, "terminology", "micro-organisms", {
        "name": "Test organisms with MDRO flag",
        "concepts": concepts})
    _publish(client, "terminology", "micro-mdro-alerts", {
        "name": "Test MDRO mapping",
        "concepts": [{"code": "mdro", "display": "MDRO", "active": True}],
        "alerts": [{"organism": "staphylococcus-aureus",
                    "hai_type": "mrsa"}]})


def test_mdrom_finalisation_creates_ip_alert_non_mdrom_does_not(client):
    _publish_mdrom_organisms(client)
    # non-MDRO culture: no IP case is created
    _full_culture(client, "pat-1", organism="escherichia-coli")
    r = client.get("/infection/cases", headers=CLINICIAN)
    assert r.json()["total"] == 0, r.text
    # MDRO culture finalised through the bench path (organism stage)
    _patient(client, "pat-2")
    order = _order(client, BLOOD_CULTURE, "pat-2")
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "plated")
    r = client.post(f"/lab/micro/orders/{order['id']}/preliminary",
                    json={"text": "Gram-positive cocci in clusters"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post(f"/lab/micro/orders/{order['id']}/organism",
                    json={"organism_code": "staphylococcus-aureus"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = _finalize(client, order["id"])
    alerts = r.json()["mdro_alerts"]
    assert len(alerts) == 1, r.text
    assert alerts[0]["organism"] == "staphylococcus-aureus"
    assert alerts[0]["kind"] == "infection-case"
    assert alerts[0]["hai_type"] == "mrsa"
    # the alert exists on the IP side, organism named in its note
    r = client.get("/infection/cases", headers=CLINICIAN)
    assert r.json()["total"] == 1, r.text
    case = r.json()["items"][0]
    assert case["id"] == alerts[0]["ref"]
    assert "staphylococcus-aureus" in (case["note"] or "")
    assert case["patient_ref"] == "Patient/pat-2"
    # and the culture record itself shows the alert reference
    workup = _workup(client, order["id"])
    assert workup["mdro_alerts"][0]["ref"] == case["id"]
    assert workup["finalized_at"] is not None


def test_mdrom_without_mapping_falls_back_to_precaution_alert(client):
    concepts = []
    for concept in lab_micro.lab.default_organisms_payload()["concepts"]:
        concept = dict(concept)
        if concept["code"] == "klebsiella-pneumoniae":
            concept["mdro"] = True
        concepts.append(concept)
    _publish(client, "terminology", "micro-organisms", {
        "name": "Test organisms (unmapped MDRO)", "concepts": concepts})
    order_id = _full_culture(client, "pat-9",
                              organism="klebsiella-pneumoniae")
    workup = _workup(client, order_id)
    assert len(workup["mdro_alerts"]) == 1
    alert = workup["mdro_alerts"][0]
    assert alert["kind"] == "isolation-precaution"
    assert alert["mechanism"] == "precaution-intake-fallback"
    r = client.get("/infection/precautions",
                   params={"patient": "pat-9"}, headers=CLINICIAN)
    assert r.json()["total"] == 1, r.text
    assert "klebsiella-pneumoniae" in r.json()["items"][0]["reason"]
