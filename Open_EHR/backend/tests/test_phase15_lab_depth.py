"""Phase 15 (B / BD-09 / BD-10 / BD-34) — Test Manager + Panel
Manager over the governed catalogue, blood-bank lookback + FDA
deviation reporting, CLIA six-element competency, external EQA/PT
exchange (fixture-defined; live exchange NOT RUN).

Synthetic data only."""
from datetime import date, timedelta

from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

GLUCOSE = "2345-7"
POTASSIUM = "2823-3"
BMP = "51990-0"
SIX_ELEMENTS = [
    "direct-observation-performance",
    "monitoring-recording-reporting",
    "review-intermediate-qc-pt",
    "direct-observation-maintenance",
    "assessment-test-performance",
    "assessment-problem-solving",
]


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["15"]}],
                          "birthDate": "1990-01-01"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _transition(client, order_id, to_status):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r


def _unit(client, unit_id, expiry="2099-01-01", product="rbc",
          group="O POS"):
    r = client.post("/lab/blood-bank/units",
                    json={"unit_id": unit_id, "product_type": product,
                          "blood_group": group, "expiry_date": expiry},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _elements(days_ago=0, assessor="lab-manager"):
    assessed = (date.today() - timedelta(days=days_ago)).isoformat()
    return [{"element": code, "assessed_on": assessed,
             "assessor": assessor} for code in SIX_ELEMENTS]


# ---------------------------------------------------------------------------
# A. Test Manager + Panel Manager
# ---------------------------------------------------------------------------

def test_manager_catalog_enriched_views(client):
    view = client.get("/lab/manager/catalog", headers=CLINICIAN).json()
    tests = {t["code"]: t for t in view["tests"]}
    glucose = tests[GLUCOSE]
    assert glucose["aliquot_rules"]["count"] == 1
    assert glucose["specimen_requirements"]["container"] == "standard tube"
    assert glucose["specimen_requirements"]["specimen_type"] == \
        "Blood specimen"
    panels = {p["code"]: p for p in view["panels"]}
    bmp = panels[BMP]
    assert bmp["component_codes"] == ["2345-7", "3094-0", "2160-0",
                                      "2951-2", "2823-3"]
    assert len(bmp["components"]) == 5
    assert all("aliquot_rules" in c for c in bmp["components"])
    # the TSH -> free-T4 reflex rule rides the catalogue view
    rule_ids = {r["id"] for r in view["reflex_rules"]}
    assert "tsh-low-add-ft4" in rule_ids
    tsh = tests["3016-3"]
    assert any(r["addCode"] == "3024-7" for r in tsh["reflex_rules"])
    assert view["draft"] is None


def test_compose_requires_director_or_manager(client):
    body = {"code": "NEW-T1", "display": "New test one", "unit": "mg/dL",
            "aliquot_rules": {"count": 1, "volumeMl": 2.0,
                              "container": "edta tube"}}
    # clinician whose linked staff job_role is physician -> 403
    r = client.post("/lab/manager/tests/draft", json=body,
                    headers=CLINICIAN)
    assert r.status_code == 403, r.text
    # viewer lacks lab.work entirely -> 403
    r = client.post("/lab/manager/tests/draft", json=body, headers=VIEWER)
    assert r.status_code == 403, r.text
    # configurator + admin compose
    r = client.post("/lab/manager/tests/draft", json=body, headers=CONFIG)
    assert r.status_code == 201, r.text
    r = client.post("/lab/manager/panels/draft",
                    json={"code": "NEW-P1", "display": "New panel",
                          "components": [GLUCOSE, "NEW-T1"]},
                    headers=ADMIN)
    assert r.status_code == 201, r.text


def test_director_job_role_can_compose(client):
    # publish a workforce config that knows the lab-director role,
    # then give the clinician's sign-in a director staff entry
    ops = client.get("/people/config", headers=ADMIN).json()
    ops["concepts"] = list(ops["concepts"]) + [
        {"code": "lab-director", "display": "Laboratory director",
         "active": True}]
    r = client.post("/studio/config/terminology/workforce", json=ops,
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/workforce/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/people/staff",
                    json={"full_name": "Dana Director, MD",
                          "job_role": "lab-director",
                          "username": "dev-clinician"},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "DIR-T1", "display": "Director test",
                          "aliquot_rules": {"count": 1, "volumeMl": 1.0,
                                            "container": "standard tube"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_compose_writes_draft_only_published_untouched(client):
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "DRAFT-T", "display": "Draft-only test",
                          "aliquot_rules": {"count": 2, "volumeMl": 3.0,
                                            "container": "cryovial"}},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    draft = r.json()["draft"]
    assert draft["status"] == "draft"
    assert draft["key"] == "lab-catalog"
    # published catalogue does NOT carry the new test
    catalog = client.get("/lab/catalog", headers=CLINICIAN).json()["catalog"]
    assert "DRAFT-T" not in {c["code"] for c in catalog["concepts"]}
    # ... ordering it fails (not published) ...
    r = client.post("/lab/orders",
                    json={"code": "DRAFT-T",
                          "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # ... but the draft payload does carry it, aliquot rules included
    view = client.get("/lab/manager/draft", headers=CLINICIAN).json()
    concepts = {c["code"]: c for c in view["draft"]["payload"]["concepts"]}
    assert concepts["DRAFT-T"]["aliquotRules"] == {
        "count": 2, "volumeMl": 3.0, "container": "cryovial"}
    # a second compose UPDATES the same draft (no second draft row)
    r2 = client.post("/lab/manager/tests/draft",
                     json={"code": "DRAFT-T2", "display": "Second draft"},
                     headers=ADMIN)
    assert r2.json()["draft"]["version"] == draft["version"]


def test_compose_validation(client):
    # panel component must exist
    r = client.post("/lab/manager/panels/draft",
                    json={"code": "BAD-P", "display": "Bad panel",
                          "components": [GLUCOSE, "MISSING-1"]},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    # a panel cannot compose another panel
    r = client.post("/lab/manager/panels/draft",
                    json={"code": "BAD-P", "display": "Bad panel",
                          "components": [BMP]},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    # reflex rules must reference existing tests
    r = client.post("/lab/manager/panels/draft",
                    json={"code": "BAD-P", "display": "Bad panel",
                          "components": [GLUCOSE],
                          "reflex_rules": [
                              {"triggerCode": GLUCOSE, "when": "below-low",
                               "addCode": "MISSING-2"}]},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    # aliquot rules are structured + validated
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "BAD-T", "display": "Bad test",
                          "aliquot_rules": {"count": 0, "volumeMl": 2.0,
                                            "container": "tube"}},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "BAD-T", "display": "Bad test",
                          "aliquot_rules": {"count": 1,
                                            "volumeMl": "lots",
                                            "container": "tube"}},
                    headers=ADMIN)
    assert r.status_code == 422, r.text


def test_consumption_compose_publish_order_expand(client):
    """Compose a new panel in draft -> publish via the existing
    Studio publish endpoint -> place a lab order for the panel ->
    components expand through the existing result machinery."""
    _patient(client)
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "PANEL-T", "display": "Panel member test",
                          "unit": "mg/dL",
                          "reference_range": {"low": 1.0, "high": 9.0,
                                              "unit": "mg/dL"},
                          "aliquot_rules": {"count": 1, "volumeMl": 2.0,
                                            "container": "standard tube"}},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    r = client.post("/lab/manager/panels/draft",
                    json={"code": "PANEL-NEW", "display": "Composed panel",
                          "components": [GLUCOSE, "PANEL-T"],
                          "reflex_rules": [
                              {"triggerCode": GLUCOSE, "when": "below-low",
                               "addCode": "PANEL-T",
                               "note": "Low glucose reflexes panel test"}]},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["draft"]["version"]
    # publish through the EXISTING Studio publish endpoint
    r = client.post(
        f"/studio/config/terminology/lab-catalog/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    # the published catalogue now carries the panel + reflex rule
    catalog = client.get("/lab/catalog", headers=CLINICIAN).json()["catalog"]
    concepts = {c["code"]: c for c in catalog["concepts"]}
    assert concepts["PANEL-NEW"]["components"] == [GLUCOSE, "PANEL-T"]
    assert any(r["addCode"] == "PANEL-T"
               for r in catalog.get("reflexRules") or [])
    # order the panel and result BOTH components (machinery expands)
    order = _order(client, "PANEL-NEW")
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "received")
    _transition(client, order["id"], "in-process")
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 90},
                                      {"code": "PANEL-T", "value": 5.0}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert len(r.json()["observations"]) == 2


# ---------------------------------------------------------------------------
# C. BD-09 — lookback + deviation reporting
# ---------------------------------------------------------------------------

def _lookback_fixture(client):
    _patient(client)
    # Wave A: the LAB-B1 issue gate needs a RELEASED type & screen
    # (O POS + Negative screen -> the O POS RBC units are
    # electronic-crossmatch eligible), and LAB-M1 transfusion
    # verification needs the two identifiers + vitals (the fixture
    # patient already carries birthDate 1990-01-01).
    ts = _order(client, "BB-TS")
    for to_status in ("collected", "received", "in-process"):
        _transition(client, ts["id"], to_status)
    r = client.post(f"/lab/orders/{ts['id']}/results",
                    json={"results": [{"code": "883-9", "value": "O POS"},
                                      {"code": "890-4",
                                       "value": "Negative"}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    for step in ("verify", "release"):
        r = client.post(f"/lab/reports/{report_id}/{step}",
                        headers=CLINICIAN)
        assert r.status_code == 200, r.text
    _unit(client, "LB-1")
    _unit(client, "LB-2")
    _unit(client, "LB-3")  # never issued: the unissued sibling component
    xm = _order(client, "BB-XM")
    for unit_id in ("LB-1", "LB-2"):
        r = client.post(f"/lab/blood-bank/units/{unit_id}/issue",
                        json={"order_id": xm["id"]}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    r = client.post("/lab/blood-bank/transfusions",
                    json={"unit_id": "LB-1",
                          "wristband_patient_id": "pat-1",
                          "date_of_birth": "1990-01-01",
                          "vitals": {"temperature_c": 36.8,
                                     "pulse_bpm": 72,
                                     "systolic_bp": 118,
                                     "diastolic_bp": 76,
                                     "resp_rate": 16}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return xm


def test_lookback_trace_quarantine_notifications(client):
    _lookback_fixture(client)
    r = client.post("/lab/blood-bank/lookback",
                    json={"unit_id": "LB-1",
                          "trigger_detail": "Donor NAT reactive "
                                            "(synthetic trigger)"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    case = r.json()
    component_ids = {c["unit_id"] for c in case["trace"]["components"]}
    assert component_ids == {"LB-1", "LB-2", "LB-3"}
    assert case["trace"]["quarantined_units"] == ["LB-3"]
    recipients = case["trace"]["recipients"]
    assert {rec["unit_id"] for rec in recipients} == {"LB-1", "LB-2"}
    # 10-year traceability stated in the case payload
    assert case["traceability"]["regulation"] == "21 CFR 606.160"
    assert case["traceability"]["retention_years"] == 10
    assert "10 years" in case["traceability"]["statement"]
    # the unissued sibling was quarantined through the unit status
    units = client.get("/lab/blood-bank/units", headers=CLINICIAN).json()
    by_id = {u["unit_id"]: u for u in units}
    assert by_id["LB-3"]["status"] == "quarantined"
    assert by_id["LB-1"]["status"] == "transfused"
    # notification records: recipients + the consignee for LB-3
    kinds = {(n["kind"], n["unit_id"]) for n in case["notifications"]}
    assert ("recipient", "LB-1") in kinds
    assert ("recipient", "LB-2") in kinds
    assert ("consignee", "LB-3") in kinds
    assert all(n["state"] == "pending" for n in case["notifications"])
    # GET the case back complete; advance one notification
    got = client.get(f"/lab/blood-bank/lookback/{case['case_id']}",
                     headers=CLINICIAN).json()
    assert got["case_id"] == case["case_id"]
    note_id = next(n["id"] for n in got["notifications"]
                   if n["unit_id"] == "LB-1")
    r = client.post(
        f"/lab/blood-bank/lookback/{case['case_id']}/notifications/{note_id}",
        json={"state": "notified"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    states = {n["id"]: n["state"] for n in r.json()["notifications"]}
    assert states[note_id] == "notified"
    r = client.post("/lab/blood-bank/lookback",
                    json={"unit_id": "NOPE"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text


def test_deviation_report_from_lookback(client):
    _lookback_fixture(client)
    case = client.post("/lab/blood-bank/lookback",
                       json={"unit_id": "LB-1",
                             "trigger_detail": "Synthetic trigger"},
                       headers=CLINICIAN).json()
    r = client.post("/lab/blood-bank/deviations",
                    json={"case_id": case["case_id"],
                          "description": "Unit LB-1 transfused before "
                                         "donor NAT result returned "
                                         "(synthetic deviation)"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report = r.json()
    assert report["case_id"] == case["case_id"]
    assert report["unit_id"] == "LB-1"
    assert report["artefact"]["type"] == "biological-product-deviation"
    assert len(report["artefact"]["components"]) == 3
    assert report["pdf_generated"] is True
    doc = client.get(
        f"/lab/blood-bank/deviations/{report['report_id']}/document",
        headers=CLINICIAN)
    assert doc.status_code == 200, doc.text
    assert doc.content[:4] == b"%PDF"
    got = client.get(f"/lab/blood-bank/deviations/{report['report_id']}",
                     headers=CLINICIAN).json()
    assert got["report_id"] == report["report_id"]


# ---------------------------------------------------------------------------
# D. BD-10 — CLIA six-element competency
# ---------------------------------------------------------------------------

def test_clia_competency_records_currency_and_worklist(client):
    config = client.get("/lab/competency/config", headers=CLINICIAN).json()
    assert len(config["elements"]) == 6
    assert {e["code"] for e in config["elements"]} == set(SIX_ELEMENTS)
    # current: all six elements assessed today (staff-008 technologist)
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-008", "test_system": GLUCOSE,
                          "elements": _elements(days_ago=0)},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    current = r.json()["competency"]
    assert current["status"] == "current"
    assert current["missing_elements"] == []
    assert current["staff_name"] is not None
    # expired: assessed 400 days ago against a 12-month window
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-007", "test_system": GLUCOSE,
                          "elements": _elements(days_ago=400)},
                    headers=CLINICIAN)
    assert r.json()["competency"]["status"] == "expired"
    # expiring: assessed 340 days ago -> ~25 days of currency left
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-003", "test_system": POTASSIUM,
                          "elements": _elements(days_ago=340)},
                    headers=CLINICIAN)
    expiring = r.json()["competency"]
    assert expiring["status"] == "expiring", expiring
    assert 0 <= expiring["days_to_expiry"] <= 30
    # incomplete: only two of six elements
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-002", "test_system": GLUCOSE,
                          "elements": _elements()[:2]},
                    headers=CLINICIAN)
    incomplete = r.json()["competency"]
    assert incomplete["status"] == "incomplete"
    assert len(incomplete["missing_elements"]) == 4
    # worklist: expired + incomplete + expiring; current stays off
    worklist = client.get("/lab/competency/worklist",
                          headers=CLINICIAN).json()
    staff_on = {i["staff_id"] for i in worklist["items"]}
    assert staff_on == {"staff-007", "staff-003", "staff-002"}
    assert worklist["expired"] == 1
    assert worklist["incomplete"] == 1
    assert worklist["worklist_window_days"] == 90


def test_clia_competency_validation(client):
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-nope", "test_system": GLUCOSE,
                          "elements": _elements()},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-008", "test_system": "NOPE",
                          "elements": _elements()},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    bad = _elements()
    bad[0] = {"element": "not-an-element",
              "assessed_on": date.today().isoformat(), "assessor": "x"}
    r = client.post("/lab/competency/records",
                    json={"staff_id": "staff-008", "test_system": GLUCOSE,
                          "elements": bad},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# E. BD-34 — external EQA/PT exchange (fixture-defined)
# ---------------------------------------------------------------------------

def test_eqa_exchange_export_import_roundtrip(client):
    r = client.post("/lab/eqa-exchange/enrolments",
                    json={"programme": "PT-2026-Q1",
                          "analyte_codes": [POTASSIUM]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # an internal EQA result filed through the existing machinery
    r = client.post("/lab/eqa",
                    json={"event": "PT-2026-Q1", "analyte_code": POTASSIUM,
                          "reported_value": 4.0, "acceptable_low": 3.5,
                          "acceptable_high": 4.5},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # export in the published provider format
    out = client.get("/lab/eqa-exchange/export",
                     params={"programme": "PT-2026-Q1"},
                     headers=CLINICIAN).json()
    assert out["format"] == "opencare-eqaprovider-csv-v1"
    assert out["live_exchange"].startswith("NOT RUN")
    assert "AnalyteCode" in out["content"]
    assert POTASSIUM in out["content"]
    assert out["rows"] == 1
    # import returned scores (provider CSV): one pass, one fail
    csv_text = ("Programme,AnalyteCode,ReportedValue,AcceptableLow,"
                "AcceptableHigh\n"
                f"PT-2026-Q1,{POTASSIUM},4.2,3.5,4.5\n"
                f"PT-2026-Q1,2345-7,300,70,99\n")
    r = client.post("/lab/eqa-exchange/import",
                    json={"programme": "PT-2026-Q1", "content": csv_text},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    imported = r.json()
    assert imported["imported"] == 2
    assert imported["passed"] == 1
    assert imported["failed"] == 1
    assert imported["scoring_state"] == "scored"
    # scores are filed on the internal EQA records
    records = client.get("/lab/eqa", headers=CLINICIAN).json()
    mine = [x for x in records if x["event"] == "PT-2026-Q1"]
    assert len(mine) == 3
    # re-export now carries all three rows (round-trip)
    out2 = client.get("/lab/eqa-exchange/export",
                      params={"programme": "PT-2026-Q1"},
                      headers=CLINICIAN).json()
    assert out2["rows"] == 3


def test_eqa_exchange_validation(client):
    r = client.post("/lab/eqa-exchange/enrolments",
                    json={"programme": "PT-X", "analyte_codes": ["NOPE"]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.get("/lab/eqa-exchange/export",
                   params={"programme": "PT-MISSING"}, headers=CLINICIAN)
    assert r.status_code == 404, r.text
    client.post("/lab/eqa-exchange/enrolments",
                json={"programme": "PT-Y", "analyte_codes": [POTASSIUM]},
                headers=CLINICIAN)
    r = client.post("/lab/eqa-exchange/import",
                    json={"programme": "PT-Y",
                          "scores": [{"analyte_code": POTASSIUM,
                                      "reported_value": "high",
                                      "acceptable_low": 3.5,
                                      "acceptable_high": 4.5}]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
