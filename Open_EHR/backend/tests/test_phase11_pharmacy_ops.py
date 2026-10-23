"""Phase 11 (Track C/J): pharmacy department depth.

Lot-level inventory with PAR config, DSCSA procurement/receiving into a
traceability ledger, ADC restock lists, controlled substances (two-person
counts, witnessed waste, discrepancy resolution), sterile compounding
with config-computed beyond-use dates, the pharmacist verification +
DUR work queues with the rules-engine renal flag, the inert
drug-interaction integration point, and the Phase 11 compliance seeds
(DEA / USP / DSCSA register entries + draft packs + DUR agent draft).
"""
import datetime as dt

from .conftest import ADMIN, CLINICIAN, VIEWER

RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
LISINOPRIL = "1049221"
ATORVASTATIN = "1044910"
APIXABAN = "1351886"
OXYCODONE = "1049625"


def _patient(client, pid, birth="1980-01-01", gender="female"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Test", "given": ["P11"]}],
        "birthDate": birth, "gender": gender}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _med_order(client, pid, code=LISINOPRIL, display="Lisinopril 10 MG Oral Tablet",
               *, priority=None):
    body = {"type": "medication", "code": code, "code_display": display,
            "patient": pid}
    if priority:
        body["priority"] = priority
    r = client.post("/orders", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["order"]["id"]


def _sign_activate(client, oid):
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _audit_actions(client):
    return client.get("/studio/audit?limit=200", headers=ADMIN).json()


def _publish_config(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/{kind}/{key}/versions/{version}/publish", headers=ADMIN)
    assert r.status_code == 200, r.text


# ------------------------------------------------------------ inventory

def test_lots_seeded_consistent_with_aggregate_stock(client):
    stock = {i["code"]: i["quantity"] for i in
             client.get("/pharmacy/stock", headers=CLINICIAN).json()["items"]}
    inv = client.get("/pharmacy/inventory", headers=CLINICIAN).json()
    assert {loc["code"] for loc in inv["locations"]} == {
        "main-pharmacy", "iv-room", "adc-icu", "adc-med-surg"}
    lot_sums = {}
    for lot in inv["items"]:
        assert lot["expiry_date"]  # every seeded lot carries an expiry
        lot_sums[lot["medication_code"]] = (
            lot_sums.get(lot["medication_code"], 0) + lot["quantity"])
    for code, quantity in stock.items():
        if quantity:  # stock-tracked concepts with a balance
            assert lot_sums.get(code) == quantity


def test_inventory_adjust_audited_and_bounded(client):
    body = {"medication_code": ATORVASTATIN, "location": "main-pharmacy",
            "lot_number": f"SEED-{ATORVASTATIN}", "delta": -5,
            "reason": "Breakage during count"}
    r = client.post("/pharmacy/inventory/adjust", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["lot"]["quantity"] == 85
    assert r.json()["stock_after"] == 85
    stock = {i["code"]: i["quantity"] for i in
             client.get("/pharmacy/stock", headers=CLINICIAN).json()["items"]}
    assert stock[ATORVASTATIN] == 85
    actions = [e["action"] for e in _audit_actions(client)]
    assert "pharmacy.inventory.adjust" in actions
    # Cannot adjust below zero, unknown lots are refused, reason required.
    r = client.post("/pharmacy/inventory/adjust",
                    json={**body, "delta": -1000}, headers=CLINICIAN)
    assert r.status_code == 409
    r = client.post("/pharmacy/inventory/adjust",
                    json={**body, "lot_number": "NOPE"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/pharmacy/inventory/adjust",
                    json={**body, "reason": "  "}, headers=CLINICIAN)
    assert r.status_code == 422


def test_worklist_low_stock_and_expiring(client):
    soon = (dt.date.today() + dt.timedelta(days=10)).isoformat()
    po = client.post("/pharmacy/procurement/orders", json={
        "supplier": "Synthetic Wholesale",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 5}]},
        headers=CLINICIAN).json()
    r = client.post(f"/pharmacy/procurement/orders/{po['po_id']}/receive",
                    json={"lines": [{"medication_code": LISINOPRIL,
                                     "quantity": 5, "lot_number": "LOT-SOON",
                                     "expiry_date": soon}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Push atorvastatin below its reorder point (90 -> 25, reorder 30).
    client.post("/pharmacy/inventory/adjust", json={
        "medication_code": ATORVASTATIN, "location": "main-pharmacy",
        "lot_number": f"SEED-{ATORVASTATIN}", "delta": -65,
        "reason": "Dispensing surge"}, headers=CLINICIAN)
    wl = client.get("/pharmacy/inventory/worklist", headers=CLINICIAN).json()
    low = {i["medication_code"]: i for i in wl["low_stock"]}
    assert low[ATORVASTATIN]["on_hand"] == 25
    assert low[ATORVASTATIN]["suggested_order"] == 55  # PAR 80 - 25
    expiring = {i["lot_number"] for i in wl["expiring"]}
    assert "LOT-SOON" in expiring


# ---------------------------------------------------------- procurement

def test_procurement_receiving_dscsa_status_flow(client):
    po = client.post("/pharmacy/procurement/orders", json={
        "supplier": "Synthetic Wholesale",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 5}]},
        headers=CLINICIAN)
    assert po.status_code == 201, po.text
    po_id = po.json()["po_id"]
    serials = [f"SN-{i:04d}" for i in range(3)]
    r = client.post(f"/pharmacy/procurement/orders/{po_id}/receive", json={
        "lines": [{"medication_code": LISINOPRIL, "quantity": 3,
                   "lot_number": "LOT-A", "expiry_date": "2028-01-01",
                   "serials": serials}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "partially_received"
    ledger = client.get("/pharmacy/procurement/traceability",
                        headers=CLINICIAN).json()["items"]
    receiving = [e for e in ledger if e["event"] == "receiving"]
    assert any(e["lot_number"] == "LOT-A" and e["detail"]["serials"] == serials
               for e in receiving)
    r = client.post(f"/pharmacy/procurement/orders/{po_id}/receive", json={
        "lines": [{"medication_code": LISINOPRIL, "quantity": 2,
                   "lot_number": "LOT-A", "expiry_date": "2028-01-01"}]},
        headers=CLINICIAN)
    assert r.json()["status"] == "received"
    r = client.post(f"/pharmacy/procurement/orders/{po_id}/receive", json={
        "lines": [{"medication_code": LISINOPRIL, "quantity": 1,
                   "lot_number": "LOT-A", "expiry_date": "2028-01-01"}]},
        headers=CLINICIAN)
    assert r.status_code == 409  # fully received already


def test_receiving_validates_before_writing(client):
    po = client.post("/pharmacy/procurement/orders", json={
        "supplier": "Synthetic Wholesale",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 10}]},
        headers=CLINICIAN).json()
    url = f"/pharmacy/procurement/orders/{po['po_id']}/receive"
    # Over-receipt refused, and no lot is written by the failed call.
    r = client.post(url, json={"lines": [{
        "medication_code": LISINOPRIL, "quantity": 11,
        "lot_number": "LOT-BAD", "expiry_date": "2028-01-01"}]}, headers=CLINICIAN)
    assert r.status_code == 422
    lots = client.get("/pharmacy/inventory", headers=CLINICIAN).json()["items"]
    assert not any(l["lot_number"] == "LOT-BAD" for l in lots)
    # Serial count must equal quantity.
    r = client.post(url, json={"lines": [{
        "medication_code": LISINOPRIL, "quantity": 4, "lot_number": "LOT-OK",
        "expiry_date": "2028-01-01", "serials": ["S1", "S2"]}]}, headers=CLINICIAN)
    assert r.status_code == 422
    # A clean receipt then succeeds in full — nothing was half-written.
    r = client.post(url, json={"lines": [{
        "medication_code": LISINOPRIL, "quantity": 10, "lot_number": "LOT-OK",
        "expiry_date": "2028-01-01"}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "received"


def test_adc_restock_list_from_par_and_receiving(client):
    restock = client.get("/pharmacy/adc/restock-list",
                         params={"location": "adc-icu"},
                         headers=CLINICIAN).json()
    items = {i["medication_code"]: i for i in restock["items"]}
    assert items[LISINOPRIL]["on_hand"] == 0
    assert items[LISINOPRIL]["suggested_quantity"] == 20  # PAR 20
    # Receiving stock INTO the ADC clears its restock suggestion.
    po = client.post("/pharmacy/procurement/orders", json={
        "supplier": "Synthetic Wholesale",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 20}]},
        headers=CLINICIAN).json()
    r = client.post(f"/pharmacy/procurement/orders/{po['po_id']}/receive", json={
        "location": "adc-icu",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 20,
                   "lot_number": "LOT-ADC", "expiry_date": "2028-06-01"}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    restock = client.get("/pharmacy/adc/restock-list",
                         params={"location": "adc-icu"},
                         headers=CLINICIAN).json()
    assert all(i["medication_code"] != LISINOPRIL for i in restock["items"])


def test_dispense_allocates_fefo_lot_and_consumes_serials(client):
    pid = _patient(client, "pat-p11-dispense")
    soon = (dt.date.today() + dt.timedelta(days=20)).isoformat()
    serials = ["SN-A", "SN-B", "SN-C"]
    po = client.post("/pharmacy/procurement/orders", json={
        "supplier": "Synthetic Wholesale",
        "lines": [{"medication_code": LISINOPRIL, "quantity": 3}]},
        headers=CLINICIAN).json()
    client.post(f"/pharmacy/procurement/orders/{po['po_id']}/receive", json={
        "lines": [{"medication_code": LISINOPRIL, "quantity": 3,
                   "lot_number": "LOT-FEFO", "expiry_date": soon,
                   "serials": serials}]}, headers=CLINICIAN)
    oid = _med_order(client, pid)
    _sign_activate(client, oid)
    # Wave A (PHARM-B1): dispensing requires a current verification.
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/pharmacy/dispense", json={
        "medication_request_id": oid, "quantity": 2, "status": "active"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    ext = {(e["url"]): e.get("valueString") for e in
           r.json()["dispense"].get("extension", [])}
    serials_out = [e.get("valueString") for e in
                   r.json()["dispense"].get("extension", [])
                   if e["url"] == "urn:opencare:extension/dispense-serial"]
    # FEFO: the received lot expires before the seeded lot (+365d).
    assert ext["urn:opencare:extension/dispense-lot"] == "LOT-FEFO"
    assert serials_out == ["SN-A, SN-B"]  # one extension, joined
    ledger = client.get("/pharmacy/procurement/traceability",
                        headers=CLINICIAN).json()["items"]
    dispensing = [e for e in ledger if e["event"] == "dispense"]
    assert dispensing and dispensing[0]["detail"]["serials"] == ["SN-A", "SN-B"]
    lot = [l for l in client.get("/pharmacy/inventory",
                                 headers=CLINICIAN).json()["items"]
           if l["lot_number"] == "LOT-FEFO"][0]
    assert lot["quantity"] == 1


# ---------------------------------------------------------- verification

def test_verification_queue_restricted_ack_flow(client):
    pid = _patient(client, "pat-p11-apix")
    oid = _med_order(client, pid, APIXABAN,
                     "Apixaban 5 MG Oral Tablet [Eliquis]")
    _sign_activate(client, oid)
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == oid][0]
    assert item["formulary_status"] == "restricted"
    assert "stewardship" in item["restriction_note"]
    url = f"/pharmacy/verification/{oid}/verify"
    r = client.post(url, json={}, headers=CLINICIAN)
    assert r.status_code == 422  # restriction must be acknowledged
    # Wave A (PHARM-M1): apixaban is also high-alert in the seeded
    # formulary, so verification past the hard stop needs a governed
    # override (reason code from the published pharmacy-ops list +
    # a note) on top of the restriction acknowledgement.
    r = client.post(url, json={"restriction_acknowledged": True},
                    headers=CLINICIAN)
    assert r.status_code == 422  # high-alert hard stop, no override yet
    assert "high-alert" in r.json()["detail"]
    r = client.post(url, json={
        "restriction_acknowledged": True,
        "override_reason_code": "benefit-outweighs-risk",
        "override_note": "Indication confirmed; renal function reviewed."},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["verification"]["verified_by"] == "dev-clinician"
    assert r.json()["verification"]["formulary_status"] == "restricted"
    assert r.json()["verification"]["override_reason_code"] == \
        "benefit-outweighs-risk"
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    assert all(i["order_id"] != oid for i in queue["items"])
    r = client.post(url, json={"restriction_acknowledged": True},
                    headers=CLINICIAN)
    assert r.status_code == 409  # already verified


def test_verification_non_formulary_blocked_and_config_driven(client):
    pid = _patient(client, "pat-p11-nonform")
    # Sertraline has no formulary concept -> non-formulary by default.
    oid = _med_order(client, pid, "1049651", "Sertraline 50 MG Oral Tablet")
    _sign_activate(client, oid)
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == oid][0]
    assert item["formulary_status"] == "non-formulary"
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422
    # Marking a concept non-formulary in a published formulary version
    # flips the same endpoint's behaviour — configuration, not code.
    oid2 = _med_order(client, pid)
    _sign_activate(client, oid2)
    effective = client.get("/studio/config/terminology/effective/formulary",
                           headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    for concept in payload["concepts"]:
        if concept["code"] == LISINOPRIL:
            concept["formularyStatus"] = "non-formulary"
    _publish_config(client, "terminology", "formulary", payload)
    r = client.post(f"/pharmacy/verification/{oid2}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 422


def test_verification_queue_priority_and_sla_breach(client, monkeypatch):
    import app.pharmacy_ops as ops
    pid = _patient(client, "pat-p11-sla")
    routine = _med_order(client, pid)
    _sign_activate(client, routine)
    stat = _med_order(client, pid, "847232",
                      "Metformin hydrochloride 500 MG Oral Tablet",
                      priority="stat")
    _sign_activate(client, stat)
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    ids = [i["order_id"] for i in queue["items"]]
    assert ids.index(stat) < ids.index(routine)
    # Two hours later the stat order (30-minute SLA) is in breach.
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=2)
    monkeypatch.setattr(ops, "_now", lambda: future)
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == stat][0]
    assert item["sla_breached"] is True
    assert item["wait_minutes"] >= 119


def test_dur_queue_allergy_and_duplicate_signals(client):
    pid = _patient(client, "pat-p11-dur")
    first = _med_order(client, pid)
    _sign_activate(client, first)
    second = _med_order(client, pid)
    _sign_activate(client, second)
    r = client.post("/fhir/AllergyIntolerance", json={
        "resourceType": "AllergyIntolerance", "id": f"alg-{pid}",
        "clinicalStatus": {"coding": [{"code": "active"}]},
        "code": {"coding": [{"system": RXNORM, "code": LISINOPRIL,
                             "display": "Lisinopril"}]},
        "patient": {"reference": f"Patient/{pid}"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    items = {i["order_id"]: i for i in dur["items"]}
    for oid in (first, second):
        kinds = {s["type"] for s in items[oid]["signals"]}
        assert "allergy" in kinds
        assert "duplicate-therapy" in kinds
    assert dur["interaction_checking"]["status"] == "no-source"


def _observation(client, pid, code, value, unit):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "valueQuantity": {"value": value, "unit": unit}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_renal_flag_rule_fires_on_verify(client):
    # 76-year-old man, creatinine 3.0 mg/dL, 70 kg -> CrCl ~20.6 (< 30).
    pid = _patient(client, "pat-p11-renal", birth="1950-06-01", gender="male")
    _observation(client, pid, "2160-0", 3.0, "mg/dL")
    _observation(client, pid, "29463-7", 70, "kg")
    oid = _med_order(client, pid, "847232",
                     "Metformin hydrochloride 500 MG Oral Tablet")
    _sign_activate(client, oid)
    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    item = [i for i in dur["items"] if i["order_id"] == oid][0]
    renal = [s for s in item["signals"] if s["type"] == "renal"][0]
    assert "CrCl" in renal["detail"]
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    qitem = [i for i in queue["items"] if i["order_id"] == oid][0]
    assert qitem["renal"]["flagged"] is True
    assert 10 < qitem["renal"]["crcl_ml_min"] < 30
    assert qitem["renal"]["threshold_ml_min"] == 30
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    fired = {(f["event"], f["rule"]) for f in r.json()["rule_firings"]}
    assert ("pharmacy.dur.screened", "renal-dose-review") in fired
    tasks = client.get("/fhir/Task", headers=CLINICIAN).json()
    entries = tasks.get("entry", [])
    assert any("Renal dose review" in str(e["resource"].get("description", ""))
               for e in entries)


def test_interaction_checking_inert_without_source(client):
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "no-source"
    assert status["source"] is None
    # Even a pair any real KB would flag returns NO findings: the system
    # has no interaction data and must not pretend otherwise.
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": ["11951", LISINOPRIL]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "no-source"
    assert out["pairs_checked"] == 0
    assert out["findings"] == []
    ops_config = client.get(
        "/studio/config/terminology/effective/pharmacy-ops",
        headers=ADMIN).json()
    payload = ops_config["payload"] if "payload" in ops_config else ops_config
    assert payload["interactionKnowledgeSource"] is None


# -------------------------------------------------- controlled substances

def _cs_count(client, counted, witness="nurse-witness-1", headers=CLINICIAN):
    return client.post("/pharmacy/cs/counts", json={
        "medication_code": OXYCODONE, "location": "main-pharmacy",
        "counted_quantity": counted, "witness": witness}, headers=headers)


def test_cs_count_balanced_discrepancy_resolve(client):
    r = _cs_count(client, 40)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "balanced"
    assert r.json()["expected"] == 40
    r = _cs_count(client, 38)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "discrepancy"
    assert r.json()["variance"] == -2
    count_id = r.json()["count_id"]
    book = client.get("/pharmacy/cs/book", headers=CLINICIAN).json()
    item = [i for i in book["items"] if i["medication_code"] == OXYCODONE
            and i["location"] == "main-pharmacy"][0]
    assert item["balanced"] is False
    assert item["unresolved_discrepancies"] == 1
    assert [d["count_id"] for d in book["open_discrepancies"]] == [count_id]
    r = client.post(f"/pharmacy/cs/counts/{count_id}/resolve",
                    json={"reason": "Two tablets wasted, waste record filed"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["resolution_reason"]
    book = client.get("/pharmacy/cs/book", headers=CLINICIAN).json()
    item = [i for i in book["items"] if i["medication_code"] == OXYCODONE
            and i["location"] == "main-pharmacy"][0]
    assert item["balanced"] is True
    assert book["open_discrepancies"] == []


def test_cs_two_person_rules_enforced(client):
    # The counter (dev-clinician) cannot witness their own count/waste.
    r = _cs_count(client, 40, witness="dev-clinician")
    assert r.status_code == 422
    r = client.post("/pharmacy/cs/waste", json={
        "medication_code": OXYCODONE, "location": "main-pharmacy",
        "lot_number": f"SEED-{OXYCODONE}", "quantity": 1,
        "reason": "Broken tablet", "witness": "dev-clinician"},
        headers=CLINICIAN)
    assert r.status_code == 422
    # Non-controlled medications do not belong in the CS book.
    r = client.post("/pharmacy/cs/counts", json={
        "medication_code": LISINOPRIL, "location": "main-pharmacy",
        "counted_quantity": 10, "witness": "nurse-witness-1"},
        headers=CLINICIAN)
    assert r.status_code == 422


def test_cs_waste_decrements_lot_and_stock(client):
    r = client.post("/pharmacy/cs/waste", json={
        "medication_code": OXYCODONE, "location": "main-pharmacy",
        "lot_number": f"SEED-{OXYCODONE}", "quantity": 2,
        "reason": "Partial dose remainder", "witness": "nurse-witness-1"},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    stock = {i["code"]: i["quantity"] for i in
             client.get("/pharmacy/stock", headers=CLINICIAN).json()["items"]}
    assert stock[OXYCODONE] == 38
    actions = [e["action"] for e in _audit_actions(client)]
    assert "pharmacy.cs.waste" in actions
    r = client.post("/pharmacy/cs/waste", json={
        "medication_code": OXYCODONE, "location": "main-pharmacy",
        "lot_number": f"SEED-{OXYCODONE}", "quantity": 500,
        "reason": "Impossible", "witness": "nurse-witness-1"},
        headers=CLINICIAN)
    assert r.status_code == 409


# ---------------------------------------------------------- compounding

def _compound(client, category="category-1", storage="refrigerated"):
    return client.post("/pharmacy/compounding/batches", json={
        "preparation_name": "Synthetic IV admixture",
        "category": category, "storage": storage,
        "ingredients": [{"medication_code": LISINOPRIL,
                         "name": "Lisinopril bulk (synthetic)",
                         "quantity": "10 mg", "lot_number": "ING-LOT-1"}]},
        headers=CLINICIAN)


def test_compounding_bud_from_config_and_two_person_verify(client):
    r = _compound(client)
    assert r.status_code == 201, r.text
    batch = r.json()
    compounded = dt.datetime.fromisoformat(batch["compounded_at"])
    bud = dt.datetime.fromisoformat(batch["beyond_use_at"])
    # category-1 refrigerated = 24h from the published pharmacy-ops config.
    assert bud - compounded == dt.timedelta(hours=24)
    assert batch["status"] == "in_preparation"
    queue = client.get("/pharmacy/compounding/queue",
                       headers=CLINICIAN).json()
    assert any(b["batch_id"] == batch["batch_id"] for b in queue["items"])
    # The preparer (dev-clinician) cannot verify their own batch.
    r = client.post(f"/pharmacy/compounding/batches/{batch['batch_id']}/verify",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post(f"/pharmacy/compounding/batches/{batch['batch_id']}/verify",
                    json={}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "verified"
    r = client.post(f"/pharmacy/compounding/batches/{batch['batch_id']}/verify",
                    json={}, headers=ADMIN)
    assert r.status_code == 409


def test_compounding_bud_category2_and_validation(client):
    r = _compound(client, category="category-2", storage="room")
    assert r.status_code == 201, r.text
    batch = r.json()
    delta = (dt.datetime.fromisoformat(batch["beyond_use_at"])
             - dt.datetime.fromisoformat(batch["compounded_at"]))
    assert delta == dt.timedelta(hours=720)  # site-example category-2 config
    r = _compound(client, category="category-99")
    assert r.status_code == 422
    r = client.post("/pharmacy/compounding/batches", json={
        "preparation_name": "No lot prep", "category": "category-1",
        "storage": "room",
        "ingredients": [{"name": "Mystery powder", "quantity": "1 g"}]},
        headers=CLINICIAN)
    assert r.status_code == 422  # ingredient lot capture is mandatory


# -------------------------------------------- packs, register, agent seed

def test_phase11_compliance_seeds(client):
    reqs = client.get("/compliance/requirements", headers=CLINICIAN).json()
    # Floor, not an exact count: later phases add register entries
    # (Phase 12 added six EMTALA / discharge-planning requirements).
    assert reqs["total"] >= 15
    citations = {r["citation"] for r in reqs["items"]}
    assert {"21 CFR 1304.04", "21 CFR Part 1317", "USP <797>", "USP <800>",
            "FD&C Act §582 (21 USC 360eee)"} <= citations
    packs = {p["key"]: p for p in client.get(
        "/studio/config?kind=regulatory_pack", headers=ADMIN).json()}
    for key in ("dea-controlled-substances", "usp-compounding",
                "dscsa-traceability"):
        assert packs[key]["status"] == "draft"  # drafts until governed


def test_dur_reviewer_agent_seeded_draft_only(client):
    agents = {a["key"]: a for a in client.get(
        "/studio/config?kind=agent", headers=ADMIN).json()}
    draft = agents["pharmacy-dur-reviewer"]
    assert draft["status"] == "draft"
    # A draft definition cannot run — honest-off, like every agent seed.
    r = client.post("/agents/pharmacy-dur-reviewer/run",
                    json={"prompt": "Review orders"}, headers=CLINICIAN)
    assert r.status_code == 404


def test_formulary_concepts_carry_governance_fields(client):
    effective = client.get("/studio/config/terminology/effective/formulary",
                           headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    concepts = {c["code"]: c for c in payload["concepts"]}
    assert concepts[LISINOPRIL]["formularyStatus"] == "formulary"
    assert concepts[APIXABAN]["formularyStatus"] == "restricted"
    assert concepts[APIXABAN]["restrictionNote"]
    assert concepts[OXYCODONE]["controlledSchedule"] == "II"


def test_viewer_cannot_verify_or_count(client):
    pid = _patient(client, "pat-p11-roles")
    oid = _med_order(client, pid)
    _sign_activate(client, oid)
    r = client.post(f"/pharmacy/verification/{oid}/verify", json={},
                    headers=VIEWER)
    assert r.status_code == 403
    r = _cs_count(client, 40, headers=VIEWER)
    assert r.status_code == 403
