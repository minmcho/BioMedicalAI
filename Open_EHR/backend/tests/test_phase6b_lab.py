"""Phase 6B — Full Clinical Laboratory Completion (roadmap §7).

Pre-analytical (collection worklist, custody, coded rejection +
recollection), bench catalogues (coag + urinalysis with qualitative
values), microbiology staged reporting, blood bank / transfusion,
anatomic pathology sign-out, post-analytical (auto-verify, delta,
reflex, amended reports) and laboratory operations (QC programme +
Westgard, EQA, instruments, TAT) — with the Phase 2 visibility and
patient-scoping invariants held throughout.

Synthetic data only (fabricated patients, fabricated values)."""
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

GLUCOSE = "2345-7"
POTASSIUM = "2823-3"
TSH = "3016-3"
CBC = "58410-2"
UA_PANEL = "24356-8"
COAG_PANEL = "COAG-PANEL"
BLOOD_CULTURE = "87040-1"
PATHOLOGY = "11529-5"
TS_ORDER = "BB-TS"
XM_ORDER = "BB-XM"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["6B"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _order(client, code, patient="pat-1", expect=201, **extra):
    body = {"code": code, "subject": {"reference": f"Patient/{patient}"}}
    body.update(extra)
    r = client.post("/lab/orders", json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def _transition(client, order_id, to_status, expect=200, **extra):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status, **extra}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _to_in_process(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")
    _transition(client, order_id, "in-process")


def _results(client, order_id, results, expect=201):
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": results}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _release(client, report_id):
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _run_released(client, code, results, patient="pat-1"):
    order = _order(client, code, patient=patient)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"], results).json()
    _release(client, outcome["report"]["id"])
    return order["id"], outcome["report"]["id"]


def _queue_item(client, report_id):
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    for item in queue["items"]:
        if item["report"]["id"] == report_id:
            return item
    return None


def _portal_ids(client):
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    return {e["resource"]["id"]: e["resource"] for e in portal["entry"]}


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _catalog(client):
    return client.get("/lab/catalog", headers=CLINICIAN).json()["catalog"]


# ---------------------------------------------------------------------------
# 1. Pre-analytical: custody, worklist, rejection + recollection
# ---------------------------------------------------------------------------

def test_custody_timestamps_recorded_and_exposed(client):
    _patient(client, "pat-1")
    order = _order(client, CBC)
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["custody"]["ordered_at"] is not None
    assert detail["custody"]["collected_at"] is None
    assert detail["custody"]["received_at"] is None
    assert detail["custody"]["in_analysis_at"] is None
    _transition(client, order["id"], "collected")
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["custody"]["collected_at"] is not None
    assert detail["custody"]["received_at"] is None
    _transition(client, order["id"], "received")
    _transition(client, order["id"], "in-process")
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["custody"]["received_at"] is not None
    assert detail["custody"]["in_analysis_at"] is not None


def test_collection_worklist_grouping_priority_and_bench(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    routine = _order(client, CBC, encounter={"reference": "Encounter/enc-a"})
    stat = _order(client, GLUCOSE, encounter={"reference": "Encounter/enc-a"},
                  priority="stat")
    other = _order(client, CBC, patient="pat-2")
    worklist = client.get("/lab/collection-worklist", headers=CLINICIAN).json()
    assert worklist["total_orders"] == 3
    groups = {(g["patient_ref"], g["location"]): g
              for g in worklist["groups"]}
    group = groups[("Patient/pat-1", "Encounter/enc-a")]
    assert len(group["orders"]) == 2
    # stat outranks routine inside the group (config priority order)
    assert group["orders"][0]["order_id"] == stat["id"]
    assert group["orders"][0]["priority"] == "stat"
    assert group["highest_priority"] == "stat"
    assert len(groups[("Patient/pat-2", "unspecified")]["orders"]) == 1
    # collected orders leave the worklist
    _transition(client, stat["id"], "collected")
    worklist = client.get("/lab/collection-worklist", headers=CLINICIAN).json()
    assert worklist["total_orders"] == 2
    remaining = [o["order_id"] for g in worklist["groups"] for o in g["orders"]]
    assert stat["id"] not in remaining


def test_rejection_coded_criteria_history_and_recollection(client):
    _patient(client, "pat-1")
    order = _order(client, CBC)
    _transition(client, order["id"], "collected")
    # unknown criterion code -> 422 (criteria are published terminology)
    _transition(client, order["id"], "rejected", expect=422,
                reason="bad tube", reason_code="not-a-criterion")
    # coded rejection with free-text detail
    _transition(client, order["id"], "rejected",
                reason="Visible haemolysis in the tube",
                reason_code="hemolysed")
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["order"]["status"] == "rejected"
    history = detail["order"]["rejectionHistory"]
    assert len(history) == 1
    assert history[0]["criterion_code"] == "hemolysed"
    assert history[0]["criterion"] == "Haemolysed specimen"
    assert history[0]["reason"] == "Visible haemolysis in the tube"
    # the rejected specimen is preserved, never deleted
    assert detail["specimen"]["status"] == "rejected"
    rejected_specimen_id = detail["specimen"]["id"]
    # a recollect task was spawned for the patient
    tasks = client.get("/fhir/Task", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    recollect = [e["resource"] for e in tasks["entry"]
                 if (e["resource"].get("code") or {}).get("coding", [{}])[0]
                 .get("code") == "recollect"]
    assert len(recollect) == 1 and recollect[0]["status"] == "requested"
    # recollection: order returns to ordered; specimen link cleared
    r = client.post(f"/lab/orders/{order['id']}/recollect", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "ordered"
    assert r.json()["specimen"] is None
    # the recollect task is completed; a second recollect conflicts
    tasks = client.get("/fhir/Task", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    recollect = [e["resource"] for e in tasks["entry"]
                 if (e["resource"].get("code") or {}).get("coding", [{}])[0]
                 .get("code") == "recollect"]
    assert recollect[0]["status"] == "completed"
    assert client.post(f"/lab/orders/{order['id']}/recollect",
                       headers=CLINICIAN).status_code == 409
    # a fresh collection creates a NEW specimen; the rejected one remains
    _transition(client, order["id"], "collected")
    detail = client.get(f"/lab/orders/{order['id']}", headers=CLINICIAN).json()
    assert detail["specimen"]["id"] != rejected_specimen_id
    specimens = client.get("/lab/specimens", headers=CLINICIAN).json()
    statuses = {e["resource"]["id"]: e["resource"]["status"]
                for e in specimens["entry"]}
    assert statuses[rejected_specimen_id] == "rejected"


# ---------------------------------------------------------------------------
# 2. Bench catalogues: coag + urinalysis, qualitative values
# ---------------------------------------------------------------------------

def test_catalog_phase6b_additions(client):
    catalog = _catalog(client)
    concepts = {c["code"]: c for c in catalog["concepts"]}
    for code in ("5902-2", "6301-6", "3173-2"):
        assert concepts[code]["bench"] == "coagulation"
    assert concepts[COAG_PANEL]["panel"] is True
    ua = concepts[UA_PANEL]
    assert ua["panel"] is True
    glucose_strip = concepts["5792-7"]
    assert glucose_strip["qualitative"] is True
    assert "Negative" in glucose_strip["values"]
    assert concepts[BLOOD_CULTURE]["bench"] == "microbiology"
    assert concepts[PATHOLOGY]["bench"] == "pathology"
    assert concepts["883-9"]["bench"] == "blood-bank"
    assert concepts["883-9"]["qualitative"] is True
    criteria = {c["code"] for c in catalog["rejectionCriteria"]}
    assert {"hemolysed", "clotted", "insufficient",
            "mislabelled", "wrong-container"} <= criteria
    assert catalog["autoVerify"]["enabled"] is False  # default preserves behaviour
    assert {p["code"] for p in catalog["bloodProducts"]} >= {
        "rbc", "plasma", "platelets", "cryoprecipitate"}
    assert catalog["reflexRules"][0]["id"] == "tsh-low-add-ft4"


def test_coag_panel_numeric_results(client):
    _patient(client, "pat-1")
    order = _order(client, COAG_PANEL)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"], [
        {"code": "5902-2", "value": 12.4},
        {"code": "6301-6", "value": 1.1},
        {"code": "3173-2", "value": 30.0},
    ]).json()
    assert len(outcome["observations"]) == 3
    assert all("valueQuantity" in o for o in outcome["observations"])


def test_qualitative_urinalysis_and_numeric_validation_unchanged(client):
    _patient(client, "pat-1")
    order = _order(client, UA_PANEL)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"], [
        {"code": "5792-7", "value": "Negative"},     # glucose strip
        {"code": "5804-0", "value": "1+"},           # protein
        {"code": "5803-2", "value": 6.0},            # pH — numeric
        {"code": "5811-5", "value": 1.02},           # specific gravity
    ]).json()
    by_code = {o["code"]["coding"][0]["code"]: o
               for o in outcome["observations"]}
    assert by_code["5792-7"]["valueString"] == "Negative"
    assert by_code["5804-0"]["valueString"] == "1+"
    assert by_code["5803-2"]["valueQuantity"]["value"] == 6.0

    # a value outside the concept's published list -> 422
    order2 = _order(client, UA_PANEL)
    _to_in_process(client, order2["id"])
    _results(client, order2["id"], [{"code": "5792-7", "value": "purple"}],
             expect=422)
    # a NUMBER for a qualitative concept -> 422
    _results(client, order2["id"], [{"code": "5792-7", "value": 5}], expect=422)
    # a STRING for a numeric concept -> 422, exactly as before Phase 6B
    cbc = _order(client, CBC)
    _to_in_process(client, cbc["id"])
    _results(client, cbc["id"], [{"code": "718-7", "value": "high"}], expect=422)
    # bench orders refuse the generic result path: a plated culture is
    # not in a resultable state on the standard spine at all (409) —
    # micro results only enter through the bench endpoints.
    culture = _order(client, BLOOD_CULTURE)
    _transition(client, culture["id"], "collected")
    _transition(client, culture["id"], "plated")
    _results(client, culture["id"], [{"code": BLOOD_CULTURE, "value": 1}],
             expect=409)


# ---------------------------------------------------------------------------
# 3. Microbiology: staged visibility
# ---------------------------------------------------------------------------

def _micro_to_plated(client, patient="pat-1"):
    order = _order(client, BLOOD_CULTURE, patient=patient)
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "plated")
    return order


def test_microbiology_staged_visibility(client):
    _patient(client, "pat-1")
    order = _micro_to_plated(client)
    # preliminary before plating would 409; here we are plated.
    r = client.post(f"/lab/micro/orders/{order['id']}/preliminary",
                    json={"text": "Gram-negative rods seen"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    # staged report: invisible to the patient portal and the inbox
    assert report_id not in _portal_ids(client)
    inbox = client.get("/fhir/inbox", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    assert report_id not in {e["resource"]["id"] for e in inbox["entry"]}
    assert client.get(f"/fhir/DiagnosticReport/{report_id}",
                      headers=PATIENT).status_code == 404
    # organism must come from the published terminology
    r = client.post(f"/lab/micro/orders/{order['id']}/organism",
                    json={"organism_code": "not-an-organism"},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/micro/orders/{order['id']}/organism",
                    json={"organism_code": "escherichia-coli"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["order"]["status"] == "organism-identified"
    # susceptibilities: S/I/R only, antibiotics from terminology
    r = client.post(f"/lab/micro/orders/{order['id']}/susceptibility",
                    json={"results": [{"antibiotic": "ciprofloxacin",
                                        "interpretation": "X"}]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/micro/orders/{order['id']}/susceptibility",
                    json={"results": [
                        {"antibiotic": "ciprofloxacin", "interpretation": "S"},
                        {"antibiotic": "meropenem", "interpretation": "R"}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # STILL invisible pre-final
    assert report_id not in _portal_ids(client)
    # finalize needs the verify capability the clinician holds
    r = client.post(f"/lab/micro/orders/{order['id']}/finalize",
                    headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.post(f"/lab/micro/orders/{order['id']}/finalize",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["status"] == "final"
    # now the final culture report is visible to the patient
    portal = _portal_ids(client)
    assert report_id in portal
    assert portal[report_id]["status"] == "final"


def test_microbiology_no_growth_finalises_without_susceptibility(client):
    _patient(client, "pat-1")
    order = _micro_to_plated(client)
    client.post(f"/lab/micro/orders/{order['id']}/preliminary",
                json={"text": "No growth at 48 hours"}, headers=CLINICIAN)
    client.post(f"/lab/micro/orders/{order['id']}/organism",
                json={"organism_code": "no-growth"}, headers=CLINICIAN)
    r = client.post(f"/lab/micro/orders/{order['id']}/finalize",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["order"]["status"] == "final"


# ---------------------------------------------------------------------------
# 4. Blood bank / transfusion
# ---------------------------------------------------------------------------

def _unit(client, unit_id="SYN-U1", expiry="2099-01-01",
          product="rbc", group="O POS", expect=201):
    r = client.post("/lab/blood-bank/units",
                    json={"unit_id": unit_id, "product_type": product,
                          "blood_group": group, "expiry_date": expiry},
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def test_blood_bank_type_screen_results_on_standard_spine(client):
    _patient(client, "pat-1")
    order = _order(client, TS_ORDER)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"], [
        {"code": "883-9", "value": "O POS"},
        {"code": "890-4", "value": "Negative"},
    ]).json()
    by_code = {o["code"]["coding"][0]["code"]: o
               for o in outcome["observations"]}
    assert by_code["883-9"]["valueString"] == "O POS"
    assert by_code["890-4"]["valueString"] == "Negative"
    # an ABO value outside the published list -> 422
    order2 = _order(client, TS_ORDER)
    _to_in_process(client, order2["id"])
    _results(client, order2["id"], [{"code": "883-9", "value": "X POS"}],
             expect=422)


def test_blood_unit_inventory_issue_transfuse(client):
    _patient(client, "pat-1")
    _unit(client)
    _unit(client, unit_id="SYN-U1", expect=409)          # duplicate id
    _unit(client, unit_id="SYN-U2", product="whole-blood", expect=422)
    _unit(client, unit_id="SYN-U2", group="X POS", expect=422)
    _unit(client, unit_id="SYN-U2", expiry="not-a-date", expect=422)
    xm = _order(client, XM_ORDER)
    # issuing requires a blood-bank order
    chem = _order(client, GLUCOSE)
    r = client.post("/lab/blood-bank/units/SYN-U1/issue",
                    json={"order_id": chem["id"]}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/blood-bank/units/SYN-U1/issue",
                    json={"order_id": xm["id"]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "issued"
    assert r.json()["patient_ref"] == "Patient/pat-1"
    # double issue -> 409
    r = client.post("/lab/blood-bank/units/SYN-U1/issue",
                    json={"order_id": xm["id"]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # transfusion record links unit -> patient
    r = client.post("/lab/blood-bank/transfusions",
                    json={"unit_id": "SYN-U1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    procedure = r.json()["transfusion"]
    assert procedure["subject"]["reference"] == "Patient/pat-1"
    exts = {e["url"]: e.get("valueString") for e in procedure["extension"]}
    assert exts["urn:opencare:extension/blood-unit"] == "SYN-U1"
    assert r.json()["unit"]["status"] == "transfused"


def test_blood_unit_expired_cannot_be_issued(client):
    _patient(client, "pat-1")
    _unit(client, unit_id="SYN-OLD", expiry="2000-01-01")
    xm = _order(client, XM_ORDER)
    r = client.post("/lab/blood-bank/units/SYN-OLD/issue",
                    json={"order_id": xm["id"]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    units = client.get("/lab/blood-bank/units", headers=CLINICIAN).json()
    by_id = {u["unit_id"]: u for u in units}
    assert by_id["SYN-OLD"]["status"] == "expired"


def test_transfusion_reaction_hold_and_clear(client):
    _patient(client, "pat-1")
    _unit(client, unit_id="SYN-R1")
    _unit(client, unit_id="SYN-R2")
    xm = _order(client, XM_ORDER)
    client.post("/lab/blood-bank/units/SYN-R1/issue",
                json={"order_id": xm["id"]}, headers=CLINICIAN)
    client.post("/lab/blood-bank/transfusions",
                json={"unit_id": "SYN-R1"}, headers=CLINICIAN)
    # reaction: unit quarantined + hold placed on the patient
    r = client.post("/lab/blood-bank/units/SYN-R1/reaction",
                    json={"symptoms": "Fever and rigors 20 minutes in"},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["unit"]["status"] == "quarantined"
    assert r.json()["hold"]["active"] is True
    assert r.json()["transfusion_id"] is not None
    # further issues to this patient are blocked while the hold stands
    r = client.post("/lab/blood-bank/units/SYN-R2/issue",
                    json={"order_id": xm["id"]}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert "hold" in r.json()["detail"].lower()
    # clearing is a clinician (lab.verify) action
    r = client.post("/lab/blood-bank/holds/pat-1/clear", headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.post("/lab/blood-bank/holds/pat-1/clear", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    # issue works again
    r = client.post("/lab/blood-bank/units/SYN-R2/issue",
                    json={"order_id": xm["id"]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    # blood-bank surfaces are staff-only
    assert client.get("/lab/blood-bank/units", headers=PATIENT).status_code == 403
    assert client.get("/lab/blood-bank/holds", headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# 5. Anatomic pathology
# ---------------------------------------------------------------------------

SYNOPTIC_ANSWERS = [
    {"linkId": link_id, "answer": [{"valueString": value}]}
    for link_id, value in {
        "specimenType": "Colon biopsy",
        "tumourSite": "Sigmoid colon",
        "histologicType": "Adenocarcinoma",
        "marginStatus": "Margins uninvolved",
        "grade": "Moderately differentiated",
    }.items()
]


def test_pathology_unsigned_invisible_signed_visible(client):
    _patient(client, "pat-1")
    order = _order(client, PATHOLOGY)
    for step in ("collected", "accessioned", "grossed", "histology"):
        _transition(client, order["id"], step)
    # the synoptic form's required items are enforced (Phase 5 machinery)
    r = client.post(f"/lab/pathology/orders/{order['id']}/report",
                    json={"answers": [
                        {"linkId": "specimenType",
                         "answer": [{"valueString": "Colon biopsy"}]}]},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/pathology/orders/{order['id']}/report",
                    json={"answers": SYNOPTIC_ANSWERS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    assert r.json()["questionnaire_response"]["resourceType"] == \
        "QuestionnaireResponse"
    assert r.json()["order"]["status"] == "reported"
    # unsigned: invisible to portal + inbox + patient by-id
    assert report_id not in _portal_ids(client)
    assert client.get(f"/fhir/DiagnosticReport/{report_id}",
                      headers=PATIENT).status_code == 404
    # sign-out is the verify-capability step
    r = client.post(f"/lab/pathology/reports/{report_id}/sign",
                    headers=VIEWER)
    assert r.status_code == 403, r.text
    r = client.post(f"/lab/pathology/reports/{report_id}/sign",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["report"]["status"] == "final"
    assert r.json()["order"]["status"] == "signed-out"
    assert report_id in _portal_ids(client)
    # signing twice conflicts
    assert client.post(f"/lab/pathology/reports/{report_id}/sign",
                       headers=CLINICIAN).status_code == 409


# ---------------------------------------------------------------------------
# 6. Post-analytical: delta, reflex, auto-verify, amended reports
# ---------------------------------------------------------------------------

def test_delta_flag_on_verification_queue(client):
    _patient(client, "pat-1")
    _run_released(client, POTASSIUM, [{"code": POTASSIUM, "value": 4.0}])
    order = _order(client, POTASSIUM)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": POTASSIUM, "value": 5.0}]).json()
    item = _queue_item(client, outcome["report"]["id"])
    assert item is not None
    flags = [f for f in item["delta_flags"] if f["code"] == POTASSIUM]
    assert len(flags) == 1
    assert flags[0]["previous_value"] == 4.0
    assert flags[0]["current_value"] == 5.0
    assert abs(flags[0]["change_pct"] - 25.0) < 0.01  # +25% > 20% threshold
    # a small change inside the threshold carries NO flag
    order2 = _order(client, POTASSIUM)
    _to_in_process(client, order2["id"])
    outcome2 = _results(client, order2["id"],
                        [{"code": POTASSIUM, "value": 4.2}]).json()
    item2 = _queue_item(client, outcome2["report"]["id"])
    assert item2["delta_flags"] == []
    # the flag never blocks verification/release
    _release(client, outcome["report"]["id"])


def test_reflex_rule_creates_flagged_draft_order(client):
    _patient(client, "pat-1")
    order = _order(client, TSH)
    _to_in_process(client, order["id"])
    outcome = _results(client, order["id"],
                       [{"code": TSH, "value": 0.2}]).json()  # below low 0.4
    orders = client.get("/lab/orders", params={"status": "ordered"},
                        headers=CLINICIAN).json()
    reflex = []
    for entry in orders["entry"]:
        resource = entry["resource"]
        for ext in resource.get("extension") or []:
            if ext.get("url") == "urn:opencare:extension/reflex":
                reflex.append((resource, ext.get("valueString")))
    assert len(reflex) == 1
    resource, rule_id = reflex[0]
    assert rule_id == "tsh-low-add-ft4"
    assert resource["code"]["coding"][0]["code"] == "3024-7"
    assert resource["subject"]["reference"] == "Patient/pat-1"
    assert resource["status"] == "ordered"  # a draft order, nothing more
    # no duplicate reflex order when the source report is verified/released
    _release(client, outcome["report"]["id"])
    orders = client.get("/lab/orders", params={"status": "ordered"},
                        headers=CLINICIAN).json()
    count = sum(1 for e in orders["entry"]
                for ext in e["resource"].get("extension") or []
                if ext.get("url") == "urn:opencare:extension/reflex")
    assert count == 1
    # a normal TSH fires no reflex
    order2 = _order(client, TSH)
    _to_in_process(client, order2["id"])
    _results(client, order2["id"], [{"code": TSH, "value": 2.0}])
    orders = client.get("/lab/orders", params={"status": "ordered"},
                        headers=CLINICIAN).json()
    count = sum(1 for e in orders["entry"]
                for ext in e["resource"].get("extension") or []
                if ext.get("url") == "urn:opencare:extension/reflex")
    assert count == 1


def test_auto_verify_config_gated_and_never_releases(client):
    _patient(client, "pat-1")

    def glucose(value):
        order = _order(client, GLUCOSE)
        _to_in_process(client, order["id"])
        return _results(client, order["id"],
                        [{"code": GLUCOSE, "value": value}]).json()

    # default: auto-verify DISABLED in the published catalog
    outcome = glucose(90)
    item = _queue_item(client, outcome["report"]["id"])
    assert item["stage"] == "awaiting-verification"

    # enable it (published config change) and the same value auto-verifies
    catalog = _catalog(client)
    enabled = dict(catalog)
    enabled["autoVerify"] = {"enabled": True}
    _publish_config(client, "terminology", "lab-catalog", enabled)
    outcome = glucose(90)
    report_id = outcome["report"]["id"]
    item = _queue_item(client, report_id)
    assert item["stage"] == "awaiting-release"  # verified by the rules
    # ...but NEVER released: still invisible to the patient portal
    assert report_id not in _portal_ids(client)
    rows = client.get("/studio/audit", params={"limit": 200},
                      headers=ADMIN).json()
    auto = [r for r in rows if r["actor"] == "system:auto-verify"]
    assert len(auto) == 1
    assert auto[0]["action"] == "lab.report.verify"
    assert auto[0]["entity"] == f"DiagnosticReport/{report_id}"
    # outside the auto-verify range: no auto-verify
    outcome = glucose(150)
    assert _queue_item(client, outcome["report"]["id"])["stage"] == \
        "awaiting-verification"
    # a critical value is excluded even inside the numeric range check
    outcome = glucose(40)
    assert _queue_item(client, outcome["report"]["id"])["stage"] == \
        "awaiting-verification"
    # a failed latest QC run blocks auto-verify too
    r = client.post("/lab/qc",
                    json={"analyte_code": GLUCOSE,
                          "control_material": "SYN-QC-LOT-1",
                          "expected_low": 100, "expected_high": 110,
                          "measured_value": 50},
                    headers=CLINICIAN)
    assert r.status_code == 201 and r.json()["passed"] is False
    outcome = glucose(90)
    assert _queue_item(client, outcome["report"]["id"])["stage"] == \
        "awaiting-verification"


def test_amended_report_versioning_and_portal_flag(client):
    _patient(client, "pat-1")
    _, report_id = _run_released(client, GLUCOSE,
                                 [{"code": GLUCOSE, "value": 90}])
    # reason is mandatory
    r = client.post(f"/lab/reports/{report_id}/amend", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post(f"/lab/reports/{report_id}/amend",
                    json={"reason": "Transcription error corrected",
                          "results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    amended = r.json()["report"]
    assert r.json()["original_report_id"] == report_id
    assert amended["id"] != report_id
    # FHIR-correct: the new version carries status "amended" and links
    # back to the original it supersedes, with the mandatory reason.
    assert amended["status"] == "amended"
    exts = {e["url"]: e for e in amended["extension"]}
    assert exts["urn:opencare:extension/amends-report"]["valueReference"][
        "reference"] == f"DiagnosticReport/{report_id}"
    assert exts["urn:opencare:extension/amendment-reason"][
        "valueString"] == "Transcription error corrected"
    # the corrected value is on the amended version; the original is
    # preserved untouched (still final), linked forward to its successor
    portal = _portal_ids(client)
    assert amended["id"] in portal
    assert portal[report_id]["status"] == "final"
    orig_exts = {e["url"]: e for e in portal[report_id]["extension"]}
    assert orig_exts["urn:opencare:extension/amended-by-report"][
        "valueReference"]["reference"] == f"DiagnosticReport/{amended['id']}"
    amended_obs = [res for res in portal.values()
                   if res["resourceType"] == "Observation"
                   and res["id"] in
                   {ref["reference"].split("/")[1]
                    for ref in portal[amended["id"]].get("result") or []}]
    assert amended_obs and \
        amended_obs[0]["valueQuantity"]["value"] == 95
    # the clinician inbox shows the amendment linkage honestly too
    inbox = client.get("/fhir/inbox", params={"patient": "pat-1"},
                       headers=CLINICIAN).json()
    inbox_by_id = {e["resource"]["id"]: e["resource"] for e in inbox["entry"]}
    assert amended["id"] in inbox_by_id
    inbox_exts = {e["url"]: e for e in inbox_by_id[amended["id"]]["extension"]}
    assert inbox_exts["urn:opencare:extension/amendment-reason"][
        "valueString"] == "Transcription error corrected"
    # amending an already-superseded report conflicts
    r = client.post(f"/lab/reports/{report_id}/amend",
                    json={"reason": "again"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    # amending a draft conflicts too
    order = _order(client, GLUCOSE)
    _to_in_process(client, order["id"])
    draft = _results(client, order["id"],
                     [{"code": GLUCOSE, "value": 88}]).json()
    r = client.post(f"/lab/reports/{draft['report']['id']}/amend",
                    json={"reason": "too early"}, headers=CLINICIAN)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 7. Laboratory operations: QC programme, EQA, instruments, TAT
# ---------------------------------------------------------------------------

def _qc_run(client, measured, expect=201):
    r = client.post("/lab/qc",
                    json={"analyte_code": POTASSIUM,
                          "control_material": "SYN-QC-LOT-1",
                          "level": "L1",
                          "expected_low": 3.0, "expected_high": 5.5,
                          "measured_value": measured},
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r.json()


def test_westgard_flags_and_levey_jennings(client):
    # programme: K L1 material mean 4.0, SD 0.1 (published catalog config)
    run = _qc_run(client, 4.0)
    assert run["level"] == "L1"
    assert run["flags"] == [] and run["z_score"] == 0.0
    assert run["passed"] is True  # range semantics unchanged
    run = _qc_run(client, 4.25)   # z = +2.5 -> 1-2s warning
    assert "1-2s" in run["flags"]
    assert abs(run["z_score"] - 2.5) < 0.01
    run = _qc_run(client, 4.45)   # z = +4.5 -> 1-3s rejection
    assert "1-3s" in run["flags"]
    _qc_run(client, 4.25)
    run = _qc_run(client, 4.25)  # two in a row beyond +2SD -> 2-2s
    assert "2-2s" in run["flags"]
    # Levey-Jennings: series stats computed server-side
    lj = client.get("/lab/qc/levey-jennings",
                    params={"analyte_code": POTASSIUM,
                            "control_material": "SYN-QC-LOT-1"},
                    headers=CLINICIAN).json()
    assert lj["count"] == 5
    assert abs(lj["mean"] - 4.24) < 0.01
    assert lj["sd"] > 0 and lj["limits"]["plus_2sd"] > lj["mean"]


def test_eqa_records(client):
    r = client.post("/lab/eqa",
                    json={"event": "SYN-EQA-2026-Q3", "analyte_code": GLUCOSE,
                          "reported_value": 95, "acceptable_low": 90,
                          "acceptable_high": 110},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["passed"] is True
    r = client.post("/lab/eqa",
                    json={"event": "SYN-EQA-2026-Q3", "analyte_code": GLUCOSE,
                          "reported_value": 130, "acceptable_low": 90,
                          "acceptable_high": 110},
                    headers=CLINICIAN)
    assert r.json()["passed"] is False  # computed server-side
    r = client.post("/lab/eqa",
                    json={"event": "SYN-EQA-2026-Q3",
                          "analyte_code": "9999-9", "reported_value": 1,
                          "acceptable_low": 0, "acceptable_high": 2},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    records = client.get("/lab/eqa", headers=CLINICIAN).json()
    assert len(records) == 2
    assert client.get("/lab/eqa", headers=PATIENT).status_code == 403


def test_instrument_registry_log_and_ingest_metadata(client):
    r = client.post("/lab/instruments",
                    json={"instrument_id": "SYN-AN-01",
                          "name": "Synthetic chemistry analyser",
                          "bench": "chemistry"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "active"
    r = client.post("/lab/instruments",
                    json={"instrument_id": "SYN-AN-01", "name": "dup"},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    r = client.post("/lab/instruments/SYN-AN-01/status",
                    json={"status": "maintenance"}, headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "maintenance"
    r = client.post("/lab/instruments/SYN-AN-01/status",
                    json={"status": "broken"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/instruments/SYN-AN-01/log",
                    json={"kind": "calibration", "note": "Weekly calibration"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    log = client.get("/lab/instruments/SYN-AN-01/log",
                     headers=CLINICIAN).json()
    assert len(log) == 1 and log[0]["kind"] == "calibration"

    # ingest naming an unregistered instrument -> 422
    _patient(client, "pat-1")
    order = _order(client, GLUCOSE, id="ord-inst-1")
    _transition(client, order["id"], "collected")
    _transition(client, order["id"], "received")
    content = ("order_id,patient_id,code,value,unit\n"
               "ord-inst-1,pat-1,2345-7,88,mg/dL\n")
    r = client.post("/lab/ingest",
                    json={"format": "csv", "content": content,
                          "instrument_id": "SYN-UNKNOWN"},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/ingest",
                    json={"format": "csv", "content": content,
                          "instrument_id": "SYN-AN-01"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["instrument_id"] == "SYN-AN-01"
    detail = client.get("/lab/orders/ord-inst-1", headers=CLINICIAN).json()
    exts = {e["url"]: e.get("valueString")
            for e in detail["reports"][0].get("extension") or []}
    assert exts["urn:opencare:extension/instrument"] == "SYN-AN-01"


def test_tat_percentiles_from_custody_timestamps(client):
    _patient(client, "pat-1")
    report_ids = []
    for value in (88, 92, 95):
        _, report_id = _run_released(client, GLUCOSE,
                                     [{"code": GLUCOSE, "value": value}])
        report_ids.append(report_id)
    # Backdate the synthetic timestamps directly in the store so the
    # order->release durations are exactly 2h, 4h and 10h.
    from sqlalchemy import select as sa_select

    from app.models import FhirResource
    factory = client.app.state.session_factory
    durations = {report_ids[0]: 2, report_ids[1]: 4, report_ids[2]: 10}
    with factory() as session:
        for report_id, hours in durations.items():
            report = session.scalars(
                sa_select(FhirResource).where(
                    FhirResource.resource_type == "DiagnosticReport",
                    FhirResource.resource_id == report_id)).first()
            order_ref = report.body["basedOn"][0]["reference"]
            order = session.scalars(
                sa_select(FhirResource).where(
                    FhirResource.resource_type == "ServiceRequest",
                    FhirResource.resource_id == order_ref.split("/")[1])).first()
            body = dict(order.body)
            body["authoredOn"] = "2026-01-01T00:00:00+00:00"
            order.body = body
            rbody = dict(report.body)
            rbody["issued"] = f"2026-01-01T{hours:02d}:00:00+00:00"
            report.body = rbody
            specimen_ref = order.body["specimen"][0]["reference"]
            specimen = session.scalars(
                sa_select(FhirResource).where(
                    FhirResource.resource_type == "Specimen",
                    FhirResource.resource_id == specimen_ref.split("/")[1])).first()
            sbody = dict(specimen.body)
            sbody["receivedDateTime"] = "2026-01-01T01:00:00+00:00"
            specimen.body = sbody
        session.commit()
    tat = client.get("/lab/tat", headers=CLINICIAN).json()
    assert tat["source"] == "live-query"
    bench = tat["benches"]["chemistry"]
    assert bench["count"] == 3
    assert bench["order_to_release_hours"]["p50"] == 4.0
    assert bench["order_to_release_hours"]["p90"] == 10.0
    # received at 01:00 -> durations 1h, 3h, 9h
    assert bench["received_to_release_hours"]["p50"] == 3.0
    assert tat["analytes"][GLUCOSE]["count"] == 3
    assert client.get("/lab/tat", headers=PATIENT).status_code == 403


def test_bench_surfaces_patient_scoping(client):
    # every Phase 6B surface is staff-only; the patient token gets 403
    for path in ("/lab/collection-worklist", "/lab/micro/terminology",
                 "/lab/blood-bank/units", "/lab/blood-bank/holds",
                 "/lab/eqa", "/lab/instruments", "/lab/tat",
                 "/lab/qc/levey-jennings?analyte_code=2345-7"
                 "&control_material=SYN-QC-LOT-1",
                 "/lab/pathology/synoptic-form"):
        assert client.get(path, headers=PATIENT).status_code == 403, path
