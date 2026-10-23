"""UX pass (WS3a) — test detail: GET /lab/orders/{id}/test-detail.

The full synthetic journey (order -> collect -> receive -> in-process
-> aliquot -> plate -> controls -> plate result -> verify -> release
-> notify -> acknowledge) asserted against the composed detail view,
plus the non-plated shape. Synthetic data only."""
from tests.conftest import CLINICIAN, PATIENT, VIEWER

GLUCOSE = "2345-7"
POTASSIUM = "2823-3"
STEP_KEYS = ["ordered", "collected", "received", "plated",
             "in_analysis", "analysed", "verified", "resulted"]


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic",
                                    "given": ["Detail"]}]},
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


def _transition(client, order_id, to_status, expect=200):
    r = client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": to_status}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _to_in_process(client, order_id):
    _transition(client, order_id, "collected")
    _transition(client, order_id, "received")
    _transition(client, order_id, "in-process")


def _detail(client, order_id, headers=CLINICIAN, expect=200):
    r = client.get(f"/lab/orders/{order_id}/test-detail", headers=headers)
    assert r.status_code == expect, r.text
    return r


def _steps(detail):
    return {s["key"]: s for s in detail["steps"]}


def _release(client, report_id):
    r = client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    assert r.status_code == 200, r.text


def _passing_controls(plate):
    entries = []
    for well in plate["wells"]:
        if well["role"] in ("control-pos", "control-neg", "calibrator",
                            "blank"):
            expected = well["expected"] or {}
            value = expected["result"] if "result" in expected else \
                (expected["low"] + expected["high"]) / 2
            entries.append({"position": well["position"], "value": value})
    return entries


# ---------------------------------------------------------------------------
# The full journey
# ---------------------------------------------------------------------------

def test_test_detail_full_plated_journey(client):
    _patient(client)
    # Baseline: a RELEASED potassium of 4.0 for pat-1 — the delta
    # baseline (potassium carries delta {pct: 20} in the catalog).
    baseline = _order(client, POTASSIUM)
    _to_in_process(client, baseline["id"])
    r = client.post(f"/lab/orders/{baseline['id']}/results",
                    json={"results": [{"code": POTASSIUM, "value": 4.0}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    _release(client, r.json()["report"]["id"])

    # The journey order: collect -> receive -> in-process.
    order = _order(client, POTASSIUM)
    _to_in_process(client, order["id"])
    detail0 = client.get(f"/lab/orders/{order['id']}",
                         headers=CLINICIAN).json()
    specimen_id = detail0["specimen"]["id"]

    # An aliquot is derived from the specimen (lineage).
    r = client.post(f"/lab/specimens/{specimen_id}/derive",
                    json={"type": "aliquot", "count": 1, "volume_ml": 2.0},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    aliquot = r.json()["children"][0]

    # Plated on a chem-96 run.
    plate = client.post("/lab/plates", json={"template_code": "chem-96"},
                        headers=CLINICIAN).json()
    r = client.post(f"/lab/plates/{plate['id']}/assign",
                    json={"specimen_ref": specimen_id}, headers=CLINICIAN)
    assert r.status_code == 200, r.text

    # Mid-journey detail: the plated step exists, analysis is next.
    detail = _detail(client, order["id"]).json()
    assert [s["key"] for s in detail["steps"]] == STEP_KEYS
    steps = _steps(detail)
    for key in ("ordered", "collected", "received", "plated",
                "in_analysis"):
        assert steps[key]["state"] == "done", key
    assert steps["plated"]["at"] is not None
    assert steps["plated"]["plate_id"] == plate["id"]
    assert steps["analysed"]["state"] == "current"
    assert steps["verified"]["state"] == "pending"
    assert steps["resulted"]["state"] == "pending"
    assert detail["order"]["id"] == order["id"]
    assert detail["order"]["code"] == POTASSIUM
    assert detail["specimen"]["id"] == specimen_id
    assert detail["specimen"]["accession"].startswith("ACC-")
    assert len(detail["aliquots"]) == 1
    assert detail["aliquots"][0]["specimen_id"] == aliquot["specimen_id"]
    assert detail["aliquots"][0]["derivation_type"] == "aliquot"
    assert detail["results"] == [] and detail["report"] is None
    assert detail["acknowledgements"] == []
    assert detail["custody"]["collected_at"] is not None
    assert detail["custody"]["in_analysis_at"] is not None

    # Controls pass, then the patient result lands VIA THE PLATE:
    # potassium 6.4 — critical high (HH) and +60% over the 4.0
    # baseline (delta pct threshold 20).
    plate_now = client.get(f"/lab/plates/{plate['id']}",
                           headers=CLINICIAN).json()
    r = client.post(f"/lab/plates/{plate['id']}/results",
                    json={"results": _passing_controls(plate_now)},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/plates/{plate['id']}/results",
                    json={"results": [{"position": "B1", "value": 6.4}]},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    report_id = r.json()["processed"][0]["report_id"]

    # Resulted detail: result rows carry range / interpretation /
    # critical / delta, recomputed against the released baseline.
    detail = _detail(client, order["id"]).json()
    assert len(detail["results"]) == 1
    result = detail["results"][0]
    assert result["analyte"] == POTASSIUM and result["value"] == 6.4
    assert result["unit"] == "mmol/L"
    assert result["reference_range"] == {"low": 3.5, "high": 5.1,
                                         "unit": "mmol/L"}
    assert result["interpretation"] == "HH"
    assert result["critical"] is True
    assert result["status"] == "preliminary"
    assert result["delta"] is not None
    assert result["delta"]["previous_value"] == 4.0
    assert result["delta"]["current_value"] == 6.4
    steps = _steps(detail)
    assert steps["analysed"]["state"] == "done"
    assert steps["analysed"]["at"] is not None
    assert steps["verified"]["state"] == "current"
    assert detail["report"]["report_id"] == report_id
    assert detail["report"]["status"] == "preliminary"
    assert detail["report"]["verified_by"] is None
    assert detail["report"]["released_at"] is None

    # Verify -> release -> notify -> acknowledge.
    _release(client, report_id)
    r = client.post(f"/lab/reports/{report_id}/notify",
                    json={"notified_to": "Dr Synthetic (attending)",
                          "read_back": True}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/reports/{report_id}/acknowledge",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text

    detail = _detail(client, order["id"]).json()
    steps = _steps(detail)
    assert [s["key"] for s in detail["steps"]] == STEP_KEYS
    assert all(s["state"] == "done" for s in detail["steps"]), detail["steps"]
    assert steps["verified"]["by"] == "dev-clinician"
    assert steps["verified"]["at"] is not None
    assert steps["resulted"]["at"] is not None
    assert detail["report"]["status"] == "final"
    assert detail["report"]["verified_by"] == "dev-clinician"
    assert detail["report"]["verified_at"] is not None
    assert detail["report"]["released_at"] is not None
    assert detail["report"]["critical"] is True
    # the delta flag survives release (the report's own observations
    # are excluded from the recomputed baseline)
    assert detail["results"][0]["delta"]["previous_value"] == 4.0
    assert detail["results"][0]["status"] == "final"
    # the acknowledgement trail
    assert len(detail["acknowledgements"]) == 1
    ack = detail["acknowledgements"][0]
    assert ack["state"] == "acknowledged"
    assert ack["notified_to"] == "Dr Synthetic (attending)"
    assert ack["notified_at"] is not None
    assert ack["acknowledged_by"] == "dev-clinician"
    assert ack["acknowledged_at"] is not None


# ---------------------------------------------------------------------------
# Non-plated orders + early stages
# ---------------------------------------------------------------------------

def test_test_detail_non_plated_order_has_no_plated_step(client):
    _patient(client)
    order = _order(client, GLUCOSE)
    _to_in_process(client, order["id"])
    r = client.post(f"/lab/orders/{order['id']}/results",
                    json={"results": [{"code": GLUCOSE, "value": 95}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    detail = _detail(client, order["id"]).json()
    keys = [s["key"] for s in detail["steps"]]
    assert keys == [k for k in STEP_KEYS if k != "plated"]
    assert "plated" not in keys
    assert detail["aliquots"] == []
    assert detail["acknowledgements"] == []
    result = detail["results"][0]
    assert result["analyte"] == GLUCOSE and result["value"] == 95
    assert result["reference_range"]["low"] == 70.0
    assert result["reference_range"]["high"] == 99.0
    assert result["interpretation"] == "N"
    assert result["critical"] is False
    assert result["delta"] is None  # glucose carries no delta config
    assert detail["report"]["status"] == "preliminary"
    steps = _steps(detail)
    assert steps["analysed"]["state"] == "done"
    assert steps["verified"]["state"] == "current"
    assert steps["resulted"]["state"] == "pending"


def test_test_detail_just_ordered(client):
    _patient(client)
    order = _order(client, GLUCOSE)
    detail = _detail(client, order["id"]).json()
    steps = _steps(detail)
    assert steps["ordered"]["state"] == "done"
    assert steps["ordered"]["at"] is not None
    assert steps["collected"]["state"] == "current"
    assert steps["received"]["state"] == "pending"
    assert steps["analysed"]["state"] == "pending"
    assert detail["specimen"] is None
    assert detail["aliquots"] == []
    assert detail["results"] == []
    assert detail["report"] is None
    assert detail["acknowledgements"] == []
    assert detail["custody"]["ordered_at"] is not None
    assert detail["custody"]["collected_at"] is None


def test_test_detail_role_gates_and_unknown_order(client):
    _patient(client)
    order = _order(client, GLUCOSE)
    assert client.get(f"/lab/orders/{order['id']}/test-detail",
                      headers=PATIENT).status_code == 403
    assert client.get(f"/lab/orders/{order['id']}/test-detail").status_code == 401
    assert client.get(f"/lab/orders/{order['id']}/test-detail",
                      headers=VIEWER).status_code == 200
    _detail(client, "no-such-order", expect=404)
