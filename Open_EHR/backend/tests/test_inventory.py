"""Inventory & Materials (WS-A backend core) — app.inventory.

Covers the item master (published-taxonomy validation, duplicate /
cost / formulary / asset links), lot-level stock with FEFO issue,
transfers, governed adjustments, PAR + low-stock / expiry worklists
(the expiry window read from the PUBLISHED taxonomy at request
time), requisitions, cycle counts, preference-card case draw with
implant traceability, and barcode / RFID identification.

The inventory router is wired into app.main (coordinator
integration landed: include_router + ensure_inventory_defaults at
startup); _mount below is now a no-op safeguard. The taxonomy is
seeded at startup — except in the one test that proves behaviour
when it was never published, whose fixture deletes the seeded
taxonomy rows to restore that state. Locations come from the
seeded published hierarchy: facility-main (facility), dept-medicine
(department), unit-icu / unit-medsurg (units) are stock locations;
region-main (a region) is NOT.

Synthetic data only.
"""
import copy
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import inventory as inv
from app.barcodes import BarcodeRecord
from app.configstore import (default_inventory_taxonomy_payload,
                             ensure_inventory_defaults,
                             list_config_versions, published_config)
from app.main import create_app
from app.models import ConfigStatus, Device, StudioConfig
from app.rfid import RfidTag
from app.surgery_depth import PreferenceCard
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

FACILITY = "facility-main"
UNIT = "unit-icu"
UNIT2 = "unit-medsurg"
DEPT = "dept-medicine"


def _mount(app):
    if not any(getattr(r, "path", "").startswith("/inventory")
               for r in app.routes):
        app.include_router(inv.router)
    return app


@pytest.fixture()
def client(tmp_path):
    app = _mount(create_app(database_url=f"sqlite:///{tmp_path}/inv.db"))
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            ensure_inventory_defaults(session)
        yield c


@pytest.fixture()
def unseeded_client(tmp_path):
    """An app whose inventory taxonomy is not published. Startup
    seeds it (WS-D wiring), so the fixture deletes the seeded
    taxonomy rows — the endpoints read the published config at
    request time, so this restores the genuine unpublished state."""
    app = _mount(create_app(database_url=f"sqlite:///{tmp_path}/inv2.db"))
    with TestClient(app) as c:
        with app.state.session_factory() as session:
            for row in session.scalars(select(StudioConfig).where(
                    StudioConfig.kind == "inventory")).all():
                session.delete(row)
            session.commit()
        yield c


def _session(client):
    return client.app.state.session_factory()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_item(client, sku="SKU-GLOVE", headers=ADMIN, **over):
    payload = {"sku": sku, "name": f"Item {sku}", "category": "ppe",
               "subcategory": "exam-gloves", "uom": "box"}
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


def on_hand(client, sku, location=None):
    params = {"sku": sku}
    if location:
        params["location_code"] = location
    rows = client.get("/inventory/stock", params=params,
                      headers=ADMIN).json()["items"]
    return sum(r["quantity"] for r in rows)


def movements(client, **params):
    return client.get("/inventory/movements", params=params,
                      headers=ADMIN).json()["items"]


def _utc_today():
    # The server computes day counts (expiry worklist, FEFO) from the
    # UTC date (app/inventory.py _today, the codebase-wide
    # convention). Fixture dates must use the SAME "today": with a
    # local date, these tests fail deterministically whenever the
    # local and UTC dates differ (e.g. 17:00-24:00 US Pacific) —
    # observed in the Wave A parent gate, batch 2, 2026-10-20 23:11
    # PDT. Whether the product should use a facility-local date is a
    # separate governed-config question, recorded in the Wave A SQA.
    return datetime.now(timezone.utc).date()


def _future(days):
    return (_utc_today() + timedelta(days=days)).isoformat()


def _past(days):
    return (_utc_today() - timedelta(days=days)).isoformat()


def _republish_taxonomy(client, mutate):
    """Publish a new taxonomy version (StudioConfig rows, the
    governed pattern): supersede the published row, insert the
    mutated payload as the next published version."""
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


# ---------------------------------------------------------------------------
# Taxonomy + item master
# ---------------------------------------------------------------------------

def test_taxonomy_endpoint_returns_published_payload(client):
    r = client.get("/inventory/taxonomy", headers=VIEWER)
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload == default_inventory_taxonomy_payload()
    assert payload["expiryWarningDays"] == 90


def test_taxonomy_404_when_unpublished(unseeded_client):
    r = unseeded_client.get("/inventory/taxonomy", headers=ADMIN)
    assert r.status_code == 404
    # And item creation cannot validate a pair -> 422 naming it.
    r = unseeded_client.post("/inventory/items", json={
        "sku": "SKU-X", "name": "X", "category": "ppe",
        "subcategory": "exam-gloves"}, headers=ADMIN)
    assert r.status_code == 422


def test_item_create_happy(client):
    item = make_item(client, unit_cost_cents=350)
    assert item["sku"] == "SKU-GLOVE"
    assert item["category"] == "ppe"
    assert item["unit_cost_cents"] == 350
    assert item["active"] is True
    fetched = client.get("/inventory/items/SKU-GLOVE", headers=VIEWER)
    assert fetched.status_code == 200
    assert fetched.json()["name"] == "Item SKU-GLOVE"


def test_item_create_unpublished_category_422(client):
    r = client.post("/inventory/items", json={
        "sku": "SKU-BAD", "name": "Bad", "category": "moon-rocks",
        "subcategory": "exam-gloves"}, headers=ADMIN)
    assert r.status_code == 422
    assert "moon-rocks" in r.json()["detail"]


def test_item_create_wrong_subcategory_422(client):
    r = client.post("/inventory/items", json={
        "sku": "SKU-BAD2", "name": "Bad", "category": "ppe",
        "subcategory": "orthopedic-implants"}, headers=ADMIN)
    assert r.status_code == 422
    assert "orthopedic-implants" in r.json()["detail"]


def test_item_create_duplicate_sku_409(client):
    make_item(client)
    r = client.post("/inventory/items", json={
        "sku": "SKU-GLOVE", "name": "Again", "category": "ppe",
        "subcategory": "exam-gloves"}, headers=ADMIN)
    assert r.status_code == 409


def test_item_create_negative_cost_422(client):
    r = client.post("/inventory/items", json={
        "sku": "SKU-NEG", "name": "Neg", "category": "ppe",
        "subcategory": "exam-gloves", "unit_cost_cents": -1},
        headers=ADMIN)
    assert r.status_code == 422
    assert "unit_cost_cents" in r.json()["detail"]


def test_item_formulary_link_validated(client):
    # 1049221 is a concept in the seeded published formulary.
    item = make_item(client, sku="SKU-MED", formulary_code="1049221")
    assert item["formulary_code"] == "1049221"
    r = client.post("/inventory/items", json={
        "sku": "SKU-MED2", "name": "Med2", "category": "consumables",
        "subcategory": "syringes-needles", "formulary_code": "9999999"},
        headers=ADMIN)
    assert r.status_code == 422
    assert "9999999" in r.json()["detail"]


def test_item_asset_link_validated(client):
    with _session(client) as session:
        session.add(Device(device_key="dev-pump-1", name="Infusion pump",
                           device_type="pump", location=FACILITY))
        session.commit()
    item = make_item(client, sku="SKU-PUMP", asset_key="dev-pump-1")
    assert item["asset_key"] == "dev-pump-1"
    r = client.post("/inventory/items", json={
        "sku": "SKU-PUMP2", "name": "Pump2", "category": "consumables",
        "subcategory": "tubing-lines", "asset_key": "dev-nope"},
        headers=ADMIN)
    assert r.status_code == 422
    assert "dev-nope" in r.json()["detail"]


def test_item_update_and_sku_immutable(client):
    make_item(client)
    r = client.put("/inventory/items/SKU-GLOVE", json={
        "name": "Renamed gloves", "unit_cost_cents": 425}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Renamed gloves"
    assert r.json()["unit_cost_cents"] == 425
    r = client.put("/inventory/items/SKU-GLOVE", json={"sku": "SKU-OTHER"},
                   headers=ADMIN)
    assert r.status_code == 422
    r = client.put("/inventory/items/SKU-GLOVE", json={
        "category": "ppe", "subcategory": "orthopedic-implants"},
        headers=ADMIN)
    assert r.status_code == 422


def test_item_list_filters(client):
    make_item(client, sku="SKU-A")
    make_item(client, sku="SKU-B", category="linen",
              subcategory="bed-linen", name="Sheets")
    items = client.get("/inventory/items", headers=VIEWER).json()
    assert items["total"] == 2
    linen = client.get("/inventory/items", params={"category": "linen"},
                       headers=VIEWER).json()
    assert [i["sku"] for i in linen["items"]] == ["SKU-B"]
    found = client.get("/inventory/items", params={"q": "sheet"},
                       headers=VIEWER).json()
    assert [i["sku"] for i in found["items"]] == ["SKU-B"]


# ---------------------------------------------------------------------------
# Locations + receiving rules
# ---------------------------------------------------------------------------

def test_location_validation(client):
    make_item(client)
    # A region is the wrong level for stock.
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-GLOVE", "location_code": "region-main", "quantity": 1},
        headers=CLINICIAN)
    assert r.status_code == 422
    assert "region-main" in r.json()["detail"]
    # An unknown code is a 422 naming it.
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-GLOVE", "location_code": "moon-base", "quantity": 1},
        headers=CLINICIAN)
    assert r.status_code == 422
    assert "moon-base" in r.json()["detail"]
    # Facility, department and unit codes are all valid stock locations.
    for code in (FACILITY, DEPT, UNIT):
        receive(client, "SKU-GLOVE", location=code, quantity=1)


def test_receive_flag_rules(client):
    make_item(client, sku="SKU-LOT", lot_tracked=True, expiry_tracked=True)
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-LOT", "location_code": FACILITY, "quantity": 5,
        "expiry_date": _future(100)}, headers=CLINICIAN)
    assert r.status_code == 422  # lot-tracked without lot_number
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-LOT", "location_code": FACILITY, "quantity": 5,
        "lot_number": "L1"}, headers=CLINICIAN)
    assert r.status_code == 422  # expiry-tracked without expiry_date
    ok = receive(client, "SKU-LOT", quantity=5, lot_number="L1",
                 expiry_date=_future(100))
    assert ok["lot"]["quantity"] == 5

    make_item(client, sku="SKU-SER", serial_tracked=True)
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-SER", "location_code": FACILITY, "quantity": 1},
        headers=CLINICIAN)
    assert r.status_code == 422  # serial-tracked without serial
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-SER", "location_code": FACILITY, "quantity": 2,
        "serial": "SN-1"}, headers=CLINICIAN)
    assert r.status_code == 422  # serial-tracked quantity must be 1
    receive(client, "SKU-SER", quantity=1, serial="SN-1")
    assert on_hand(client, "SKU-SER") == 1


def test_receive_lot_cost_override_on_stock_read(client):
    make_item(client, sku="SKU-COST", unit_cost_cents=100)
    receive(client, "SKU-COST", quantity=4, lot_number="L-A")
    receive(client, "SKU-COST", quantity=3, lot_number="L-B",
            unit_cost_cents=250)
    rows = {r["lot_number"]: r for r in client.get(
        "/inventory/stock", params={"sku": "SKU-COST"},
        headers=ADMIN).json()["items"]}
    assert rows["L-A"]["unit_cost_cents"] == 100  # item default
    assert rows["L-A"]["value_cents"] == 400
    assert rows["L-B"]["unit_cost_cents"] == 250  # lot override
    assert rows["L-B"]["lot_unit_cost_cents"] == 250
    assert rows["L-B"]["value_cents"] == 750
    r = client.post("/inventory/movements/receive", json={
        "sku": "SKU-COST", "location_code": FACILITY, "quantity": 1,
        "unit_cost_cents": -5}, headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Issue: FEFO, expiry, oversell
# ---------------------------------------------------------------------------

def _fefo_item(client):
    make_item(client, sku="SKU-FEFO", lot_tracked=True, expiry_tracked=True,
              unit_cost_cents=10)
    receive(client, "SKU-FEFO", quantity=5, lot_number="LATE",
            expiry_date=_future(200))
    receive(client, "SKU-FEFO", quantity=5, lot_number="EARLY",
            expiry_date=_future(50))


def test_issue_fefo_order(client):
    _fefo_item(client)
    r = client.post("/inventory/movements/issue", json={
        "sku": "SKU-FEFO", "location_code": FACILITY, "quantity": 7},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    allocations = r.json()["allocations"]
    assert [(a["lot_number"], a["qty"]) for a in allocations] == [
        ("EARLY", 5), ("LATE", 2)]
    assert len(r.json()["movements"]) == 2  # one movement per lot drawn
    rows = {row["lot_number"]: row["quantity"] for row in client.get(
        "/inventory/stock", params={"sku": "SKU-FEFO"},
        headers=ADMIN).json()["items"]}
    assert rows == {"EARLY": 0, "LATE": 3}


def test_issue_skips_expired_lots(client):
    make_item(client, sku="SKU-EXP", lot_tracked=True, expiry_tracked=True)
    receive(client, "SKU-EXP", quantity=5, lot_number="OLD",
            expiry_date=_past(1))
    receive(client, "SKU-EXP", quantity=3, lot_number="FRESH",
            expiry_date=_future(30))
    r = client.post("/inventory/movements/issue", json={
        "sku": "SKU-EXP", "location_code": FACILITY, "quantity": 3},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert [a["lot_number"] for a in r.json()["allocations"]] == ["FRESH"]
    # Only expired stock remains: any further issue is an oversell.
    r = client.post("/inventory/movements/issue", json={
        "sku": "SKU-EXP", "location_code": FACILITY, "quantity": 1},
        headers=CLINICIAN)
    assert r.status_code == 409


def test_issue_oversell_409_stock_unchanged(client):
    _fefo_item(client)
    before = movements(client, sku="SKU-FEFO")
    r = client.post("/inventory/movements/issue", json={
        "sku": "SKU-FEFO", "location_code": FACILITY, "quantity": 11},
        headers=CLINICIAN)
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "SKU-FEFO" in detail and "11" in detail and "10" in detail
    assert on_hand(client, "SKU-FEFO", FACILITY) == 10
    assert movements(client, sku="SKU-FEFO") == before  # nothing written


# ---------------------------------------------------------------------------
# Transfer + adjust
# ---------------------------------------------------------------------------

def test_transfer_balances_both_sides(client):
    _fefo_item(client)
    r = client.post("/inventory/movements/transfer", json={
        "sku": "SKU-FEFO", "from_location": FACILITY, "to_location": UNIT,
        "quantity": 4}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert on_hand(client, "SKU-FEFO", FACILITY) == 6
    assert on_hand(client, "SKU-FEFO", UNIT) == 4
    dest = client.get("/inventory/stock", params={
        "sku": "SKU-FEFO", "location_code": UNIT},
        headers=ADMIN).json()["items"]
    assert dest[0]["lot_number"] == "EARLY"  # same lot identity, FEFO
    pair = movements(client, sku="SKU-FEFO", event="transfer")
    assert sorted(m["quantity"] for m in pair) == [-4, 4]
    assert {m["location_code"] for m in pair} == {FACILITY, UNIT}
    # Oversell at the source is refused whole.
    r = client.post("/inventory/movements/transfer", json={
        "sku": "SKU-FEFO", "from_location": FACILITY, "to_location": UNIT,
        "quantity": 99}, headers=CLINICIAN)
    assert r.status_code == 409
    assert on_hand(client, "SKU-FEFO", FACILITY) == 6


def test_adjust_reason_validation(client):
    make_item(client, sku="SKU-ADJ")
    receive(client, "SKU-ADJ", quantity=10)
    r = client.post("/inventory/movements/adjust", json={
        "sku": "SKU-ADJ", "location_code": FACILITY, "quantity_delta": -1,
        "reason_code": "vibes"}, headers=CLINICIAN)
    assert r.status_code == 422  # not a published reason code
    assert "vibes" in r.json()["detail"]
    r = client.post("/inventory/movements/adjust", json={
        "sku": "SKU-ADJ", "location_code": FACILITY, "quantity_delta": -1,
        "reason_code": "other"}, headers=CLINICIAN)
    assert r.status_code == 422  # "other" requires a note
    ok = client.post("/inventory/movements/adjust", json={
        "sku": "SKU-ADJ", "location_code": FACILITY, "quantity_delta": -2,
        "reason_code": "other", "note": "Found torn packaging"},
        headers=CLINICIAN)
    assert ok.status_code == 200, ok.text
    assert on_hand(client, "SKU-ADJ", FACILITY) == 8
    recorded = movements(client, sku="SKU-ADJ", event="adjust")
    assert recorded[0]["reason_code"] == "other"
    assert recorded[0]["quantity"] == -2
    r = client.post("/inventory/movements/adjust", json={
        "sku": "SKU-ADJ", "location_code": FACILITY, "quantity_delta": -99,
        "reason_code": "lost"}, headers=CLINICIAN)
    assert r.status_code == 409  # below zero
    assert on_hand(client, "SKU-ADJ", FACILITY) == 8


# ---------------------------------------------------------------------------
# PAR levels + worklists
# ---------------------------------------------------------------------------

def test_par_and_low_stock_worklist(client):
    make_item(client, sku="SKU-PAR")
    make_item(client, sku="SKU-NOPAR")
    receive(client, "SKU-PAR", quantity=4)
    receive(client, "SKU-NOPAR", quantity=0 + 1)
    r = client.put("/inventory/par-levels", json={
        "sku": "SKU-PAR", "location_code": FACILITY, "par_level": 10},
        headers=ADMIN)
    assert r.status_code == 200, r.text
    rows = client.get("/inventory/worklists/low-stock",
                      headers=VIEWER).json()
    assert isinstance(rows, list)  # bare list per contract
    assert rows == [{"sku": "SKU-PAR", "name": "Item SKU-PAR",
                     "location_code": FACILITY, "on_hand": 4,
                     "par_level": 10, "deficit": 6}]
    # SKU-NOPAR has no PAR row: it never appears, however low it is.
    receive(client, "SKU-PAR", quantity=6)
    assert client.get("/inventory/worklists/low-stock",
                      headers=VIEWER).json() == []
    # Location filter + upsert semantics.
    client.put("/inventory/par-levels", json={
        "sku": "SKU-PAR", "location_code": UNIT, "par_level": 3},
        headers=ADMIN)
    rows = client.get("/inventory/worklists/low-stock",
                      params={"location_code": UNIT}, headers=VIEWER).json()
    assert len(rows) == 1 and rows[0]["deficit"] == 3
    pars = client.get("/inventory/par-levels", headers=VIEWER).json()
    assert pars["total"] == 2
    # Unknown item / bad location are refused.
    assert client.put("/inventory/par-levels", json={
        "sku": "SKU-GHOST", "location_code": FACILITY, "par_level": 1},
        headers=ADMIN).status_code == 404
    assert client.put("/inventory/par-levels", json={
        "sku": "SKU-PAR", "location_code": "region-main", "par_level": 1},
        headers=ADMIN).status_code == 422


def test_expiry_worklist_and_governed_window(client):
    make_item(client, sku="SKU-EXPWL", lot_tracked=True,
              expiry_tracked=True)
    receive(client, "SKU-EXPWL", quantity=2, lot_number="SOON",
            expiry_date=_future(60))
    receive(client, "SKU-EXPWL", quantity=2, lot_number="GONE",
            expiry_date=_past(3))
    receive(client, "SKU-EXPWL", quantity=2, lot_number="FAR",
            expiry_date=_future(300))
    rows = client.get("/inventory/worklists/expiry", headers=VIEWER).json()
    assert isinstance(rows, list)
    # Default window 90: GONE (expired) first, then SOON; FAR is out.
    assert [r["lot_number"] for r in rows] == ["GONE", "SOON"]
    assert rows[0]["expired"] is True and rows[0]["days_to_expiry"] == -3
    assert rows[1]["expired"] is False and rows[1]["days_to_expiry"] == 60
    # Republish with a 30-day window: SOON falls out — the window is
    # governed configuration read at request time, not a constant.
    _republish_taxonomy(client, lambda p: p.update(
        {"expiryWarningDays": 30}))
    rows = client.get("/inventory/worklists/expiry", headers=VIEWER).json()
    assert [r["lot_number"] for r in rows] == ["GONE"]


# ---------------------------------------------------------------------------
# Requisitions
# ---------------------------------------------------------------------------

def _requisition(client, qty=10, sku="SKU-REQ"):
    r = client.post("/inventory/requisitions", json={
        "from_location_code": FACILITY, "to_location_code": UNIT,
        "lines": [{"sku": sku, "qty_requested": qty}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def test_requisition_partial_then_complete(client):
    make_item(client, sku="SKU-REQ")
    receive(client, "SKU-REQ", quantity=6)
    req = _requisition(client)
    assert req["status"] == "requested"
    r = client.post(f"/inventory/requisitions/{req['req_id']}/issue",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "partially_issued"
    assert r.json()["lines"][0]["qty_issued"] == 6
    assert on_hand(client, "SKU-REQ", FACILITY) == 0
    req_movements = movements(client, reference=req["req_id"],
                              event="issue")
    assert sum(-m["quantity"] for m in req_movements) == 6
    receive(client, "SKU-REQ", quantity=5)
    r = client.post(f"/inventory/requisitions/{req['req_id']}/issue",
                    json={}, headers=CLINICIAN)
    assert r.json()["status"] == "issued"
    assert r.json()["lines"][0]["qty_issued"] == 10
    # Fully issued: further issue and cancel are both refused.
    assert client.post(f"/inventory/requisitions/{req['req_id']}/issue",
                       json={}, headers=CLINICIAN).status_code == 409
    assert client.post(f"/inventory/requisitions/{req['req_id']}/cancel",
                       headers=CLINICIAN).status_code == 409


def test_requisition_over_issue_refusals_and_cancel(client):
    make_item(client, sku="SKU-REQ2")
    receive(client, "SKU-REQ2", quantity=3)
    req = _requisition(client, qty=5, sku="SKU-REQ2")
    # Explicit quantity beyond the remaining request -> 422.
    r = client.post(f"/inventory/requisitions/{req['req_id']}/issue",
                    json={"lines": [{"sku": "SKU-REQ2", "quantity": 6}]},
                    headers=CLINICIAN)
    assert r.status_code == 422
    # Explicit quantity within the request but beyond stock -> 409.
    r = client.post(f"/inventory/requisitions/{req['req_id']}/issue",
                    json={"lines": [{"sku": "SKU-REQ2", "quantity": 4}]},
                    headers=CLINICIAN)
    assert r.status_code == 409
    assert on_hand(client, "SKU-REQ2", FACILITY) == 3  # nothing written
    # Nothing issued yet: cancel works.
    r = client.post(f"/inventory/requisitions/{req['req_id']}/cancel",
                    headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # Unknown sku lines are refused at creation.
    r = client.post("/inventory/requisitions", json={
        "from_location_code": FACILITY, "to_location_code": UNIT,
        "lines": [{"sku": "SKU-GHOST", "qty_requested": 1}]},
        headers=CLINICIAN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Cycle counts
# ---------------------------------------------------------------------------

def test_cycle_count_variance_applies_count_adjust(client):
    make_item(client, sku="SKU-CNT", lot_tracked=True)
    receive(client, "SKU-CNT", quantity=10, lot_number="L1")
    receive(client, "SKU-CNT", quantity=5, lot_number="L2")
    r = client.post("/inventory/counts", json={"location_code": FACILITY},
                    headers=ADMIN)
    assert r.status_code == 201, r.text
    count = r.json()
    assert {(l["lot_number"], l["expected"]) for l in count["lines"]} == {
        ("L1", 10), ("L2", 5)}
    counted = [{"sku": "SKU-CNT", "lot_number": "L1", "serial": None,
                "counted": 10},
               {"sku": "SKU-CNT", "lot_number": "L2", "serial": None,
                "counted": 3}]
    # Variance without a reason is refused.
    r = client.post(f"/inventory/counts/{count['count_id']}/complete",
                    json={"lines": counted}, headers=ADMIN)
    assert r.status_code == 422
    # A missing snapshot line is refused.
    r = client.post(f"/inventory/counts/{count['count_id']}/complete",
                    json={"lines": counted[:1],
                          "reason_code": "recount-correction"},
                    headers=ADMIN)
    assert r.status_code == 422
    r = client.post(f"/inventory/counts/{count['count_id']}/complete",
                    json={"lines": counted,
                          "reason_code": "recount-correction"},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["status"] == "completed"
    variances = {l["lot_number"]: l["variance"] for l in done["lines"]}
    assert variances == {"L1": 0, "L2": -2}
    adjustments = movements(client, event="count_adjust")
    assert len(adjustments) == 1
    assert adjustments[0]["quantity"] == -2
    assert adjustments[0]["reason_code"] == "recount-correction"
    assert on_hand(client, "SKU-CNT", FACILITY) == 13


def test_cycle_count_balanced_writes_no_movements(client):
    make_item(client, sku="SKU-BAL")
    receive(client, "SKU-BAL", location=UNIT, quantity=7)
    count = client.post("/inventory/counts",
                        json={"location_code": UNIT}, headers=ADMIN).json()
    lines = [{"sku": l["sku"], "lot_number": l["lot_number"],
              "serial": l["serial"], "counted": l["expected"]}
             for l in count["lines"]]
    r = client.post(f"/inventory/counts/{count['count_id']}/complete",
                    json={"lines": lines}, headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "completed"
    assert movements(client, event="count_adjust") == []


# ---------------------------------------------------------------------------
# Case draw + implant trace
# ---------------------------------------------------------------------------

CARD_ITEMS = [
    {"kind": "implant", "code": "SKU-IMP", "qty": 2, "display": "Hip stem"},
    {"kind": "supply", "code": "SKU-SUP", "qty": 3, "display": "Drapes"},
]


def _seed_card(client, card_id="card-1", items=None, surgeon="dr-smith",
               procedure_code="PROC-1"):
    with _session(client) as session:
        session.add(PreferenceCard(
            card_id=card_id, surgeon=surgeon,
            procedure_code=procedure_code,
            items=items if items is not None else copy.deepcopy(CARD_ITEMS),
            created_by="test"))
        session.commit()


def _stock_case(client):
    make_item(client, sku="SKU-IMP", category="implants",
              subcategory="orthopedic-implants", lot_tracked=True,
              expiry_tracked=True, unit_cost_cents=5000)
    make_item(client, sku="SKU-SUP", category="surgical-supplies",
              subcategory="sterile-drapes-gowns", unit_cost_cents=200)
    receive(client, "SKU-IMP", location=UNIT, quantity=5, lot_number="IMP-1",
            expiry_date=_future(120))
    receive(client, "SKU-SUP", location=UNIT, quantity=10)


def test_case_issue_happy_path_and_trace(client):
    _stock_case(client)
    _seed_card(client)
    r = client.post("/inventory/case-issues", json={
        "card_id": "card-1", "case_id": "case-1", "location_code": UNIT,
        "patient_ref": "Patient/pat-1"}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    lines = {l["sku"]: l for l in r.json()["lines"]}
    assert lines["SKU-IMP"]["qty"] == 2
    alloc = lines["SKU-IMP"]["allocations"][0]
    assert alloc["lot_number"] == "IMP-1" and alloc["qty"] == 2
    assert alloc["unit_cost_cents"] == 5000
    assert alloc["value_cents"] == 10000
    assert on_hand(client, "SKU-IMP", UNIT) == 3
    assert on_hand(client, "SKU-SUP", UNIT) == 7
    drawn = movements(client, event="case_issue",
                      reference="Procedure/case-1")
    assert len(drawn) == 2
    assert all(m["patient_ref"] == "Patient/pat-1" for m in drawn)
    assert all(m["detail"]["card_id"] == "card-1" for m in drawn)
    # GET /case-issues groups the draw for the case.
    grouped = client.get("/inventory/case-issues",
                         params={"case_id": "case-1"},
                         headers=VIEWER).json()
    assert {l["sku"]: l["qty"] for l in grouped["lines"]} == {
        "SKU-IMP": 2, "SKU-SUP": 3}
    # Implant trace, lot -> patient/case direction.
    by_lot = client.get("/inventory/implant-trace",
                        params={"lot_number": "IMP-1"},
                        headers=VIEWER).json()["entries"]
    case_entries = [e for e in by_lot if e["event"] == "case_issue"]
    assert len(case_entries) == 1
    assert case_entries[0]["patient_ref"] == "Patient/pat-1"
    assert case_entries[0]["reference"] == "Procedure/case-1"
    # Implant trace, patient -> lots direction.
    by_patient = client.get("/inventory/implant-trace",
                            params={"patient_ref": "Patient/pat-1"},
                            headers=VIEWER).json()["entries"]
    assert [e["lot_number"] for e in by_patient] == ["IMP-1"]
    # The supply line is not an implant: its draw never traces here.
    assert all(e["sku"] == "SKU-IMP" for e in by_lot + by_patient)


def test_case_issue_shortfall_409_atomic(client):
    _stock_case(client)
    _seed_card(client, card_id="card-big", surgeon="dr-jones",
               procedure_code="PROC-2", items=[
        {"kind": "implant", "code": "SKU-IMP", "qty": 99},
        {"kind": "supply", "code": "SKU-SUP", "qty": 3}])
    before = movements(client)
    r = client.post("/inventory/case-issues", json={
        "card_id": "card-big", "case_id": "case-2", "location_code": UNIT,
        "patient_ref": "Patient/pat-1"}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "SKU-IMP" in r.json()["detail"]
    assert on_hand(client, "SKU-IMP", UNIT) == 5  # nothing written
    assert on_hand(client, "SKU-SUP", UNIT) == 10
    assert movements(client) == before
    # A card line with no inventory item at all -> 409 naming the code.
    _seed_card(client, card_id="card-ghost", surgeon="dr-who",
               procedure_code="PROC-3", items=[
        {"kind": "supply", "code": "SKU-GHOST", "qty": 1}])
    r = client.post("/inventory/case-issues", json={
        "card_id": "card-ghost", "case_id": "case-3",
        "location_code": UNIT}, headers=CLINICIAN)
    assert r.status_code == 409
    assert "SKU-GHOST" in r.json()["detail"]
    # Unknown card -> 404.
    r = client.post("/inventory/case-issues", json={
        "card_id": "card-nope", "case_id": "case-4",
        "location_code": UNIT}, headers=CLINICIAN)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Identification: scan + RFID events
# ---------------------------------------------------------------------------

def test_scan_by_sku_pipe_barcode_and_rfid(client):
    make_item(client, sku="SKU-SCAN", lot_tracked=True)
    receive(client, "SKU-SCAN", quantity=4, lot_number="LOT-9")
    by_sku = client.post("/inventory/scan", json={"value": "SKU-SCAN"},
                         headers=VIEWER)
    assert by_sku.status_code == 200, by_sku.text
    assert by_sku.json()["via"] == "sku"
    assert by_sku.json()["item"]["sku"] == "SKU-SCAN"
    assert by_sku.json()["stock"][0]["quantity"] == 4
    by_pipe = client.post("/inventory/scan",
                          json={"value": "SKU-SCAN|LOT-9"},
                          headers=VIEWER)
    assert by_pipe.status_code == 200
    assert by_pipe.json()["via"] == "pipe"
    assert by_pipe.json()["lot"]["lot_number"] == "LOT-9"
    by_lot = client.post("/inventory/scan", json={"value": "LOT-9"},
                         headers=VIEWER)
    assert by_lot.status_code == 200
    assert by_lot.json()["via"] == "lot"
    # (1) a registered barcode bound to the item.
    with _session(client) as session:
        session.add(BarcodeRecord(
            value="OCAS1|SKU-SCAN", kind="asset",
            entity_ref="InventoryItem/SKU-SCAN", label_fields={},
            status="active", issued_by="test"))
        session.add(RfidTag(
            epc="EPC-SCAN-1", tag_type="inventory",
            entity_type="InventoryItem", entity_id="SKU-SCAN",
            status="bound"))
        session.commit()
    by_barcode = client.post("/inventory/scan",
                             json={"value": "OCAS1|SKU-SCAN"},
                             headers=VIEWER)
    assert by_barcode.status_code == 200
    assert by_barcode.json()["via"] == "barcode"
    by_rfid = client.post("/inventory/scan", json={"value": "EPC-SCAN-1"},
                          headers=VIEWER)
    assert by_rfid.status_code == 200
    assert by_rfid.json()["via"] == "rfid"
    # Every scan was recorded.
    with _session(client) as session:
        events = session.scalars(select(inv.InventoryScanEvent)).all()
        assert len(events) == 5
        assert all(e.resolved_sku == "SKU-SCAN" for e in events)


def test_scan_unknown_404(client):
    r = client.post("/inventory/scan", json={"value": "NOPE-123"},
                    headers=VIEWER)
    assert r.status_code == 404
    with _session(client) as session:
        events = session.scalars(select(inv.InventoryScanEvent)).all()
        assert len(events) == 1  # even the miss is recorded
        assert events[0].resolved_sku is None


def test_rfid_events_bound_and_unknown(client):
    make_item(client, sku="SKU-RFID")
    receive(client, "SKU-RFID", quantity=2)
    with _session(client) as session:
        session.add(RfidTag(
            epc="EPC-INV-1", tag_type="inventory",
            entity_type="InventoryItem", entity_id="SKU-RFID",
            status="bound"))
        session.add(RfidTag(
            epc="EPC-LOOSE", tag_type="inventory", entity_type="",
            entity_id="", status="unbound"))
        session.commit()
    r = client.post("/inventory/rfid-events", json={
        "epc": "EPC-INV-1", "reader_id": "reader-1", "zone": "storage",
        "context": "cycle-count", "location_code": FACILITY},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["resolved"] is True
    assert r.json()["item"]["sku"] == "SKU-RFID"
    assert r.json()["stock"][0]["quantity"] == 2
    with _session(client) as session:
        events = session.scalars(select(inv.InventoryScanEvent).where(
            inv.InventoryScanEvent.kind == "rfid")).all()
        assert len(events) == 1
        assert events[0].value == "EPC-INV-1"
    assert client.post("/inventory/rfid-events", json={
        "epc": "EPC-UNKNOWN", "context": "receiving"},
        headers=CLINICIAN).status_code == 404
    assert client.post("/inventory/rfid-events", json={
        "epc": "EPC-LOOSE", "context": "receiving"},
        headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# Overview + role gates
# ---------------------------------------------------------------------------

def test_overview_aggregates(client):
    make_item(client, sku="SKU-OV", unit_cost_cents=100)
    make_item(client, sku="SKU-OV2")  # uncosted
    receive(client, "SKU-OV", quantity=10)
    receive(client, "SKU-OV2", quantity=5)
    client.put("/inventory/par-levels", json={
        "sku": "SKU-OV", "location_code": FACILITY, "par_level": 20},
        headers=ADMIN)
    r = client.post("/inventory/movements/issue", json={
        "sku": "SKU-OV", "location_code": FACILITY, "quantity": 3},
        headers=CLINICIAN)
    assert r.status_code == 200
    overview = client.get("/inventory/overview", headers=VIEWER).json()
    assert overview["active_items"] == 2
    assert overview["stock_locations"] == 1
    assert overview["on_hand_units"] == 12
    assert overview["stock_value_cents"] == 700
    assert overview["uncosted_lots"] == 1
    assert overview["low_stock_lines"] == 1
    assert overview["movements_today"] == 3  # 2 receives + 1 issue
    assert overview["expiring_lines"] == 0
    assert overview["expired_lines"] == 0


def test_role_gates(client):
    make_item(client)
    # Viewer reads.
    assert client.get("/inventory/items", headers=VIEWER).status_code == 200
    assert client.get("/inventory/overview",
                      headers=VIEWER).status_code == 200
    assert client.get("/inventory/worklists/low-stock",
                      headers=VIEWER).status_code == 200
    # Viewer cannot manage or move.
    assert client.post("/inventory/items", json={
        "sku": "SKU-V", "name": "V", "category": "ppe",
        "subcategory": "exam-gloves"}, headers=VIEWER).status_code == 403
    assert client.post("/inventory/movements/receive", json={
        "sku": "SKU-GLOVE", "location_code": FACILITY, "quantity": 1},
        headers=VIEWER).status_code == 403
    # The patient role holds no inventory capability at all.
    assert client.get("/inventory/items",
                      headers=PATIENT).status_code == 403
    assert client.get("/inventory/overview",
                      headers=PATIENT).status_code == 403
    # Clinician holds manage + move.
    assert client.post("/inventory/items", json={
        "sku": "SKU-CLIN", "name": "Clin", "category": "ppe",
        "subcategory": "exam-gloves"}, headers=CLINICIAN).status_code == 201
    # Configurator holds read (sanity: matrix shipped by WS-B).
    assert client.get("/inventory/items", headers=CONFIG).status_code == 200
