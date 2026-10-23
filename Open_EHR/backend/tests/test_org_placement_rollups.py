"""Organisation & Registration Placement pass — WS3 filters &
rollups: org-placement scoping on the Capacity Command Center
overview (app.continuum.command_overview) and placement views on
care teams (app.teams._team_out + the team panel).

The hierarchy under test is the governed seed (terminology/
locations, per the pass contract): region-main "OpenCare Region"
-> facility-main "OpenCare Hospital" -> dept-medicine ->
unit-medsurg (12 beds), dept-critical-care -> unit-icu (6 beds),
dept-emergency -> ed, dept-ambulatory -> clinic (18 beds total).

Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN

OVERVIEW_KEYS = {"generated_at", "census", "expected_discharges_today",
                 "ed", "boarding_count", "ed_boarding_count",
                 "oldest_boarding_minutes", "boarding", "bed_requests",
                 "accepted_transfers", "projection", "discharges_today",
                 "transport_open", "diversion", "surge_level"}


def _overview(client, **params):
    r = client.get("/continuum/command/overview", params=params,
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Command overview — unscoped is byte-shape identical to before the pass
# ---------------------------------------------------------------------------

def test_command_overview_unscoped_shape_unchanged(client):
    body = _overview(client)
    assert set(body) == OVERVIEW_KEYS
    assert "scope" not in body
    assert body["census"]["totals"]["beds_total"] == 18
    codes = {u["code"] for u in body["census"]["units"]}
    assert {"unit-medsurg", "unit-icu"} <= codes
    assert "deterministic" in body["projection"]["basis"]
    assert "SCOPE" not in body["projection"]["basis"]


# ---------------------------------------------------------------------------
# Command overview — scoped census + totals + projection basis
# ---------------------------------------------------------------------------

def test_command_overview_scoped_by_department(client):
    body = _overview(client, department_id="dept-critical-care")
    assert [u["code"] for u in body["census"]["units"]] == ["unit-icu"]
    assert body["census"]["totals"]["beds_total"] == 6
    assert body["scope"] == {"region": None, "location": None,
                             "department": "dept-critical-care",
                             "unit": None, "unit_codes": ["unit-icu"]}
    # The projection is recomputed from the scoped totals and its
    # basis names the scope honestly.
    assert "SCOPE" in body["projection"]["basis"]
    assert body["projection"]["projected_occupancy_pct"] >= 0.0
    # Facility-wide surfaces are still present in scoped mode.
    assert "counts" in body["ed"]
    assert "transport_open" in body
    assert "expected_discharges_today" in body


def test_command_overview_scoped_by_region(client):
    body = _overview(client, region_id="region-main")
    assert body["census"]["totals"]["beds_total"] == 18
    assert body["scope"]["region"] == "region-main"
    assert {"unit-medsurg", "unit-icu"} <= set(body["scope"]["unit_codes"])
    assert body["scope"]["unit_codes"] == sorted(body["scope"]["unit_codes"])


def test_command_overview_scoped_by_unit(client):
    body = _overview(client, unit="unit-medsurg")
    assert [u["code"] for u in body["census"]["units"]] == ["unit-medsurg"]
    assert body["census"]["totals"]["beds_total"] == 12
    assert body["scope"]["unit"] == "unit-medsurg"
    assert body["scope"]["unit_codes"] == ["unit-medsurg"]


def test_command_overview_scoped_by_location(client):
    body = _overview(client, location_id="facility-main")
    assert body["census"]["totals"]["beds_total"] == 18
    assert body["scope"]["location"] == "facility-main"


def test_command_overview_unknown_department_is_422(client):
    r = client.get("/continuum/command/overview",
                   params={"department_id": "dept-nope"},
                   headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "dept-nope" in r.text


def test_command_overview_broken_chain_is_422(client):
    # unit-icu belongs to dept-critical-care, not dept-medicine.
    r = client.get("/continuum/command/overview",
                   params={"department_id": "dept-medicine",
                           "unit": "unit-icu"},
                   headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Teams — placement view on _team_out and the team panel context
# ---------------------------------------------------------------------------

def test_team_out_carries_placement_for_icu_team(client):
    detail = client.get("/teams/team-icu", headers=CLINICIAN).json()
    placement = detail["placement"]
    assert placement["department"] == {"code": "dept-critical-care",
                                       "display": "Critical Care"}
    assert placement["unit"] == {"code": "unit-icu",
                                 "display": "Intensive Care Unit"}
    assert placement["location"] == {"code": "facility-main",
                                     "display": "OpenCare Hospital"}
    assert placement["region"] == {"code": "region-main",
                                   "display": "OpenCare Region"}
    # The list surface carries it too (same serializer).
    listing = client.get("/teams/", headers=ADMIN).json()
    by_id = {t["team_id"]: t for t in listing["items"]}
    assert by_id["team-icu"]["placement"]["department"]["code"] == \
        "dept-critical-care"
    assert by_id["team-medsurg-a"]["placement"]["department"]["code"] == \
        "dept-medicine"


def test_team_panel_context_carries_placement(client):
    panel = client.get("/teams/team-icu/panel", headers=CLINICIAN).json()
    placement = panel["team"]["placement"]
    assert placement["department"] == {"code": "dept-critical-care",
                                       "display": "Critical Care"}
    assert placement["unit"]["code"] == "unit-icu"


def test_team_without_unit_has_no_placement(client):
    detail = client.get("/teams/team-cardiology-consult",
                        headers=CLINICIAN).json()
    assert detail["unit"] is None
    assert detail["placement"] is None


# ---------------------------------------------------------------------------
# Reports — credential-expiry placement params (staff-based report)
# ---------------------------------------------------------------------------

def test_credential_expiry_report_accepts_placement_params(client):
    # The seeded definition declares the placement parameters; an
    # unknown placement id is a 422 naming it (resolve_scope), and a
    # valid department scope runs (rows depend on seeded staff
    # placement backfill, so only the run itself is asserted).
    r = client.post("/reports/credential-expiry/run",
                    json={"format": "csv",
                          "params": {"department_id": "dept-nope"}},
                    headers=ADMIN)
    assert r.status_code == 422, r.text
    assert "dept-nope" in r.text
    r = client.post("/reports/credential-expiry/run",
                    json={"format": "csv",
                          "params": {"department_id": "dept-critical-care"}},
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
