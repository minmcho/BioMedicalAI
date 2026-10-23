"""UX pass (WS3a) — virtual plates: governed plate templates,
plate-run instances with materialised wells, specimen assignment,
control evaluation, the plate QC gate (plate path + direct result
entry), and completion rules.

Synthetic data only (fabricated patients, fabricated values)."""
from sqlalchemy import select

from app.audit import verify_chain
from app.models import AuditLog
from tests.conftest import CLINICIAN, PATIENT, VIEWER

GLUCOSE = "2345-7"
CHEM = "chem-96"
MICRO = "micro-48"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["Plate"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _specimen(client, code=GLUCOSE, patient="pat-1", to_status="in-process"):
    """A lab order taken to `to_status`; returns (order_id, specimen_id)."""
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    for target in ("collected", "received", "in-process"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": target}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
        if target == to_status:
            break
    detail = client.get(f"/lab/orders/{order_id}", headers=CLINICIAN).json()
    return order_id, detail["specimen"]["id"]


def _create_plate(client, template=CHEM, expect=201, headers=CLINICIAN):
    r = client.post("/lab/plates", json={"template_code": template},
                    headers=headers)
    assert r.status_code == expect, r.text
    return r


def _assign(client, plate_id, specimen_ref, expect=200, **extra):
    r = client.post(f"/lab/plates/{plate_id}/assign",
                    json={"specimen_ref": specimen_ref, **extra},
                    headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _plate(client, plate_id):
    r = client.get(f"/lab/plates/{plate_id}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


def _well(plate, position):
    return next(w for w in plate["wells"] if w["position"] == position)


def _passing_controls(plate):
    """Bulk entries that PASS every control/calibrator/blank well of
    a plate detail payload: the midpoint of a numeric expected range,
    the expected string of a qualitative check."""
    entries = []
    for well in plate["wells"]:
        if well["role"] in ("control-pos", "control-neg", "calibrator",
                            "blank"):
            expected = well["expected"] or {}
            if "result" in expected:
                value = expected["result"]
            else:
                value = (expected["low"] + expected["high"]) / 2
            entries.append({"position": well["position"], "value": value})
    return entries


def _post_results(client, plate_id, entries, expect=200):
    r = client.post(f"/lab/plates/{plate_id}/results",
                    json={"results": entries}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _open_gate(client, plate_id):
    plate = _plate(client, plate_id)
    _post_results(client, plate_id, _passing_controls(plate))
    plate = _plate(client, plate_id)
    assert plate["qc"]["gate_open"] is True
    return plate


# ---------------------------------------------------------------------------
# Templates: seeded published config, contract shape
# ---------------------------------------------------------------------------

def test_plate_templates_seeded_with_contract_shape(client):
    eff = client.get("/studio/config/terminology/effective/lab-plate-templates",
                     headers=CLINICIAN)
    assert eff.status_code == 200, eff.text
    concepts = {c["code"]: c for c in eff.json()["payload"]["concepts"]}
    assert set(concepts) == {CHEM, MICRO}

    chem = concepts[CHEM]
    assert chem["active"] is True and chem["assay"] == "chemistry"
    assert chem["format"] == "96-well" and len(chem["wells"]) == 96
    assert {w["position"] for w in chem["wells"]} == {
        f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)}
    row_a = [w for w in chem["wells"] if w["position"].startswith("A")]
    assert len(row_a) == 12
    assert {w["role"] for w in row_a} <= {
        "control-pos", "control-neg", "calibrator", "blank"}
    for well in row_a:  # chemistry controls carry NUMERIC ranges
        assert set(well["expected"]) == {"low", "high"}
        assert well["expected"]["low"] <= well["expected"]["high"]
    rest = [w for w in chem["wells"] if not w["position"].startswith("A")]
    assert len(rest) == 84
    assert all(w["role"] == "patient" and "expected" not in w for w in rest)

    micro = concepts[MICRO]
    assert micro["active"] is True and micro["format"] == "48-well"
    assert len(micro["wells"]) == 48
    controls = [w for w in micro["wells"] if w["role"] != "patient"]
    assert len(controls) == 8
    for well in controls:  # microbiology controls are QUALITATIVE
        assert set(well["expected"]) == {"result"}
        assert well["expected"]["result"] in ("Positive", "Negative")


# ---------------------------------------------------------------------------
# Run creation
# ---------------------------------------------------------------------------

def test_create_run_materializes_wells(client):
    plate = _create_plate(client).json()
    assert plate["template_code"] == CHEM and plate["status"] == "open"
    assert plate["assay"] == "chemistry" and plate["format"] == "96-well"
    assert len(plate["wells"]) == 96
    a1 = _well(plate, "A1")
    assert a1["role"] == "control-pos"
    assert a1["expected"] == {"low": 90.0, "high": 110.0}
    assert a1["result_status"] == "pending" and a1["specimen_id"] is None
    assert _well(plate, "B1")["role"] == "patient"
    assert plate["qc"] == {"controls_total": 12, "controls_passed": 0,
                           "controls_failed": 0, "gate_open": False}
    # listing + status filter
    listed = client.get("/lab/plates", headers=VIEWER).json()
    assert listed["total"] == 1 and listed["items"][0]["id"] == plate["id"]
    assert client.get("/lab/plates", params={"status": "complete"},
                      headers=CLINICIAN).json()["total"] == 0
    # unknown template -> 404; missing code -> 422; role gates
    _create_plate(client, template="no-such-plate", expect=404)
    r = client.post("/lab/plates", json={}, headers=CLINICIAN)
    assert r.status_code == 422
    _create_plate(client, expect=403, headers=VIEWER)
    assert client.post("/lab/plates", json={"template_code": CHEM}).status_code == 401
    assert client.get("/lab/plates/9999", headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# Assignment
# ---------------------------------------------------------------------------

def test_assign_by_id_auto_position_and_conflicts(client):
    _patient(client)
    order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    out = _assign(client, plate["id"], specimen_id).json()
    # no position -> first empty PATIENT well (row A is the QC row)
    assert out["well"]["position"] == "B1"
    assert out["well"]["specimen_id"] == specimen_id
    assert out["well"]["order_id"] == order_id
    assert out["well"]["assigned_by"] == "dev-clinician"
    assert out["well"]["assigned_at"] is not None

    _oid2, specimen2 = _specimen(client, patient="pat-1")
    # occupied well -> 409
    _assign(client, plate["id"], specimen2, expect=409, position="B1")
    # a control well cannot take a specimen -> 422
    _assign(client, plate["id"], specimen2, expect=422, position="A1")
    # unknown well position -> 422
    _assign(client, plate["id"], specimen2, expect=422, position="Z9")
    # the same specimen on ANOTHER open plate -> 409 naming the plate
    plate2 = _create_plate(client).json()
    r = _assign(client, plate2["id"], specimen_id, expect=409)
    assert f"plate {plate['id']}" in r.json()["detail"]
    # unknown specimen -> 404
    _assign(client, plate2["id"], "no-such-specimen", expect=404)
    # the second specimen assigns cleanly to the next free well
    out = _assign(client, plate["id"], specimen2).json()
    assert out["well"]["position"] == "B2"


def test_assign_by_barcode(client):
    _patient(client)
    _order_id, specimen_id = _specimen(client)
    # the chain read materialises the specimen's lifecycle node +
    # barcode (app.lab_specimens), exactly as a bench scan would find it
    chain = client.get(f"/lab/specimens/{specimen_id}/chain",
                       headers=CLINICIAN).json()
    root = next(m for m in chain["members"]
                if m["specimen_key"] == specimen_id)
    barcode = root["barcode"]
    assert barcode and barcode.startswith("OCSP1|")
    plate = _create_plate(client).json()
    out = _assign(client, plate["id"], barcode).json()
    assert out["well"]["specimen_id"] == specimen_id


def test_assign_to_completed_plate_is_409(client):
    _patient(client)
    _order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    r = client.post(f"/lab/plates/{plate['id']}/complete", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "complete"
    _assign(client, plate["id"], specimen_id, expect=409)


# ---------------------------------------------------------------------------
# Control evaluation
# ---------------------------------------------------------------------------

def test_control_evaluation_numeric_pass_and_fail(client):
    plate = _create_plate(client).json()
    out = _post_results(client, plate["id"], [
        {"position": "A1", "value": 100},     # 90..110 -> pass
        {"position": "A5", "value": 105},     # inclusive high bound -> pass
        {"position": "A3", "value": 50},      # control-neg 0..5 -> FAIL
    ]).json()
    by_pos = {p["position"]: p for p in out["processed"]}
    assert by_pos["A1"]["result_status"] == "passed"
    assert by_pos["A5"]["result_status"] == "passed"
    assert by_pos["A3"]["result_status"] == "failed"
    plate = _plate(client, plate["id"])
    assert plate["qc"]["controls_total"] == 12
    assert plate["qc"]["controls_passed"] == 2
    assert plate["qc"]["controls_failed"] == 1
    assert plate["qc"]["gate_open"] is False  # a failed control holds it


def test_control_evaluation_qualitative_case_insensitive(client):
    plate = _create_plate(client, template=MICRO).json()
    out = _post_results(client, plate["id"], [
        {"position": "A1", "value": "positive"},   # expected Positive -> pass
        {"position": "A3", "value": "NEGATIVE"},   # expected Negative -> pass
        {"position": "A4", "value": "Positive"},   # expected Negative -> FAIL
    ]).json()
    by_pos = {p["position"]: p for p in out["processed"]}
    assert by_pos["A1"]["result_status"] == "passed"
    assert by_pos["A3"]["result_status"] == "passed"
    assert by_pos["A4"]["result_status"] == "failed"
    plate = _plate(client, plate["id"])
    assert plate["qc"]["controls_passed"] == 2
    assert plate["qc"]["controls_failed"] == 1


# ---------------------------------------------------------------------------
# The QC gate: plate path
# ---------------------------------------------------------------------------

def test_gate_blocks_patient_well_then_same_payload_opens_it(client):
    _patient(client)
    order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    _assign(client, plate["id"], specimen_id)  # -> B1

    # gate closed: a patient result is refused 409, naming plate + gate
    r = _post_results(client, plate["id"],
                      [{"position": "B1", "value": 95}], expect=409)
    detail = r.json()["detail"]
    assert f"Plate {plate['id']}" in detail and "QC gate" in detail
    # and nothing was resulted on the order
    detail_view = client.get(f"/lab/orders/{order_id}",
                             headers=CLINICIAN).json()
    assert detail_view["order"]["status"] == "in-process"
    assert detail_view["reports"] == []

    # ONE payload, patient entry FIRST: controls are processed first,
    # open the gate, and the patient result lands in the same call
    plate_now = _plate(client, plate["id"])
    payload = [{"position": "B1", "value": 95}] + _passing_controls(plate_now)
    out = _post_results(client, plate["id"], payload).json()
    patient = next(p for p in out["processed"] if p["position"] == "B1")
    assert patient["result_status"] == "resulted"
    assert patient["order_id"] == order_id and patient["report_id"]
    assert out["plate"]["qc"]["gate_open"] is True

    # the result is on the ORDER, through the shared result path
    detail_view = client.get(f"/lab/orders/{order_id}",
                             headers=CLINICIAN).json()
    assert detail_view["order"]["status"] == "resulted"
    assert [rep["id"] for rep in detail_view["reports"]] == \
        [patient["report_id"]]
    well = _well(_plate(client, plate["id"]), "B1")
    assert well["result_status"] == "resulted"
    assert well["result_value"] == 95
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    item = next(i for i in queue["items"]
                if i["report"]["id"] == patient["report_id"])
    assert item["results"][0]["code"] == GLUCOSE
    assert item["results"][0]["value"] == 95


def test_gate_stays_closed_when_a_control_failed(client):
    _patient(client)
    _order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    _assign(client, plate["id"], specimen_id)
    entries = _passing_controls(_plate(client, plate["id"]))
    entries[0] = {"position": entries[0]["position"], "value": 99999}
    _post_results(client, plate["id"], entries)
    plate = _plate(client, plate["id"])
    assert plate["qc"]["controls_failed"] == 1
    assert plate["qc"]["gate_open"] is False
    _post_results(client, plate["id"],
                  [{"position": "B1", "value": 95}], expect=409)


# ---------------------------------------------------------------------------
# The QC gate: direct result entry
# ---------------------------------------------------------------------------

def test_direct_entry_blocked_for_plated_specimen_until_gate_opens(client):
    _patient(client)
    order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    _assign(client, plate["id"], specimen_id)

    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text
    assert f"plate {plate['id']}" in r.json()["detail"]
    assert "plate QC gate: controls not yet passed" in r.json()["detail"]

    # a NON-plated order is completely unaffected by the gate
    other_order, _other_spec = _specimen(client)
    r = client.post(f"/lab/orders/{other_order}/results",
                    json={"results": [{"code": GLUCOSE, "value": 88}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text

    # once the plate's controls pass, direct entry on the plated
    # order works again
    _open_gate(client, plate["id"])
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Completion rules
# ---------------------------------------------------------------------------

def test_complete_rules(client):
    _patient(client)
    order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    _assign(client, plate["id"], specimen_id)
    # an assigned-but-unresulted patient well blocks completion
    r = client.post(f"/lab/plates/{plate['id']}/complete", headers=CLINICIAN)
    assert r.status_code == 409 and "B1" in r.json()["detail"]
    # result the well (gate opened by the same payload's controls)
    payload = [{"position": "B1", "value": 95}] + \
        _passing_controls(_plate(client, plate["id"]))
    _post_results(client, plate["id"], payload)
    r = client.post(f"/lab/plates/{plate['id']}/complete", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "complete"
    assert r.json()["completed_by"] == "dev-clinician"
    # completing twice conflicts; results on a complete plate refuse
    assert client.post(f"/lab/plates/{plate['id']}/complete",
                       headers=CLINICIAN).status_code == 409
    _post_results(client, plate["id"],
                  [{"position": "A1", "value": 100}], expect=409)
    # a plate whose patient wells were never assigned completes fine
    empty = _create_plate(client).json()
    r = client.post(f"/lab/plates/{empty['id']}/complete", headers=CLINICIAN)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def test_plate_actions_are_audited(client):
    _patient(client)
    _order_id, specimen_id = _specimen(client)
    plate = _create_plate(client).json()
    _assign(client, plate["id"], specimen_id)
    payload = [{"position": "B1", "value": 95}] + \
        _passing_controls(_plate(client, plate["id"]))
    _post_results(client, plate["id"], payload)
    client.post(f"/lab/plates/{plate['id']}/complete", headers=CLINICIAN)
    factory = client.app.state.session_factory
    with factory() as session:
        actions = [row.action for row in session.scalars(
            select(AuditLog)).all()]
        for expected in ("lab.plate.create", "lab.plate.assign",
                         "lab.plate.results", "lab.plate.complete",
                         "lab.results.create"):
            assert expected in actions, expected
        assert verify_chain(session) is True
    # patients never see plates
    assert client.get("/lab/plates", headers=PATIENT).status_code == 403
