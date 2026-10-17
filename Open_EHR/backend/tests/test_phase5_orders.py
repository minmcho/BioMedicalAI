"""Phase 5 — general orders / CPOE (backend/app/orders.py).

Order types, the order lifecycle and order sets are Studio
configuration; these tests pin the ENGINE behaviour over the defaults
and prove a published workflow variant changes behaviour without code
changes. Decision support is honest and config-driven: allergy hard
stop with a recorded override, duplicate warning flag (never a stop).
Drug-interaction checking is NOT implemented (no interaction
database) — nothing here pretends otherwise.
"""
from tests.conftest import ADMIN, CLINICIAN, PATIENT, VIEWER

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _patient(client, pid="ord-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Order"]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _place(client, body, headers=CLINICIAN):
    return client.post("/orders", json=body, headers=headers)


def _transition(client, oid, to, headers=CLINICIAN, **extra):
    return client.post(f"/orders/{oid}/transition",
                       json={"to_status": to, **extra}, headers=headers)


def _allergy(client, pid, code="847232", display="Metformin"):
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"alg-{pid}-{code}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": RXNORM, "code": code,
                             "display": display}]},
        "patient": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _publish_config(client, kind, key, payload):
    # The config create endpoint takes the payload as the raw body.
    r = client.post(f"/studio/config/{kind}/{key}",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Catalogue + placement
# ---------------------------------------------------------------------------

def test_order_types_come_from_published_config(client):
    r = client.get("/orders/types", headers=CLINICIAN)
    assert r.status_code == 200
    codes = {c["code"] for c in r.json()["order_types"]["concepts"]}
    assert {"medication", "procedure", "consult", "imaging", "nursing",
            "diet", "activity"} <= codes


def test_place_procedure_order_starts_draft(client):
    pid = _patient(client)
    r = _place(client, {"type": "procedure", "code": "93005",
                        "display": "ECG 12-lead", "patient": pid})
    assert r.status_code == 201, r.text
    order = r.json()["order"]
    assert order["resourceType"] == "ServiceRequest"
    assert order["status"] == "draft"
    assert r.json()["warnings"] == []


def test_unknown_order_type_is_422(client):
    pid = _patient(client)
    r = _place(client, {"type": "teleportation", "code": "x", "patient": pid})
    assert r.status_code == 422
    assert "teleportation" in r.json()["detail"]


def test_missing_required_field_is_422(client):
    pid = _patient(client)
    # Consult orders require a performer role (config: requiredFields).
    r = _place(client, {"type": "consult", "code": "cardio-consult",
                        "patient": pid})
    assert r.status_code == 422
    assert "performerRole" in r.json()["detail"]
    # Imaging orders require a modality.
    r = _place(client, {"type": "imaging", "code": "71046", "patient": pid})
    assert r.status_code == 422
    assert "modality" in r.json()["detail"]


def test_order_for_unknown_patient_is_404(client):
    r = _place(client, {"type": "procedure", "code": "93005",
                        "patient": "no-such-patient"})
    assert r.status_code == 404


def test_viewer_and_patient_cannot_place_orders(client):
    pid = _patient(client)
    body = {"type": "procedure", "code": "93005", "patient": pid}
    assert _place(client, body, headers=VIEWER).status_code == 403
    assert _place(client, body, headers=PATIENT).status_code == 403


# ---------------------------------------------------------------------------
# Lifecycle + fulfilment visibility
# ---------------------------------------------------------------------------

def test_unsigned_order_hidden_from_fulfilment_queue(client):
    pid = _patient(client)
    oid = _place(client, {"type": "procedure", "code": "93005",
                          "patient": pid}).json()["order"]["id"]
    queue = client.get("/orders/queue", headers=CLINICIAN).json()
    assert all(o["id"] != oid for o in queue["items"])
    r = _transition(client, oid, "signed")
    assert r.status_code == 200 and r.json()["status"] == "signed"
    queue = client.get("/orders/queue", headers=CLINICIAN).json()
    assert any(o["id"] == oid for o in queue["items"])
    # Discontinuing removes it from the queue again.
    assert _transition(client, oid, "discontinued").status_code == 200
    queue = client.get("/orders/queue", headers=CLINICIAN).json()
    assert all(o["id"] != oid for o in queue["items"])


def test_illegal_transition_is_409_and_unknown_state_422(client):
    pid = _patient(client)
    oid = _place(client, {"type": "nursing", "code": "vital-signs",
                          "patient": pid}).json()["order"]["id"]
    assert _transition(client, oid, "completed").status_code == 409
    assert _transition(client, oid, "flying").status_code == 422


def test_workflow_variant_changes_lifecycle_without_code(client):
    """Publish an order-flow variant with no draft -> cancelled edge:
    cancelling a draft must then be a 409 — behaviour follows config."""
    pid = _patient(client)
    base = client.get("/studio/config/workflow/effective/order-flow",
                      headers=ADMIN).json()["payload"]
    variant = {
        "states": base["states"],
        "transitions": [t for t in base["transitions"]
                        if not (t["from"] == "draft"
                                and t["to"] == "cancelled")],
        "transitionsRoles": base.get("transitionsRoles", {}),
    }
    _publish_config(client, "workflow", "order-flow", variant)
    oid = _place(client, {"type": "nursing", "code": "vital-signs",
                          "patient": pid}).json()["order"]["id"]
    assert _transition(client, oid, "cancelled").status_code == 409
    # Signing (still in the variant) works.
    assert _transition(client, oid, "signed").status_code == 200


# ---------------------------------------------------------------------------
# Decision support: allergy hard stop + override; duplicate warning
# ---------------------------------------------------------------------------

def test_allergy_blocks_medication_order_naming_allergen(client):
    pid = _patient(client)
    _allergy(client, pid)
    r = _place(client, {"type": "medication", "code": "847232",
                        "patient": pid})
    assert r.status_code == 422
    assert "Metformin" in r.json()["detail"]


def test_allergy_override_with_reason_is_recorded(client):
    pid = _patient(client)
    _allergy(client, pid)
    r = _place(client, {"type": "medication", "code": "847232",
                        "patient": pid,
                        "allergy_override_reason": "Tolerated since 2024"})
    assert r.status_code == 201, r.text
    assert any(w.startswith("allergy-overridden")
               for w in r.json()["warnings"])
    exts = {e["url"]: e.get("valueString")
            for e in r.json()["order"].get("extension", [])}
    assert exts["urn:opencare:extension/allergy-override"] == \
        "Tolerated since 2024"
    # The recorded override also carries the signature.
    oid = r.json()["order"]["id"]
    assert _transition(client, oid, "signed").status_code == 200


def test_allergy_recorded_after_placement_blocks_signing(client):
    pid = _patient(client)
    oid = _place(client, {"type": "medication", "code": "847232",
                          "patient": pid}).json()["order"]["id"]
    _allergy(client, pid)
    r = _transition(client, oid, "signed")
    assert r.status_code == 422
    assert "Metformin" in r.json()["detail"]
    # Signing WITH a reason proceeds and records it on the order.
    r = _transition(client, oid, "signed",
                    allergy_override_reason="Benefits outweigh risk")
    assert r.status_code == 200, r.text
    exts = {e["url"]: e.get("valueString")
            for e in r.json().get("extension", [])}
    assert exts["urn:opencare:extension/allergy-override"] == \
        "Benefits outweigh risk"


def test_inactive_allergy_does_not_block(client):
    pid = _patient(client)
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"alg-inactive-{pid}",
        "clinicalStatus": {"coding": [{"code": "resolved"}]},
        "code": {"coding": [{"system": RXNORM, "code": "847232",
                             "display": "Metformin"}]},
        "patient": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201
    r = _place(client, {"type": "medication", "code": "847232",
                        "patient": pid})
    assert r.status_code == 201


def test_duplicate_active_order_warns_but_never_blocks(client):
    pid = _patient(client)
    body = {"type": "medication", "code": "1049221", "display": "Lisinopril",
            "patient": pid}
    first = _place(client, body)
    assert first.status_code == 201 and first.json()["warnings"] == []
    second = _place(client, body)
    assert second.status_code == 201
    assert any(w.startswith("duplicate-active-order")
               for w in second.json()["warnings"])


# ---------------------------------------------------------------------------
# Order sets
# ---------------------------------------------------------------------------

def test_order_set_expands_into_individual_orders(client):
    pid = _patient(client)
    r = client.post("/orders/from-set",
                    json={"set": "diabetes-admission", "patient": pid},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["total"] == 4
    orders = r.json()["orders"]
    assert all(o["status"] == "draft" for o in orders)
    types = {o["resourceType"] for o in orders}
    assert types == {"MedicationRequest", "ServiceRequest"}
    # Each expanded order is individually signable / discontinuable.
    first, second = orders[0]["id"], orders[1]["id"]
    assert _transition(client, first, "signed").status_code == 200
    assert _transition(client, second, "cancelled").status_code == 200
    listed = client.get(f"/orders?patient={pid}", headers=CLINICIAN).json()
    assert listed["total"] == 4


def test_order_set_validates_all_items_before_creating_any(client):
    pid = _patient(client)
    _allergy(client, pid)  # diabetes-admission contains Metformin 847232
    r = client.post("/orders/from-set",
                    json={"set": "diabetes-admission", "patient": pid},
                    headers=CLINICIAN)
    assert r.status_code == 422
    listed = client.get(f"/orders?patient={pid}", headers=CLINICIAN).json()
    assert listed["total"] == 0  # nothing half-placed


def test_unknown_order_set_is_422(client):
    pid = _patient(client)
    r = client.post("/orders/from-set",
                    json={"set": "no-such-set", "patient": pid},
                    headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Consults: Task + receiving-role inbox
# ---------------------------------------------------------------------------

def test_consult_creates_task_and_signed_consult_reaches_inbox(client):
    pid = _patient(client)
    r = _place(client, {"type": "consult", "code": "cardiology-consult",
                        "display": "Cardiology consult",
                        "performer_role": "clinician", "patient": pid})
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    tasks = client.get(f"/fhir/Task?patient={pid}",
                       headers=CLINICIAN).json()
    assert tasks["total"] == 1
    assert tasks["entry"][0]["resource"]["status"] == "requested"
    # Unsigned: not in the receiving role's inbox yet.
    inbox = client.get("/orders/inbox?role=clinician",
                       headers=CLINICIAN).json()
    assert all(i["order"]["id"] != oid for i in inbox["items"])
    assert _transition(client, oid, "signed").status_code == 200
    inbox = client.get("/orders/inbox?role=clinician",
                       headers=CLINICIAN).json()
    assert any(i["order"]["id"] == oid for i in inbox["items"])
    # Completing the order completes the Task.
    _transition(client, oid, "active")
    assert _transition(client, oid, "completed").status_code == 200
    tasks = client.get(f"/fhir/Task?patient={pid}",
                       headers=CLINICIAN).json()
    assert tasks["entry"][0]["resource"]["status"] == "completed"
    inbox = client.get("/orders/inbox?role=clinician",
                       headers=CLINICIAN).json()
    assert all(i["order"]["id"] != oid for i in inbox["items"])


# ---------------------------------------------------------------------------
# Imaging orders link into the radiology flow
# ---------------------------------------------------------------------------

def test_imaging_order_feeds_radiology_board(client):
    pid = _patient(client)
    r = _place(client, {"type": "imaging", "code": "71046",
                        "display": "XR chest 2 views", "modality": "DX",
                        "patient": pid})
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    assert _transition(client, oid, "signed").status_code == 200
    r = client.post("/radiology/studies",
                    json={"service_request_id": oid, "modality": "DX"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # ImagingStudy lifecycle (Phase 4B): a created study is "available".
    assert r.json()["study"]["status"] == "available"
