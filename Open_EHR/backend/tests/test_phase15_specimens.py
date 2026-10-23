"""Phase 15 (B) — specimen lifecycle: derivation (aliquots / splits /
subcultures / isolates) with parent->child lineage + own barcodes,
family chain of custody with break detection, and add-on testing.

Synthetic data only."""
from tests.conftest import ADMIN, CLINICIAN, VIEWER

GLUCOSE = "2345-7"
POTASSIUM = "2823-3"
CREATININE = "2160-0"
BLOOD_CULTURE = "87040-1"


def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": pid,
                          "name": [{"family": "Synthetic", "given": ["15"]}],
                          "birthDate": "1990-01-01"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _collected_specimen(client, code=GLUCOSE, patient="pat-1"):
    r = client.post("/lab/orders",
                    json={"code": code,
                          "subject": {"reference": f"Patient/{patient}"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order = r.json()
    r = client.post(f"/lab/orders/{order['id']}/transition",
                    json={"to_status": "collected"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    detail = client.get(f"/lab/orders/{order['id']}",
                        headers=CLINICIAN).json()
    return order["id"], detail["specimen"]["id"]


def _derive(client, specimen_id, expect=201, **body):
    r = client.post(f"/lab/specimens/{specimen_id}/derive",
                    json=body, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


def _scan(client, identifier, action, expect=201, **extra):
    r = client.post("/lab/specimens/scan",
                    json={"identifier": identifier, "action": action,
                          **extra}, headers=CLINICIAN)
    assert r.status_code == expect, r.text
    return r


# ---------------------------------------------------------------------------
# Derivation: lineage, sequence, volumes, barcodes
# ---------------------------------------------------------------------------

def test_derive_aliquots_lineage_sequence_and_barcodes(client):
    _patient(client)
    _order_id, specimen_id = _collected_specimen(client)
    out = _derive(client, specimen_id, type="aliquot", count=2,
                  volume_ml=2.0, container="edta tube").json()
    assert out["total"] == 2
    children = out["children"]
    assert [c["sequence"] for c in children] == [1, 2]
    for child in children:
        assert child["parent_key"] == specimen_id
        assert child["root_key"] == specimen_id
        assert child["derivation_type"] == "aliquot"
        assert child["volume_ml"] == 2.0
        assert child["container"] == "edta tube"
        assert child["barcode"].startswith("OCSP1|")
        # the child is a real FHIR Specimen with a parent link
        fhir_child = client.get(f"/fhir/Specimen/{child['specimen_id']}",
                                headers=CLINICIAN).json()
        assert fhir_child["parent"] == [
            {"reference": f"Specimen/{specimen_id}"}]
    # distinct barcodes, all registered for universal lookup
    assert children[0]["barcode"] != children[1]["barcode"]
    registry = client.get("/lab/specimen-barcodes",
                          headers=CLINICIAN).json()
    values = {item["value"] for item in registry["items"]}
    assert children[0]["barcode"] in values
    assert children[1]["barcode"] in values
    kinds = {item["value"]: item["kind"] for item in registry["items"]}
    assert kinds[children[0]["barcode"]] == "aliquot"
    entity = {item["value"]: item["entity_ref"]
              for item in registry["items"]}
    assert entity[children[0]["barcode"]] == \
        f"Specimen/{children[0]['specimen_id']}"
    # the parent's remaining volume reflects the derivations (10 - 4)
    assert out["parent"]["remaining_volume_ml"] == 6.0
    # a second derivation continues the sequence
    out2 = _derive(client, specimen_id, type="split", count=1,
                   volume_ml=1.0).json()
    assert out2["children"][0]["sequence"] == 3
    assert out2["children"][0]["derivation_type"] == "split"


def test_derive_volume_insufficient_and_bad_type(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    # root default volume is 10 mL: 3 x 4 mL exceeds it
    _derive(client, specimen_id, expect=422, type="aliquot", count=3,
            volume_ml=4.0)
    _derive(client, specimen_id, expect=422, type="bogus", count=1,
            volume_ml=1.0)
    _derive(client, specimen_id, expect=422, type="aliquot", count=1,
            volume_ml=0)
    r = client.post(f"/lab/specimens/{specimen_id}/derive",
                    json={"type": "aliquot", "count": 1, "volume_ml": 1.0},
                    headers=VIEWER)
    assert r.status_code == 403, r.text


def test_subculture_microbiology_linkage(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client, code=BLOOD_CULTURE)
    r = client.post(f"/lab/specimens/{specimen_id}/subculture",
                    json={"volume_ml": 1.0,
                          "organism_code": "escherichia-coli",
                          "container": "agar plate"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    child = r.json()["children"][0]
    assert child["derivation_type"] == "subculture"
    assert child["organism_code"] == "escherichia-coli"
    assert child["container"] == "agar plate"
    # an organism outside the published terminology is refused
    r = client.post(f"/lab/specimens/{specimen_id}/subculture",
                    json={"volume_ml": 1.0, "organism_code": "not-an-org"},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # isolate derivation via the generic route
    out = _derive(client, specimen_id, type="isolate", count=1,
                  volume_ml=0.5, organism_code="escherichia-coli").json()
    assert out["children"][0]["derivation_type"] == "isolate"


def test_derive_from_terminal_specimen_refused(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    _scan(client, specimen_id, "disposal")
    r = client.post(f"/lab/specimens/{specimen_id}/derive",
                    json={"type": "aliquot", "count": 1, "volume_ml": 1.0},
                    headers=CLINICIAN)
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# Custody chain + break detection
# ---------------------------------------------------------------------------

def test_custody_chain_from_any_member_and_by_barcode(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    out = _derive(client, specimen_id, type="aliquot", count=1,
                  volume_ml=2.0).json()
    child = out["children"][0]
    _scan(client, specimen_id, "transport", location="lab-receiving")
    _scan(client, specimen_id, "receipt", location="chemistry bench")
    _scan(client, child["specimen_id"], "bench-transfer",
          location="haematology bench")
    # query by the CHILD's barcode returns the whole family
    chain = client.get(f"/lab/specimens/{child['barcode']}/chain",
                       headers=CLINICIAN).json()
    assert chain["root_key"] == specimen_id
    assert chain["total"] == 2
    by_key = {m["specimen_key"]: m for m in chain["members"]}
    root_actions = [e["action"] for e in by_key[specimen_id]["custody"]]
    assert root_actions == ["collection", "transport", "receipt"]
    child_actions = [e["action"]
                     for e in by_key[child["specimen_id"]]["custody"]]
    assert child_actions == ["bench-transfer"]
    assert chain["tree"]["specimen_key"] == specimen_id
    assert chain["tree"]["children"][0]["specimen_key"] == \
        child["specimen_id"]
    # the child's bench-transfer followed collection-less derivation:
    # its required predecessor is 'receipt' — but the child member has
    # no receipt, so THIS scan is a break (family shows it honestly)
    assert len(chain["breaks"]) == 1
    assert chain["breaks"][0]["specimen_key"] == child["specimen_id"]


def test_custody_happy_path_no_break(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    _scan(client, specimen_id, "transport")
    _scan(client, specimen_id, "receipt")
    _scan(client, specimen_id, "bench-transfer")
    _scan(client, specimen_id, "storage")
    chain = client.get(f"/lab/specimens/{specimen_id}/chain",
                       headers=CLINICIAN).json()
    assert chain["breaks"] == []
    breaks = client.get("/lab/specimens/custody-breaks",
                        headers=CLINICIAN).json()
    assert breaks["total"] == 0


def test_custody_break_flagged_and_on_worklist(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    out = _derive(client, specimen_id, type="aliquot", count=1,
                  volume_ml=2.0).json()
    child_id = out["children"][0]["specimen_id"]
    # receipt without the required predecessor (transport)
    scan = _scan(client, child_id, "receipt").json()
    assert scan["event"]["is_break"] is True
    assert "transport" in scan["event"]["break_reason"]
    breaks = client.get("/lab/specimens/custody-breaks",
                        headers=CLINICIAN).json()
    assert breaks["total"] == 1
    assert breaks["family_count"] == 1
    assert breaks["items"][0]["root_key"] == specimen_id
    assert breaks["items"][0]["specimen_key"] == child_id
    assert breaks["items"][0]["action"] == "receipt"
    # unknown actions are refused against the published vocabulary
    _scan(client, specimen_id, "teleport", expect=422)


# ---------------------------------------------------------------------------
# Add-on testing
# ---------------------------------------------------------------------------

def test_addon_creates_order_consumes_volume(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client, code=POTASSIUM)
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": GLUCOSE}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["addon"]["test_code"] == GLUCOSE
    assert body["addon"]["required_volume_ml"] == 5.0  # default rules
    assert body["specimen"]["remaining_volume_ml"] == 5.0
    # the add-on is a real lab order linked to the existing specimen
    detail = client.get(f"/lab/orders/{body['order']['id']}",
                        headers=CLINICIAN).json()
    assert detail["order"]["status"] == "collected"
    assert detail["specimen"]["id"] == specimen_id
    codes = detail["order"]["code"]["coding"][0]["code"]
    assert codes == GLUCOSE
    # a second 5 mL add-on exhausts the specimen; a third is refused
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": CREATININE}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": GLUCOSE}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "Insufficient volume" in r.json()["detail"]


def test_addon_validation(client):
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": "NOPE-1"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    # panels are not add-on tests
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": "51990-0"}, headers=CLINICIAN)
    assert r.status_code == 422, r.text
    r = client.post("/lab/specimens/nope/add-tests",
                    json={"test_code": GLUCOSE}, headers=CLINICIAN)
    assert r.status_code == 404, r.text


def test_addon_uses_catalog_aliquot_rules(client):
    """A test composed (via the lab manager draft + Studio publish)
    with aliquotRules volume 2.5 mL consumes exactly that on add-on."""
    _patient(client)
    _oid, specimen_id = _collected_specimen(client)
    # compose + publish a test with explicit aliquot rules
    r = client.post("/lab/manager/tests/draft",
                    json={"code": "ADDON-T", "display": "Add-on test T",
                          "unit": "mg/dL",
                          "aliquot_rules": {"count": 2, "volumeMl": 1.25,
                                            "container": "edta tube"}},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["draft"]["version"]
    r = client.post(
        f"/studio/config/terminology/lab-catalog/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post(f"/lab/specimens/{specimen_id}/add-tests",
                    json={"test_code": "ADDON-T"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # required = count x volume = 2 x 1.25 = 2.5 mL
    assert r.json()["addon"]["required_volume_ml"] == 2.5
    assert r.json()["specimen"]["remaining_volume_ml"] == 7.5
