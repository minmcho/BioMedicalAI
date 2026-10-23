"""PLATFORM PASS Workstream 10 — Studio Ops (app.studio_ops).

Composition per the workstream spec: create_app (the composed app
from app.main, which already mounts the studio / pipelines / privacy
routers) + studio_ops.router included by the fixture. Capabilities
exercised are existing ones only: every route rides studio.read.

Synthetic data only.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import studio_ops
from app.main import create_app
from app.studio_ops import (validate_endpoint_declaration,
                            validate_tool_declaration)
from tests.conftest import ADMIN, VIEWER

BRANDING = {"orgName": "Ops Test Clinic",
            "portalTitle": "Ops Test Portal",
            "primaryColor": "#1a7f37"}

ENDPOINT = {"title": "Census read", "method": "GET",
            "path": "/inpatient/census", "capability": "studio.read",
            "backing": "config", "description": "Declaration only."}

TOOL = {"title": "Census MCP tool", "kind": "mcp",
        "description": "Declaration only.", "configRef": "integration/hl7",
        "inputSchema": {"type": "object"}}


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    app.include_router(studio_ops.router)
    with TestClient(app) as c:
        yield c


def _inbox(client, headers=ADMIN):
    r = client.get("/studio-ops/inbox", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

def test_inbox_fresh_app_structure(client):
    # Reality check: create_app seeds only PUBLISHED configs, no pack
    # installations, no break-glass openings, no quarantine holds and
    # no dedup candidates — so a fresh inbox is expected empty. The
    # assertions are structural (consistency of total / by_source /
    # item shape) rather than a hard emptiness claim, per the spec:
    # if a future seed ever adds a draft, this test still verifies
    # the contract instead of failing on the seed change.
    body = _inbox(client)
    assert body["total"] == len(body["items"])
    assert sum(body["by_source"].values()) == body["total"]
    assert body["total"] <= 200
    for item in body["items"]:
        assert set(item) == {"source", "id", "title", "detail",
                             "created_at", "action"}
        assert item["source"] in body["by_source"]


def test_inbox_picks_up_config_draft_and_drops_it_after_publish(client):
    r = client.post("/studio/config/branding/ops-inbox-test",
                    json=BRANDING, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "draft"
    body = _inbox(client)
    matches = [i for i in body["items"]
               if i["source"] == "config-draft"
               and "branding/ops-inbox-test" in i["title"]]
    assert len(matches) == 1
    assert body["by_source"]["config-draft"] >= 1
    r = client.post(
        "/studio/config/branding/ops-inbox-test/versions/1/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    body = _inbox(client)
    assert not [i for i in body["items"]
                if i["source"] == "config-draft"
                and "branding/ops-inbox-test" in i["title"]]


def test_inbox_picks_up_pack_installation_pending_approve(client):
    r = client.post("/studio/packs", headers=ADMIN, json={
        "name": "ops-inbox-pack", "version": "1.0.0",
        "manifest": {"description": "Inbox test pack."}})
    assert r.status_code == 201, r.text
    r = client.post("/studio/packs/ops-inbox-pack/install", headers=ADMIN)
    assert r.status_code == 201, r.text
    installation = r.json()
    assert installation["status"] == "installed_draft"
    body = _inbox(client)
    matches = [i for i in body["items"]
               if i["source"] == "pack-installation"
               and i["id"] == installation["id"]]
    assert len(matches) == 1
    assert "Module packs" in matches[0]["action"]
    assert "approve" in matches[0]["detail"]


# ---------------------------------------------------------------------------
# Organisation
# ---------------------------------------------------------------------------

def test_organisation_returns_seeded_locations_structure(client):
    r = client.get("/studio-ops/organisation", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    totals = body["totals"]
    # Seeded locations: facility-main with unit-medsurg (12 beds) and
    # unit-icu (6 beds).
    assert totals["facilities"] == len(body["facilities"]) >= 1
    assert totals["units"] == sum(len(f["units"])
                                   for f in body["facilities"])
    assert totals["beds"] == sum(len(u["beds"])
                                  for f in body["facilities"]
                                  for u in f["units"])
    assert totals["beds"] > 0
    for facility in body["facilities"]:
        for unit in facility["units"]:
            assert unit["bed_count"] == len(unit["beds"])
    # Staff + accounts summaries are counted from real tables and
    # their breakdowns sum back to their totals.
    assert body["staff"]["total"] > 0
    assert sum(body["staff"]["by_role"].values()) == body["staff"]["total"]
    assert sum(body["staff"]["by_status"].values()) == body["staff"]["total"]
    assert body["accounts"]["total"] > 0
    assert sum(body["accounts"]["by_status"].values()) \
        == body["accounts"]["total"]
    assert body["edit_surfaces"] == ["Units & Beds manager",
                                     "People directory", "Accounts"]


# ---------------------------------------------------------------------------
# Data views
# ---------------------------------------------------------------------------

def test_data_views_lists_datasets_warehouse_and_lake(client):
    r = client.get("/studio-ops/data-views", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == len(body["items"])
    by_kind = {}
    for item in body["items"]:
        assert set(item) == {"name", "kind", "description", "columns",
                             "row_count", "source"}
        assert item["kind"] in ("dataset", "warehouse-table", "lake-zone")
        by_kind.setdefault(item["kind"], []).append(item)
    dataset_names = {i["name"] for i in by_kind["dataset"]}
    assert {"patients", "appointments", "orders", "lab-results",
            "billing-lines"} <= dataset_names
    for item in by_kind["dataset"]:
        assert isinstance(item["row_count"], int) and item["row_count"] >= 0
        assert item["columns"]
    assert by_kind["warehouse-table"]
    for item in by_kind["warehouse-table"]:
        assert item["columns"]
        assert item["row_count"] is None or item["row_count"] >= 0
    lake_names = {i["name"] for i in by_kind["lake-zone"]}
    assert lake_names == {"lake-raw", "lake-curated"}
    for item in by_kind["lake-zone"]:
        assert isinstance(item["row_count"], int) and item["row_count"] >= 0


# ---------------------------------------------------------------------------
# Validators (direct calls)
# ---------------------------------------------------------------------------

def test_endpoint_validator_valid_passes():
    assert validate_endpoint_declaration(ENDPOINT) is None
    assert validate_endpoint_declaration(
        {**ENDPOINT, "enabled": False}) is None


def test_endpoint_validator_rejects():
    cases = [
        {**ENDPOINT, "enabled": True},                    # born disabled
        {**ENDPOINT, "capability": "no.such.capability"},  # unknown cap
        {**ENDPOINT, "path": "inpatient/census"},          # no leading /
        {**ENDPOINT, "method": "FETCH"},                   # bad method
        {**ENDPOINT, "backing": "code"},                   # bad backing
        {k: v for k, v in ENDPOINT.items() if k != "title"},  # no title
    ]
    for payload in cases:
        with pytest.raises(HTTPException) as excinfo:
            validate_endpoint_declaration(payload)
        assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException) as excinfo:
        validate_endpoint_declaration({**ENDPOINT, "enabled": True})
    assert "born disabled" in excinfo.value.detail
    assert "does not create a live route" in excinfo.value.detail


def test_tool_validator_valid_and_rejects():
    assert validate_tool_declaration(TOOL) is None
    assert validate_tool_declaration({"title": "Nightly load",
                                      "kind": "pipeline"}) is None
    for payload in ({**TOOL, "kind": "javascript"},   # code exec: no
                    {**TOOL, "kind": "python"},
                    {**TOOL, "kind": "sql"},
                    {**TOOL, "enabled": True},        # born disabled
                    {**TOOL, "inputSchema": ["not", "an", "object"]}):
        with pytest.raises(HTTPException) as excinfo:
            validate_tool_declaration(payload)
        assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException) as excinfo:
        validate_tool_declaration({**TOOL, "enabled": True})
    assert "born disabled" in excinfo.value.detail


# ---------------------------------------------------------------------------
# Access gates
# ---------------------------------------------------------------------------

def test_viewer_can_read_all_three_routes(client):
    for path in ("/studio-ops/inbox", "/studio-ops/organisation",
                 "/studio-ops/data-views"):
        r = client.get(path, headers=VIEWER)
        assert r.status_code == 200, (path, r.text)


def test_unauthenticated_requests_are_401(client):
    for path in ("/studio-ops/inbox", "/studio-ops/organisation",
                 "/studio-ops/data-views"):
        r = client.get(path)
        assert r.status_code == 401, (path, r.text)
