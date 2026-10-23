"""Inventory Depth pass (W1 backend core) — app.inventory extensions:
unit ladder, kits, containers, lot stages + verification.

Covers the INVENTORY_DEPTH_MODEL acceptance list against the
governed default taxonomy: the lab-reagents class policy
(containerTracked, openStabilityDays 30, initialStage quarantine,
verificationRequired) drives the reagent flows, while a non-policy
class (ppe exam gloves) behaves exactly as before this pass.

Synthetic data only.
"""
import copy
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import inventory as inv
from app.configstore import (CAPABILITY_DEFAULTS, ConfigPayloadError,
                             default_inventory_taxonomy_payload,
                             ensure_inventory_defaults,
                             list_config_versions, published_config,
                             validate_payload)
from app.main import create_app
from app.models import ConfigStatus, Device, StudioConfig
from tests.conftest import ADMIN, CLINICIAN, VIEWER

FACILITY = "facility-main"
UNIT = "unit-icu"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/invd.db")
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            ensure_inventory_defaults(session)
        yield c


def _session(client):
    return client.app.state.session_factory()


def _utc_today():
    return datetime.now(timezone.utc).date()


def _future(days):
    return (_utc_today() + timedelta(days=days)).isoformat()


def _past(days):
    return (_utc_today() - timedelta(days=days)).isoformat()


def make_item(client, sku, headers=ADMIN, **over):
    payload = {"sku": sku, "name": f"Item {sku}", "category": "ppe",
               "subcategory": "exam-gloves", "uom": "each"}
    payload.update(over)
    r = client.post("/inventory/items", json=payload, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def receive(client, sku, location=FACILITY, quantity=10, headers=CLINICIAN,
            **over):
    payload = {"sku": sku, "location_code": location, "quantity": quantity}
    payload.update(over)
    r = client.post("/inventory/movements/receive", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def issue(client, sku, location=FACILITY, quantity=1, headers=CLINICIAN,
          **over):
    payload = {"sku": sku, "location_code": location, "quantity": quantity}
    payload.update(over)
    return client.post("/inventory/movements/issue", json=payload,
                       headers=headers)


def on_hand(client, sku, location=None):
    params = {"sku": sku}
    if location:
        params["location_code"] = location
    rows = client.get("/inventory/stock", params=params,
                      headers=ADMIN).json()["items"]
    return sum(r["quantity"] for r in rows)


def stock_rows(client, sku, location=None):
    params = {"sku": sku}
    if location:
        params["location_code"] = location
    return client.get("/inventory/stock", params=params,
                      headers=ADMIN).json()["items"]


def movements(client, **params):
    return client.get("/inventory/movements", params=params,
                      headers=ADMIN).json()["items"]


def containers(client, **params):
    return client.get("/inventory/containers", params=params,
                      headers=ADMIN).json()["items"]


def _republish_taxonomy(client, mutate):
    with _session(client) as session:
        current = published_config(session, "inventory", "taxonomy")
        assert current is not None
        payload = copy.deepcopy(current.payload)
        mutate(payload)
        current.status = ConfigStatus.superseded
        versions = list_config_versions(session, "inventory", "taxonomy")
        session.add(StudioConfig(
            kind="inventory", key="taxonomy",
            version=versions[-1].version + 1,
            status=ConfigStatus.published, payload=payload,
            created_by="test", updated_by="test"))
        session.commit()


def _make_reagent(client, sku="SKU-REAG"):
    return make_item(client, sku=sku, category="lab-reagents",
                     subcategory="chemistry-reagents", uom="test",
                     lot_tracked=True, expiry_tracked=True)


def _release_reagent_lot(client, sku, lot_number, location=FACILITY):
    """Verify (pass) then release a quarantined reagent lot."""
    r = client.post("/inventory/lots/verify", json={
        "sku": sku, "location_code": location, "lot_number": lot_number,
        "method": "qc-parallel", "result": "pass"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/inventory/lots/stage", json={
        "sku": sku, "location_code": location, "lot_number": lot_number,
        "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Governed config: validator + defaults
# ---------------------------------------------------------------------------

def test_default_taxonomy_has_lab_reagents_policy_and_reason_lists():
    payload = default_inventory_taxonomy_payload()
    validate_payload("inventory", payload)
    lab = next(c for c in payload["categories"]
               if c["code"] == "lab-reagents")
    assert lab["classPolicy"] == {
        "containerTracked": True, "openStabilityDays": 30,
        "initialStage": "quarantine", "verificationRequired": True,
        "verificationValidityDays": None}
    assert [r["code"] for r in payload["quarantineReasonCodes"]] == [
        "awaiting-verification", "temperature-excursion",
        "damaged-packaging", "other"]
    assert [r["code"] for r in payload["recallReasonCodes"]] == [
        "manufacturer-recall", "fda-recall", "internal-quality", "other"]
    assert [r["code"] for r in payload["disposalReasonCodes"]] == [
        "expired", "contaminated", "damaged", "hazmat-disposal", "other"]
    assert CAPABILITY_DEFAULTS["inventory.verify"] == (
        "admin", "clinician")


def test_validator_accepts_class_policy_and_rejects_bad():
    good = copy.deepcopy(default_inventory_taxonomy_payload())
    good["categories"][0]["classPolicy"] = {
        "containerTracked": False, "initialStage": "received"}
    good["categories"][0]["subcategories"][0]["classPolicy"] = {
        "verificationRequired": True, "verificationValidityDays": 14,
        "openStabilityDays": 0}
    validate_payload("inventory", good)

    def _bad(mutate):
        payload = copy.deepcopy(default_inventory_taxonomy_payload())
        mutate(payload)
        return payload

    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _bad(
            lambda p: p["categories"][0].update(
                {"classPolicy": {"surprise": True}})))
    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _bad(
            lambda p: p["categories"][0].update(
                {"classPolicy": {"initialStage": "limbo"}})))
    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _bad(
            lambda p: p["categories"][0].update(
                {"classPolicy": {"openStabilityDays": -1}})))
    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _bad(
            lambda p: p["categories"][0]["subcategories"][0].update(
                {"classPolicy": {"verificationValidityDays": 0}})))
    with pytest.raises(ConfigPayloadError):
        validate_payload("inventory", _bad(
            lambda p: p.update({"disposalReasonCodes": [
                {"code": "Not A Slug", "label": "x"}]})))


# ---------------------------------------------------------------------------
# Unit ladder
# ---------------------------------------------------------------------------

LADDER = [{"code": "case", "containsNext": 2},
          {"code": "box", "containsNext": 3},
          {"code": "each", "containsNext": 1}]


def test_ladder_conversion_exactness_and_par_in_level(client):
    item = make_item(client, sku="SKU-LADDER", uom="each",
                     base_uom="each", uom_ladder=LADDER)
    assert item["base_uom"] == "each"
    assert item["uom_ladder"][-1]["code"] == "each"
    # Receive 2 cases = 2 * 2 * 3 = 12 base units.
    out = receive(client, "SKU-LADDER", quantity=2, unit_level="case")
    assert out["quantity"] == 12
    assert out["unit_level"] == "case"
    assert out["detail"] == {"entered_qty": 2, "entered_level": "case"}
    assert on_hand(client, "SKU-LADDER", FACILITY) == 12
    # Issue 1 box = 3 base units; balances stay exact in base.
    r = issue(client, "SKU-LADDER", quantity=1, unit_level="box")
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 3
    assert on_hand(client, "SKU-LADDER", FACILITY) == 9
    mov = movements(client, sku="SKU-LADDER", event="issue")
    assert mov[0]["quantity"] == -3 and mov[0]["unit_level"] == "box"
    # Unknown level is a 422 naming it.
    r = issue(client, "SKU-LADDER", quantity=1, unit_level="pallet")
    assert r.status_code == 422 and "pallet" in r.json()["detail"]
    # PAR expressed in a ladder level: 4 boxes = 12 base units.
    r = client.put("/inventory/par-levels", json={
        "sku": "SKU-LADDER", "location_code": FACILITY, "par_level": 4,
        "unit_level": "box"}, headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["par_level"] == 12 and body["par_qty"] == 4
    assert body["par_unit_level"] == "box"
    pars = client.get("/inventory/par-levels",
                      params={"sku": "SKU-LADDER"}, headers=ADMIN).json()
    assert pars["items"][0]["par_level"] == 12
    assert pars["items"][0]["par_qty"] == 4


def test_ladder_validation_errors(client):
    # Last level must equal base_uom.
    r = client.post("/inventory/items", json={
        "sku": "SKU-BADLAD", "name": "Bad", "category": "ppe",
        "subcategory": "exam-gloves", "uom": "each", "base_uom": "each",
        "uom_ladder": [{"code": "box", "containsNext": 3},
                       {"code": "vial", "containsNext": 1}]},
        headers=ADMIN)
    assert r.status_code == 422 and "base_uom" in r.json()["detail"]
    # Non-last containsNext must be >= 2.
    r = client.post("/inventory/items", json={
        "sku": "SKU-BADLAD2", "name": "Bad2", "category": "ppe",
        "subcategory": "exam-gloves", "uom": "each",
        "uom_ladder": [{"code": "box", "containsNext": 1},
                       {"code": "each", "containsNext": 1}]},
        headers=ADMIN)
    assert r.status_code == 422 and "box" in r.json()["detail"]
    # Duplicate level codes refused.
    r = client.post("/inventory/items", json={
        "sku": "SKU-BADLAD3", "name": "Bad3", "category": "ppe",
        "subcategory": "exam-gloves", "uom": "each",
        "uom_ladder": [{"code": "box", "containsNext": 2},
                       {"code": "box", "containsNext": 1}]},
        headers=ADMIN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Kits: definition validation + track-whole receipt
# ---------------------------------------------------------------------------

def _components(client):
    make_item(client, sku="SKU-COMP-A", lot_tracked=True,
              expiry_tracked=True)
    make_item(client, sku="SKU-COMP-B")
    return "SKU-COMP-A", "SKU-COMP-B"


def _make_kit(client, sku="SKU-KIT", mode="track-whole", comps=None):
    comps = comps if comps is not None else [
        {"sku": "SKU-COMP-A", "qty": 2}, {"sku": "SKU-COMP-B", "qty": 3}]
    return make_item(client, sku=sku, category="surgical-supplies",
                     subcategory="anesthesia-supplies", structure="kit",
                     kit_mode=mode, kit_components=comps,
                     lot_tracked=True)


def test_kit_definition_validation(client):
    _components(client)
    # A kit requires kit_mode and a non-empty component list.
    r = client.post("/inventory/items", json={
        "sku": "SKU-K0", "name": "K0", "category": "surgical-supplies",
        "subcategory": "anesthesia-supplies", "structure": "kit"},
        headers=ADMIN)
    assert r.status_code == 422
    # Unknown component sku named.
    r = client.post("/inventory/items", json={
        "sku": "SKU-K1", "name": "K1", "category": "surgical-supplies",
        "subcategory": "anesthesia-supplies", "structure": "kit",
        "kit_mode": "track-whole",
        "kit_components": [{"sku": "SKU-GHOST", "qty": 1}]}, headers=ADMIN)
    assert r.status_code == 422 and "SKU-GHOST" in r.json()["detail"]
    # A kit cannot contain itself.
    r = client.post("/inventory/items", json={
        "sku": "SKU-K2", "name": "K2", "category": "surgical-supplies",
        "subcategory": "anesthesia-supplies", "structure": "kit",
        "kit_mode": "track-whole",
        "kit_components": [{"sku": "SKU-K2", "qty": 1}]}, headers=ADMIN)
    assert r.status_code == 422 and "SKU-K2" in r.json()["detail"]
    # Nested kits refused.
    _make_kit(client, sku="SKU-KIT-INNER")
    r = client.post("/inventory/items", json={
        "sku": "SKU-K3", "name": "K3", "category": "surgical-supplies",
        "subcategory": "anesthesia-supplies", "structure": "kit",
        "kit_mode": "track-whole",
        "kit_components": [{"sku": "SKU-KIT-INNER", "qty": 1}]},
        headers=ADMIN)
    assert r.status_code == 422 and "SKU-KIT-INNER" in r.json()["detail"]
    # Asset-linked (instrument) components refused, naming the sku.
    with _session(client) as session:
        session.add(Device(device_key="dev-kit-1", name="Pump",
                           device_type="pump", location=FACILITY))
        session.commit()
    make_item(client, sku="SKU-INST", asset_key="dev-kit-1")
    r = client.post("/inventory/items", json={
        "sku": "SKU-K4", "name": "K4", "category": "surgical-supplies",
        "subcategory": "anesthesia-supplies", "structure": "kit",
        "kit_mode": "track-whole",
        "kit_components": [{"sku": "SKU-INST", "qty": 1}]}, headers=ADMIN)
    assert r.status_code == 422 and "SKU-INST" in r.json()["detail"]


def test_track_whole_kit_expiry_is_earliest_component(client):
    _components(client)
    _make_kit(client)
    # Missing a component declaration: 422 naming the missing sku,
    # and no kit lot is created.
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-KIT", "location_code": FACILITY, "quantity": 2,
        "lot_number": "KIT-1",
        "component_lots": [{"sku": "SKU-COMP-A", "lot_number": "LA",
                            "expiry_date": _future(30)}]},
        headers=CLINICIAN)
    assert r.status_code == 422 and "SKU-COMP-B" in r.json()["detail"]
    assert stock_rows(client, "SKU-KIT") == []
    out = receive(client, "SKU-KIT", quantity=2, lot_number="KIT-1",
                  component_lots=[
                      {"sku": "SKU-COMP-A", "lot_number": "LA",
                       "expiry_date": _future(30)},
                      {"sku": "SKU-COMP-B", "lot_number": "LB",
                       "expiry_date": _future(10)}])
    lot = out["lot"]
    assert lot["expiry_date"] == _future(10)  # earliest component
    assert lot["quantity"] == 2
    comp = {c["sku"]: c for c in lot["composition"]}
    assert comp["SKU-COMP-A"]["qty_per_kit"] == 2
    assert comp["SKU-COMP-A"]["lot_number"] == "LA"
    assert lot["recall_blocked"] is False


def test_break_on_receipt_conserves_and_creates_no_kit_lot(client):
    _components(client)
    _make_kit(client, sku="SKU-KIT-BOR", mode="break-on-receipt")
    # SKU-COMP-A is lot+expiry tracked: no declaration -> 422 naming
    # it, and NOTHING is credited (validate-all-then-apply).
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-KIT-BOR", "location_code": FACILITY, "quantity": 3,
        "lot_number": "KB-1"}, headers=CLINICIAN)
    assert r.status_code == 422 and "SKU-COMP-A" in r.json()["detail"]
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 0
    out = receive(client, "SKU-KIT-BOR", quantity=3, lot_number="KB-1",
                  component_lots=[{"sku": "SKU-COMP-A",
                                   "lot_number": "LA",
                                   "expiry_date": _future(40)}])
    assert out["mode"] == "break-on-receipt"
    assert len(out["movements"]) == 2 and len(out["lots"]) == 2
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 6   # 2 per kit x 3
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 9   # 3 per kit x 3
    assert stock_rows(client, "SKU-KIT-BOR") == []         # no kit lot
    mov = movements(client, sku="SKU-COMP-A", event="receive")
    assert mov[0]["detail"]["via_kit"] == "SKU-KIT-BOR"
    assert mov[0]["detail"]["via_mode"] == "break-on-receipt"
    assert mov[0]["detail"]["kit_qty"] == 3


def test_kit_open_conserves_quantities(client):
    _components(client)
    _make_kit(client)
    receive(client, "SKU-KIT", quantity=2, lot_number="KIT-1",
            component_lots=[
                {"sku": "SKU-COMP-A", "lot_number": "LA",
                 "expiry_date": _future(30)},
                {"sku": "SKU-COMP-B", "lot_number": "LB",
                 "expiry_date": _future(10)}])
    r = client.post("/inventory/kits/open", json={
        "sku": "SKU-KIT", "location_code": FACILITY,
        "lot_number": "KIT-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-KIT", FACILITY) == 1
    rows_a = stock_rows(client, "SKU-COMP-A", FACILITY)
    assert len(rows_a) == 1 and rows_a[0]["lot_number"] == "LA"
    assert rows_a[0]["quantity"] == 2 and rows_a[0]["expiry_date"] \
        == _future(30)
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 3
    opened = movements(client, sku="SKU-KIT", event="kit_open")
    assert opened[0]["quantity"] == -1
    comp_mov = movements(client, sku="SKU-COMP-A", event="kit_open")
    assert comp_mov[0]["quantity"] == 2
    assert comp_mov[0]["detail"]["from_kit"] == "SKU-KIT"
    assert comp_mov[0]["detail"]["kit_lot_number"] == "KIT-1"


def test_kit_open_deactivated_component_409(client):
    _components(client)
    _make_kit(client)
    receive(client, "SKU-KIT", quantity=1, lot_number="KIT-1",
            component_lots=[
                {"sku": "SKU-COMP-A", "lot_number": "LA",
                 "expiry_date": _future(30)},
                {"sku": "SKU-COMP-B", "lot_number": "LB",
                 "expiry_date": _future(10)}])
    r = client.put("/inventory/items/SKU-COMP-B",
                   json={"active": False}, headers=ADMIN)
    assert r.status_code == 200, r.text
    r = client.post("/inventory/kits/open", json={
        "sku": "SKU-KIT", "location_code": FACILITY,
        "lot_number": "KIT-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 409 and "SKU-COMP-B" in r.json()["detail"]
    assert on_hand(client, "SKU-KIT", FACILITY) == 1  # nothing written
    assert movements(client, event="kit_open") == []


# ---------------------------------------------------------------------------
# Kits: assemble / disassemble
# ---------------------------------------------------------------------------

def test_assemble_shortfall_names_every_shortfall_zero_writes(client):
    _components(client)
    _make_kit(client, sku="SKU-TRAY", comps=[
        {"sku": "SKU-COMP-A", "qty": 2}, {"sku": "SKU-COMP-B", "qty": 1}])
    receive(client, "SKU-COMP-A", quantity=1, lot_number="LA",
            expiry_date=_future(20))
    before = movements(client)
    r = client.post("/inventory/kits/assemble", json={
        "sku": "SKU-TRAY", "location_code": FACILITY, "quantity": 1},
        headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "SKU-COMP-A" in detail and "needs 2" in detail
    assert "SKU-COMP-B" in detail and "needs 1" in detail
    assert movements(client) == before  # zero writes
    assert stock_rows(client, "SKU-TRAY") == []
    # Covered now: assembly draws FEFO and recomputes kit expiry.
    receive(client, "SKU-COMP-A", quantity=3, lot_number="LA2",
            expiry_date=_future(60))
    receive(client, "SKU-COMP-B", quantity=2)
    r = client.post("/inventory/kits/assemble", json={
        "sku": "SKU-TRAY", "location_code": FACILITY, "quantity": 1,
        "lot_number": "TRAY-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    lot = r.json()["lot"]
    assert lot["quantity"] == 1 and lot["lot_number"] == "TRAY-1"
    assert lot["expiry_date"] == _future(20)  # earliest drawn (LA)
    assert lot["stage"] == "released"
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 2  # 1 + 3 - 2
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 1


def test_disassemble_returns_original_lots(client):
    _components(client)
    _make_kit(client, sku="SKU-TRAY", comps=[
        {"sku": "SKU-COMP-A", "qty": 2}, {"sku": "SKU-COMP-B", "qty": 1}])
    receive(client, "SKU-COMP-A", quantity=2, lot_number="LA",
            expiry_date=_future(20))
    receive(client, "SKU-COMP-B", quantity=1)
    r = client.post("/inventory/kits/assemble", json={
        "sku": "SKU-TRAY", "location_code": FACILITY, "quantity": 1,
        "lot_number": "TRAY-1"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 0
    r = client.post("/inventory/kits/disassemble", json={
        "sku": "SKU-TRAY", "location_code": FACILITY,
        "lot_number": "TRAY-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-TRAY", FACILITY) == 0
    rows_a = stock_rows(client, "SKU-COMP-A", FACILITY)
    assert len(rows_a) == 1 and rows_a[0]["lot_number"] == "LA"
    assert rows_a[0]["quantity"] == 2  # back in its ORIGINAL lot
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 1
    assert movements(client, event="kit_disassemble")


# ---------------------------------------------------------------------------
# Stages + verification (the lab-reagents policy class)
# ---------------------------------------------------------------------------

def test_quarantine_lot_cannot_be_issued_then_verify_release_issue(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=100, lot_number="RL-1",
            expiry_date=_future(120))
    rows = stock_rows(client, "SKU-REAG", FACILITY)
    assert rows[0]["stage"] == "quarantine"  # policy initialStage
    r = issue(client, "SKU-REAG", quantity=10)
    assert r.status_code == 409, r.text
    assert "100 units blocked by stage/recall" in r.json()["detail"]
    # Release without verification: 409 naming the verification.
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-1", "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "verification" in r.json()["detail"]
    # Viewer cannot verify (inventory.verify is admin/clinician).
    r = client.post("/inventory/lots/verify", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-1", "method": "qc", "result": "pass"},
        headers=VIEWER)
    assert r.status_code == 403
    released = _release_reagent_lot(client, "SKU-REAG", "RL-1")
    assert released["stage"] == "released"
    assert released["verification"]["result"] == "pass"
    assert released["verification"]["method"] == "qc-parallel"
    assert released["movement"]["detail"]["verification"]["result"] \
        == "pass"
    r = issue(client, "SKU-REAG", quantity=10)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-REAG", FACILITY) == 90


def test_verification_validity_expiry_blocks_release(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=10, lot_number="RL-V",
            expiry_date=_future(90))
    r = client.post("/inventory/lots/verify", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-V", "method": "qc-parallel", "result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text

    def _set_validity(payload):
        for cat in payload["categories"]:
            if cat["code"] == "lab-reagents":
                cat["classPolicy"]["verificationValidityDays"] = 7
    _republish_taxonomy(client, _set_validity)
    # Age the verification record beyond its validity window.
    with _session(client) as session:
        lot = session.scalars(select(inv.InventoryStockLot).where(
            inv.InventoryStockLot.sku == "SKU-REAG")).first()
        lot.verified_on = _past(10)
        session.commit()
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-V", "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "expired verification" in r.json()["detail"]
    # A fresh verification supersedes: release now works.
    _release_reagent_lot(client, "SKU-REAG", "RL-V")


def test_failed_verification_blocks_until_pass(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=5, lot_number="RL-F",
            expiry_date=_future(90))
    client.post("/inventory/lots/verify", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-F", "method": "qc-parallel", "result": "fail"},
        headers=CLINICIAN)
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-F", "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "failed verification" in r.json()["detail"]
    _release_reagent_lot(client, "SKU-REAG", "RL-F")
    # Unknown lot verify is a 404.
    r = client.post("/inventory/lots/verify", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "NOPE", "method": "qc", "result": "pass"},
        headers=CLINICIAN)
    assert r.status_code == 404


def test_stage_machine_reasons_and_terminal_disposal(client):
    make_item(client, sku="SKU-STAGE")
    receive(client, "SKU-STAGE", quantity=8)
    # Quarantine requires a reason from quarantineReasonCodes.
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "quarantine"}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "quarantineReasonCodes" in r.json()["detail"]
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "quarantine", "reason_code": "vibes"},
        headers=CLINICIAN)
    assert r.status_code == 422
    assert "quarantineReasonCodes" in r.json()["detail"]
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "quarantine",
        "reason_code": "temperature-excursion"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "quarantine"
    # quarantine -> in_use is not in the machine: 422 naming it.
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "in_use"}, headers=CLINICIAN)
    assert r.status_code == 422 and "in_use" in r.json()["detail"]
    # depleted is a derived state, never a settable target.
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "released"}, headers=CLINICIAN)
    assert r.status_code == 200
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "disposed", "reason_code": "expired"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["stage"] == "disposed"
    assert r.json()["quantity"] == 0  # disposal zeroes the lot
    mov = movements(client, sku="SKU-STAGE", event="stage_transition")
    assert mov[0]["quantity"] == -8
    assert mov[0]["detail"]["to_stage"] == "disposed"
    # Disposed is terminal.
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-STAGE", "location_code": FACILITY, "lot_number": "",
        "to_stage": "quarantine", "reason_code": "other"},
        headers=CLINICIAN)
    assert r.status_code == 422


def test_recalled_component_lot_blocks_its_kit(client):
    _components(client)
    _make_kit(client)
    receive(client, "SKU-KIT", quantity=2, lot_number="KIT-1",
            component_lots=[
                {"sku": "SKU-COMP-A", "lot_number": "LA",
                 "expiry_date": _future(30)},
                {"sku": "SKU-COMP-B", "lot_number": "LB",
                 "expiry_date": _future(10)}])
    # The component lot LA exists as stock elsewhere; recall it.
    receive(client, "SKU-COMP-A", location=UNIT, quantity=5,
            lot_number="LA", expiry_date=_future(30))
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-COMP-A", "location_code": UNIT, "lot_number": "LA",
        "to_stage": "recalled", "reason_code": "manufacturer-recall"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    rows = stock_rows(client, "SKU-KIT", FACILITY)
    assert rows[0]["recall_blocked"] is True
    assert rows[0]["blocked_by"] == [
        {"sku": "SKU-COMP-A", "lot_number": "LA"}]
    r = issue(client, "SKU-KIT", quantity=1)
    assert r.status_code == 409, r.text
    assert "blocked by stage/recall" in r.json()["detail"]
    assert on_hand(client, "SKU-KIT", FACILITY) == 2


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def test_container_receive_sum_and_non_container_class_rejected(client):
    _make_reagent(client)
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-REAG", "location_code": FACILITY, "quantity": 20,
        "lot_number": "RL-C", "expiry_date": _future(100),
        "containers": [{"container_code": "C-1", "quantity": 12},
                       {"container_code": "C-2", "quantity": 7}]},
        headers=CLINICIAN)
    assert r.status_code == 422  # 12 + 7 != 20, both numbers named
    assert "19" in r.json()["detail"] and "20" in r.json()["detail"]
    assert containers(client, sku="SKU-REAG") == []
    # A non-container-tracked class rejects a containers body.
    make_item(client, sku="SKU-GLOVE")
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-GLOVE", "location_code": FACILITY, "quantity": 2,
        "containers": [{"quantity": 2}]}, headers=CLINICIAN)
    assert r.status_code == 422
    assert "container" in r.json()["detail"].lower()


def test_container_open_consume_depleted(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=20, lot_number="RL-C",
            expiry_date=_future(100),
            containers=[{"container_code": "C-1", "quantity": 10},
                        {"container_code": "C-2", "quantity": 10}])
    _release_reagent_lot(client, "SKU-REAG", "RL-C")
    rows = {c["container_code"]: c for c in containers(client,
                                                      sku="SKU-REAG")}
    assert rows["C-1"]["stage"] == "released"  # synced with the lot
    assert rows["C-1"]["lot_stage"] == "released"
    # Consume before opening: 409 naming the stage.
    r = client.post("/inventory/containers/consume", json={
        "container_code": "C-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 409 and "released" in r.json()["detail"]
    r = client.post("/inventory/containers/open", json={
        "container_code": "C-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    opened = r.json()
    assert opened["stage"] == "in_use" and opened["opened_by"]
    # in-use expiry = opened + 30d stability, earlier than shelf.
    assert opened["in_use_expiry_date"] == _future(30)
    assert opened["effective_expiry"] == _future(30)
    # Over-consume: 409 naming both numbers.
    r = client.post("/inventory/containers/consume", json={
        "container_code": "C-1", "quantity": 11}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "11" in r.json()["detail"] and "10" in r.json()["detail"]
    r = client.post("/inventory/containers/consume", json={
        "container_code": "C-1", "quantity": 4}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["quantity"] == 6
    assert on_hand(client, "SKU-REAG", FACILITY) == 16  # lot follows
    assert r.json()["movement"]["container_code"] == "C-1"
    r = client.post("/inventory/containers/consume", json={
        "container_code": "C-1", "quantity": 6}, headers=CLINICIAN)
    assert r.json()["stage"] == "depleted" and r.json()["quantity"] == 0
    assert on_hand(client, "SKU-REAG", FACILITY) == 10


def test_container_dispose_cradle_to_grave(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=10, lot_number="RL-D",
            expiry_date=_future(100),
            containers=[{"container_code": "CD-1", "quantity": 10}])
    r = client.post("/inventory/containers/dispose", json={
        "container_code": "CD-1", "reason_code": "vibes"},
        headers=CLINICIAN)
    assert r.status_code == 422
    assert "disposalReasonCodes" in r.json()["detail"]
    r = client.post("/inventory/containers/dispose", json={
        "container_code": "CD-1", "reason_code": "expired"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stage"] == "disposed" and body["quantity"] == 0
    assert body["disposal_reason_code"] == "expired"
    assert body["disposed_by"] and body["disposed_at"]
    assert on_hand(client, "SKU-REAG", FACILITY) == 0
    mov = movements(client, sku="SKU-REAG", event="stage_transition")
    assert mov[0]["quantity"] == -10
    assert mov[0]["container_code"] == "CD-1"
    # Terminal: disposing again is a 409.
    r = client.post("/inventory/containers/dispose", json={
        "container_code": "CD-1", "reason_code": "expired"},
        headers=CLINICIAN)
    assert r.status_code == 409


def test_in_use_expiry_overrides_shelf_in_container_fefo(client):
    _make_reagent(client)
    # Lot SHELF-EARLY expires sooner on the shelf; lot SHELF-LATE
    # later — but SHELF-LATE's opened container expires in use first.
    receive(client, "SKU-REAG", quantity=10, lot_number="SHELF-EARLY",
            expiry_date=_future(100),
            containers=[{"container_code": "C-EARLY", "quantity": 10}])
    receive(client, "SKU-REAG", quantity=10, lot_number="SHELF-LATE",
            expiry_date=_future(200),
            containers=[{"container_code": "C-LATE", "quantity": 10}])
    _release_reagent_lot(client, "SKU-REAG", "SHELF-EARLY")
    _release_reagent_lot(client, "SKU-REAG", "SHELF-LATE")
    for code in ("C-EARLY", "C-LATE"):
        r = client.post("/inventory/containers/open",
                        json={"container_code": code}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    with _session(client) as session:
        rows = {c.container_code: c for c in session.scalars(
            select(inv.InventoryContainer)).all()}
        rows["C-LATE"].in_use_expiry_date = _future(5)
        rows["C-EARLY"].in_use_expiry_date = _future(80)
        session.commit()
    r = issue(client, "SKU-REAG", quantity=5)
    assert r.status_code == 200, r.text
    alloc = r.json()["allocations"][0]
    assert alloc["lot_number"] == "SHELF-LATE"  # in-use expiry wins
    assert alloc["containers"][0]["container_code"] == "C-LATE"
    assert alloc["containers"][0]["qty"] == 5
    rows = {c["container_code"]: c for c in containers(client,
                                                      sku="SKU-REAG")}
    assert rows["C-LATE"]["quantity"] == 5
    assert rows["C-EARLY"]["quantity"] == 10


def test_container_count_auto_split_and_auto_codes(client):
    _make_reagent(client)
    out = receive(client, "SKU-REAG", quantity=10, lot_number="RL-S",
                  expiry_date=_future(100), container_count=3)
    # As even as possible, remainder to the earliest containers.
    assert [c["quantity"] for c in out["containers"]] == [4, 3, 3]
    assert [c["container_code"] for c in out["containers"]] == [
        "RL-S-001", "RL-S-002", "RL-S-003"]
    assert all(c["stage"] == "quarantine" for c in out["containers"])
    rows = containers(client, sku="SKU-REAG", stage="quarantine")
    assert len(rows) == 3
    one = client.get("/inventory/containers/RL-S-002",
                     headers=VIEWER).json()
    assert one["quantity"] == 3 and one["lot_number"] == "RL-S"


def test_recall_cascades_to_containers(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=10, lot_number="RL-R",
            expiry_date=_future(100),
            containers=[{"container_code": "CR-1", "quantity": 10}])
    _release_reagent_lot(client, "SKU-REAG", "RL-R")
    r = client.post("/inventory/lots/stage", json={
        "sku": "SKU-REAG", "location_code": FACILITY,
        "lot_number": "RL-R", "to_stage": "recalled",
        "reason_code": "fda-recall"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    rows = containers(client, sku="SKU-REAG")
    assert rows[0]["stage"] == "recalled"


# ---------------------------------------------------------------------------
# Instruments + the unchanged non-policy class
# ---------------------------------------------------------------------------

def test_instrument_item_rejects_adjust_down(client):
    with _session(client) as session:
        session.add(Device(device_key="dev-depth-1", name="Analyser",
                           device_type="analyser", location=FACILITY))
        session.commit()
    make_item(client, sku="SKU-INST-ITEM", category="instruments",
              subcategory="surgical-instruments", asset_key="dev-depth-1")
    receive(client, "SKU-INST-ITEM", quantity=1)
    r = client.post("/inventory/movements/adjust", json={
        "sku": "SKU-INST-ITEM", "location_code": FACILITY,
        "quantity_delta": -1, "reason_code": "damaged"},
        headers=CLINICIAN)
    assert r.status_code == 409
    assert "instruments are serialized assets" in r.json()["detail"]
    assert on_hand(client, "SKU-INST-ITEM", FACILITY) == 1


def test_non_policy_class_behaves_exactly_as_before(client):
    make_item(client, sku="SKU-GLOVE")
    out = receive(client, "SKU-GLOVE", quantity=10)
    assert out["lot"]["stage"] == "released"  # immediately issuable
    assert containers(client, sku="SKU-GLOVE") == []
    r = issue(client, "SKU-GLOVE", quantity=4)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-GLOVE", FACILITY) == 6
    assert "containers" not in r.json()["allocations"][0]


# ---------------------------------------------------------------------------
# W1b regression: composition snapshot rules kit credits (Fix 1)
# ---------------------------------------------------------------------------

def _lot_row(client, sku, lot_number, location=FACILITY):
    rows = [r for r in stock_rows(client, sku, location)
            if r["lot_number"] == lot_number]
    assert len(rows) == 1, rows
    return rows[0]


def _comp_qty(lot_row, comp_sku):
    return sum(c["qty"] for c in lot_row["composition"]
               if c["sku"] == comp_sku)


def test_definition_edit_after_receipt_does_not_change_open_credits(client):
    _components(client)
    _make_kit(client)
    receive(client, "SKU-KIT", quantity=2, lot_number="KIT-1",
            component_lots=[
                {"sku": "SKU-COMP-A", "lot_number": "LA",
                 "expiry_date": _future(30)},
                {"sku": "SKU-COMP-B", "lot_number": "LB",
                 "expiry_date": _future(10)}])
    # The receipt stamps REMAINING totals: qty = qty_per_kit x kits.
    lot = _lot_row(client, "SKU-KIT", "KIT-1")
    assert _comp_qty(lot, "SKU-COMP-A") == 4
    assert _comp_qty(lot, "SKU-COMP-B") == 6
    # Edit the kit DEFINITION after receipt: 5/7 instead of 2/3.
    r = client.put("/inventory/items/SKU-KIT", json={
        "kit_components": [{"sku": "SKU-COMP-A", "qty": 5},
                           {"sku": "SKU-COMP-B", "qty": 7}]},
        headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["kit_components"] == [
        {"sku": "SKU-COMP-A", "qty": 5}, {"sku": "SKU-COMP-B", "qty": 7}]
    # Open 1: credits equal the ORIGINAL snapshot amounts (2/3),
    # not the edited definition.
    r = client.post("/inventory/kits/open", json={
        "sku": "SKU-KIT", "location_code": FACILITY,
        "lot_number": "KIT-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 2
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 3
    assert on_hand(client, "SKU-KIT", FACILITY) == 1
    # The stored composition is consumed by the credit.
    lot = _lot_row(client, "SKU-KIT", "KIT-1")
    assert _comp_qty(lot, "SKU-COMP-A") == 2
    assert _comp_qty(lot, "SKU-COMP-B") == 3
    # Opening the last unit credits the remainder exactly; totals
    # conserve against the receipt (2 kits x 2 / x 3).
    r = client.post("/inventory/kits/open", json={
        "sku": "SKU-KIT", "location_code": FACILITY,
        "lot_number": "KIT-1", "quantity": 1}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 4
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 6
    lot = _lot_row(client, "SKU-KIT", "KIT-1")
    assert lot["quantity"] == 0 and lot["composition"] == []


def test_partial_disassembly_conserves_and_never_overcredits(client):
    _components(client)
    _make_kit(client, sku="SKU-TRAY", comps=[
        {"sku": "SKU-COMP-A", "qty": 2}, {"sku": "SKU-COMP-B", "qty": 1}])
    # Component A is drawn across TWO lots (3 + 3 for 3 kits x 2).
    receive(client, "SKU-COMP-A", quantity=3, lot_number="LA1",
            expiry_date=_future(20))
    receive(client, "SKU-COMP-A", quantity=3, lot_number="LA2",
            expiry_date=_future(60))
    receive(client, "SKU-COMP-B", quantity=3)
    r = client.post("/inventory/kits/assemble", json={
        "sku": "SKU-TRAY", "location_code": FACILITY, "quantity": 3,
        "lot_number": "TRAY-3"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 0
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 0
    comp = {(c["sku"], c["lot_number"]): c["qty"]
            for c in r.json()["lot"]["composition"]}
    assert comp[("SKU-COMP-A", "LA1")] == 3
    assert comp[("SKU-COMP-A", "LA2")] == 3
    assert comp[("SKU-COMP-B", "")] == 3

    def _disassemble_one():
        r = client.post("/inventory/kits/disassemble", json={
            "sku": "SKU-TRAY", "location_code": FACILITY,
            "lot_number": "TRAY-3", "quantity": 1}, headers=CLINICIAN)
        assert r.status_code == 200, r.text

    # 1st: A credits 6 x 1/3 = 2, walking entries in order (LA1).
    _disassemble_one()
    assert _lot_row(client, "SKU-COMP-A", "LA1")["quantity"] == 2
    assert _lot_row(client, "SKU-COMP-A", "LA2")["quantity"] == 0
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 1
    assert on_hand(client, "SKU-TRAY", FACILITY) == 2
    # 2nd: remaining A = 4 over 2 kits -> 2 (LA1's last 1, then LA2 1).
    _disassemble_one()
    assert _lot_row(client, "SKU-COMP-A", "LA1")["quantity"] == 3
    assert _lot_row(client, "SKU-COMP-A", "LA2")["quantity"] == 1
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 2
    assert on_hand(client, "SKU-TRAY", FACILITY) == 1
    # 3rd: the remainder lands exactly; no lot over-credited.
    _disassemble_one()
    assert _lot_row(client, "SKU-COMP-A", "LA1")["quantity"] == 3
    assert _lot_row(client, "SKU-COMP-A", "LA2")["quantity"] == 3
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 3
    lot = _lot_row(client, "SKU-TRAY", "TRAY-3")
    assert lot["quantity"] == 0 and lot["composition"] == []


def test_partial_opens_of_purchased_kit_conserve(client):
    _components(client)
    _make_kit(client)
    receive(client, "SKU-KIT", quantity=4, lot_number="KIT-4",
            component_lots=[
                {"sku": "SKU-COMP-A", "lot_number": "LA",
                 "expiry_date": _future(30)},
                {"sku": "SKU-COMP-B", "lot_number": "LB",
                 "expiry_date": _future(10)}])

    def _open(qty):
        r = client.post("/inventory/kits/open", json={
            "sku": "SKU-KIT", "location_code": FACILITY,
            "lot_number": "KIT-4", "quantity": qty}, headers=CLINICIAN)
        assert r.status_code == 200, r.text

    _open(1)
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 2
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 3
    assert on_hand(client, "SKU-KIT", FACILITY) == 3
    lot = _lot_row(client, "SKU-KIT", "KIT-4")
    assert _comp_qty(lot, "SKU-COMP-A") == 6
    assert _comp_qty(lot, "SKU-COMP-B") == 9
    _open(2)
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 6
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 9
    assert on_hand(client, "SKU-KIT", FACILITY) == 1
    lot = _lot_row(client, "SKU-KIT", "KIT-4")
    assert _comp_qty(lot, "SKU-COMP-A") == 2
    assert _comp_qty(lot, "SKU-COMP-B") == 3
    _open(1)
    assert on_hand(client, "SKU-COMP-A", FACILITY) == 8   # 4 kits x 2
    assert on_hand(client, "SKU-COMP-B", FACILITY) == 12  # 4 kits x 3
    lot = _lot_row(client, "SKU-KIT", "KIT-4")
    assert lot["quantity"] == 0 and lot["composition"] == []


# ---------------------------------------------------------------------------
# W1b regression: container transfers (Fix 2)
# ---------------------------------------------------------------------------

def _live_container_sum(client, sku, location):
    return sum(c["quantity"] for c in containers(
        client, sku=sku, location_code=location)
        if c["stage"] not in ("disposed", "depleted", "recalled",
                              "expired"))


def test_container_transfer_moves_whole_container(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=20, lot_number="RL-T",
            expiry_date=_future(100),
            containers=[{"container_code": "CT-1", "quantity": 12},
                        {"container_code": "CT-2", "quantity": 8}])
    _release_reagent_lot(client, "SKU-REAG", "RL-T")
    r = client.post("/inventory/containers/transfer", json={
        "container_code": "CT-1", "to_location_code": UNIT},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["container_code"] == "CT-1"
    assert body["location_code"] == UNIT and body["quantity"] == 12
    assert len(body["movements"]) == 2
    # Lot quantities exact at both locations.
    assert _lot_row(client, "SKU-REAG", "RL-T", FACILITY)["quantity"] == 8
    assert _lot_row(client, "SKU-REAG", "RL-T", UNIT)["quantity"] == 12
    # Invariant: lot qty == sum of live containers, both locations.
    assert _live_container_sum(client, "SKU-REAG", FACILITY) == 8
    assert _live_container_sum(client, "SKU-REAG", UNIT) == 12
    # The transfer pair carries the container_code.
    pair = movements(client, sku="SKU-REAG", event="transfer")
    assert len(pair) == 2
    assert all(m["container_code"] == "CT-1" for m in pair)
    out_mov = next(m for m in pair if m["quantity"] == -12)
    in_mov = next(m for m in pair if m["quantity"] == 12)
    assert out_mov["location_code"] == FACILITY
    assert out_mov["to_location_code"] == UNIT
    assert in_mov["location_code"] == UNIT
    assert all(m["detail"]["container_code"] == "CT-1" for m in pair)
    assert all(m["detail"]["container_stage"] == "released"
               for m in pair)
    # Same-location transfer is a 422.
    r = client.post("/inventory/containers/transfer", json={
        "container_code": "CT-2", "to_location_code": FACILITY},
        headers=CLINICIAN)
    assert r.status_code == 422
    # A disposed container refuses 409 naming the stage.
    r = client.post("/inventory/containers/dispose", json={
        "container_code": "CT-2", "reason_code": "expired"},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    r = client.post("/inventory/containers/transfer", json={
        "container_code": "CT-2", "to_location_code": UNIT},
        headers=CLINICIAN)
    assert r.status_code == 409 and "disposed" in r.json()["detail"]


def test_lot_level_transfer_of_container_tracked_item_refuses(client):
    _make_reagent(client)
    receive(client, "SKU-REAG", quantity=10, lot_number="RL-X",
            expiry_date=_future(100),
            containers=[{"container_code": "CX-1", "quantity": 10}])
    _release_reagent_lot(client, "SKU-REAG", "RL-X")
    before_movements = movements(client)
    r = client.post("/inventory/movements/transfer", json={
        "sku": "SKU-REAG", "from_location": FACILITY,
        "to_location": UNIT, "quantity": 4}, headers=CLINICIAN)
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "container-tracked" in detail
    assert "POST /inventory/containers/transfer" in detail
    # Nothing written: balances and movement count unchanged.
    assert on_hand(client, "SKU-REAG", FACILITY) == 10
    assert on_hand(client, "SKU-REAG", UNIT) == 0
    assert movements(client) == before_movements
    rows = containers(client, sku="SKU-REAG")
    assert rows[0]["location_code"] == FACILITY


# ---------------------------------------------------------------------------
# Startup ensure: Depth columns for pre-Depth databases. Regression for
# the 2026-10-21 Air failure: a database created before the Depth pass
# has the four extended tables WITHOUT the depth columns (create_all
# never alters an existing table), so /home/cards and /inventory/*
# 500'd with "no such column" until ensure_inventory_columns ran.
# ---------------------------------------------------------------------------

_PREDEPTH_DDL = (
    "CREATE TABLE inventory_items ("
    "id INTEGER PRIMARY KEY, sku VARCHAR(120), name VARCHAR(300), "
    "category VARCHAR(60), subcategory VARCHAR(60), uom VARCHAR(40), "
    "lot_tracked BOOLEAN, serial_tracked BOOLEAN, "
    "expiry_tracked BOOLEAN, unit_cost_cents INTEGER, "
    "formulary_code VARCHAR(60), asset_key VARCHAR(120), "
    "active BOOLEAN, created_by VARCHAR(120), "
    "created_at DATETIME, updated_at DATETIME)",
    "CREATE TABLE inventory_stock_lots ("
    "id INTEGER PRIMARY KEY, sku VARCHAR(120), "
    "location_code VARCHAR(120), lot_number VARCHAR(120), "
    "serial VARCHAR(120), expiry_date VARCHAR(10), "
    "quantity INTEGER, unit_cost_cents INTEGER, "
    "created_at DATETIME, updated_at DATETIME)",
    "CREATE TABLE inventory_movements ("
    "id INTEGER PRIMARY KEY, entry_id VARCHAR(120), "
    "event VARCHAR(20), sku VARCHAR(120), "
    "location_code VARCHAR(120), to_location_code VARCHAR(120), "
    "lot_number VARCHAR(120), serial VARCHAR(120), "
    "expiry_date VARCHAR(10), quantity INTEGER, "
    "balance_after INTEGER, unit_cost_cents INTEGER, "
    "value_cents INTEGER, reference VARCHAR(200), "
    "reason_code VARCHAR(60), patient_ref VARCHAR(200), "
    "note TEXT, actor VARCHAR(120), detail JSON, "
    "created_at DATETIME)",
    "CREATE TABLE inventory_par_levels ("
    "id INTEGER PRIMARY KEY, sku VARCHAR(120), "
    "location_code VARCHAR(120), par_level INTEGER, "
    "updated_by VARCHAR(120), created_at DATETIME, "
    "updated_at DATETIME)",
)


def _predepth_engine(tmp_path):
    from sqlalchemy import create_engine, text
    engine = create_engine(f"sqlite:///{tmp_path}/predepth.db")
    with engine.begin() as conn:
        for ddl in _PREDEPTH_DDL:
            conn.execute(text(ddl))
    return engine


def test_ensure_inventory_columns_adds_depth_columns(tmp_path):
    from sqlalchemy import inspect
    engine = _predepth_engine(tmp_path)
    inv.ensure_inventory_columns(engine)
    inspector = inspect(engine)
    for table, columns in inv.INVENTORY_DEPTH_ADD_COLUMNS:
        present = {c["name"] for c in inspector.get_columns(table)}
        assert {name for name, _ddl in columns} <= present
    # Second run is a no-op (no duplicate-column error).
    inv.ensure_inventory_columns(engine)
    engine.dispose()


def test_app_boots_and_serves_on_predepth_database(tmp_path):
    from sqlalchemy import text
    engine = _predepth_engine(tmp_path)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO inventory_items (sku, name, category, "
            "subcategory, uom, lot_tracked, serial_tracked, "
            "expiry_tracked, active, created_by) VALUES ("
            "'PRE-1', 'Pre-Depth Widget', 'ppe', 'exam-gloves', "
            "'each', 0, 0, 0, 1, 'test')"))
        conn.execute(text(
            "INSERT INTO inventory_stock_lots (sku, location_code, "
            "lot_number, quantity) VALUES ("
            "'PRE-1', 'facility-main', 'LOT-OLD', 5)"))
    engine.dispose()
    app = create_app(
        database_url=f"sqlite:///{tmp_path}/predepth.db")
    with TestClient(app) as c:
        # The two endpoints that 500'd on the pre-Depth Air database.
        cards = c.get("/home/cards", headers=ADMIN)
        assert cards.status_code == 200
        items = c.get("/inventory/items", headers=ADMIN)
        assert items.status_code == 200
        # Pre-Depth data survives, with model defaults backfilled:
        # the item is a simple item and its lot is allocatable
        # (stage "released"), exactly its pre-Depth behaviour.
        (row,) = [r for r in items.json()["items"]
                  if r["sku"] == "PRE-1"]
        assert row["structure"] == "simple"
        overview = c.get("/inventory/overview", headers=ADMIN)
        assert overview.status_code == 200
