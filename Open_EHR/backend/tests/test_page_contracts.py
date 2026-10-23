"""Home & Module UX pass (WS6) — page↔API contract pins.

The Command Center lesson (Organisation pass): a page white-screened
in production because its web fixture drifted from the real backend
payload and no backend test pinned the contract. These tests pin the
EXACT response key sets the high-traffic pages consume, against the
real endpoints on a real (synthetic) database — never fixtures.
Key sets were authored from live response dumps on 2026-10-19.

Exact-set pins are used for code-shaped payloads. For payloads whose
shape is published Studio config (the home dashboard definition),
the pin is the required-key subset the page dereferences, because a
site may legitimately extend its own config.

Also pinned here (parent steer, 2026-10-19): the web/src
import-resolution case-collision audit — two files in one directory
whose basenames differ only by case are invisible on Linux and
fatal on a case-insensitive filesystem (the IdleLock/idleLock
white-screen on the Mac).

Synthetic data only.
"""
import os
from collections import defaultdict

from tests.conftest import ADMIN, CLINICIAN

POTASSIUM = "2823-3"


# ---------------------------------------------------------------------------
# Command Center — GET /continuum/command/overview (CommandCenterPage)
# ---------------------------------------------------------------------------

def test_command_overview_contract(client):
    r = client.get("/continuum/command/overview", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {
        "generated_at", "census", "expected_discharges_today", "ed",
        "boarding_count", "ed_boarding_count", "oldest_boarding_minutes",
        "boarding", "bed_requests", "accepted_transfers", "projection",
        "discharges_today", "transport_open", "diversion", "surge_level",
    }
    assert set(body["census"]) == {"totals", "units"}
    totals_keys = {"beds_total", "available", "occupied", "cleaning",
                   "blocked", "occupancy_pct"}
    assert set(body["census"]["totals"]) == totals_keys
    assert body["census"]["units"], "seeded census must have units"
    for unit in body["census"]["units"]:
        assert set(unit) == totals_keys | {"code", "display", "beds"}
    assert set(body["boarding"]) == {
        "total", "items", "ed_total", "oldest_wait_minutes"}
    assert set(body["projection"]) == {
        "projected_occupied_end_of_day", "projected_occupancy_pct",
        "basis"}
    assert set(body["expected_discharges_today"]) == {
        "date", "discharges", "total"}
    assert set(body["ed"]) == {"counts"}
    assert set(body["discharges_today"]) == {"total", "before_noon"}
    # The counters the page header renders must agree with the
    # boarding object it dereferences (the original drift class).
    assert body["boarding_count"] == body["boarding"]["total"]
    assert body["ed_boarding_count"] == body["boarding"]["ed_total"]
    assert body["oldest_boarding_minutes"] == \
        body["boarding"]["oldest_wait_minutes"]


def test_command_overview_scoped_contract(client):
    r = client.get("/continuum/command/overview?unit=unit-medsurg",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body["scope"]) == {
        "region", "location", "department", "unit", "unit_codes"}


# ---------------------------------------------------------------------------
# Home — GET /people/home-dashboard (HomePage) + GET /home/cards
# ---------------------------------------------------------------------------

def test_home_dashboard_contract(client):
    r = client.get("/people/home-dashboard", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"audience", "dashboard_key", "dashboard"}
    # dashboard is published config: pin the keys HomePage reads.
    assert {"title", "description", "audience", "roles",
            "widgets"} <= set(body["dashboard"])
    for widget in body["dashboard"]["widgets"]:
        assert {"id", "title", "endpoint", "display"} <= set(widget)
        # DashboardView reads widget.field optionally; series and
        # sourced widgets carry their value path differently — but
        # every widget must name SOME value source or it renders
        # an empty panel (the drift this pin exists to catch).
        assert {"field", "series", "sourceField"} & set(widget), widget


def test_home_cards_contract(client):
    r = client.get("/home/cards", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"cards"}
    assert set(body["cards"]) == {
        "inbox", "lab", "pharmacy", "beds", "command", "ed", "safety",
        "workforce", "scheduling",
        # Inventory & Materials pass: real worklist count sources.
        "inventory"}
    for module, card in body["cards"].items():
        assert {"value", "label", "status_text", "tone"} <= set(card), \
            module
        assert set(card) <= {"value", "label", "status_text", "tone",
                             "timer_text"}, module


def test_home_effective_modules_contract(client):
    r = client.get("/home/effective-modules", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"modules", "job_role"}


# ---------------------------------------------------------------------------
# Bed board / census — BedBoardPage + UnitBoardPage + CommandCenterPage
# ---------------------------------------------------------------------------

_CENSUS_TOTALS = {"beds_total", "available", "occupied", "cleaning",
                  "blocked", "occupancy_pct"}


def test_census_contract(client):
    r = client.get("/inpatient/census", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"generated_at", "units", "totals",
                         "expected_discharges_today"}
    assert set(body["totals"]) == _CENSUS_TOTALS
    for unit in body["units"]:
        assert set(unit) == _CENSUS_TOTALS | {"code", "display", "beds"}


def test_bed_board_contract(client):
    r = client.get("/inpatient/beds", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"beds", "total"}
    assert body["beds"], "seeded bed stock must exist"
    for bed in body["beds"]:
        assert set(bed) == {
            "code", "display", "room", "bed", "unit", "status",
            "patient_ref", "encounter_id", "admitted_at", "los_days",
            "unit_display"}


def test_er_board_contract(client):
    r = client.get("/continuum/er/board", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"generated_at", "counts", "groups", "queues",
                         "diversion", "surge_level"}
    assert set(body["counts"]) == {
        "waiting", "triage", "in-treatment", "disposition", "total",
        "target_breaches"}
    assert set(body["queues"]) == {"fast_track", "main"}


# ---------------------------------------------------------------------------
# Laboratory — order detail + test detail (LabPage)
# ---------------------------------------------------------------------------

def _patient_and_order(client):
    r = client.post("/fhir/Patient",
                    json={"resourceType": "Patient", "id": "pat-1",
                          "name": [{"family": "Synthetic",
                                    "given": ["Contract"]}]},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/lab/orders",
                    json={"code": POTASSIUM,
                          "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_lab_order_detail_contract(client):
    order_id = _patient_and_order(client)
    r = client.get(f"/lab/orders/{order_id}", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"order", "specimen", "reports", "custody"}
    assert set(body["custody"]) == {
        "ordered_at", "collected_at", "received_at", "in_analysis_at"}


def test_lab_test_detail_contract(client):
    order_id = _patient_and_order(client)
    r = client.get(f"/lab/orders/{order_id}/test-detail",
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"order", "steps", "specimen", "aliquots",
                         "results", "report", "acknowledgements",
                         "custody"}
    assert set(body["order"]) == {
        "id", "status", "code", "display", "patient_ref", "authored_on",
        "requester", "priority"}
    assert body["steps"], "a fresh order still carries its step chain"
    for step in body["steps"]:
        assert set(step) == {"key", "label", "state", "at", "by"}
    assert [s["key"] for s in body["steps"]] == [
        "ordered", "collected", "received", "in_analysis", "analysed",
        "verified", "resulted"]


# ---------------------------------------------------------------------------
# Pharmacy verification queue (VerificationTab) + results inbox (InboxPage)
# ---------------------------------------------------------------------------

def test_verification_queue_contract(client):
    r = client.get("/pharmacy/verification-queue", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    # Gap Wave A (PHARM-M1): the queue additively carries the governed
    # DUR override reason codes + the verification validity window so
    # the Verification tab renders the published governance from the
    # same payload (consumed in web/src/pages/pharmacy/
    # VerificationTab.tsx) — the exact-set pin moves with the contract.
    assert set(r.json()) == {"items", "total", "dur_override_reason_codes",
                             "verification_validity_hours"}


def test_inbox_results_contract(client):
    r = client.get("/inbox/results", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"items", "total", "page_size", "limit",
                         "truncated", "scope", "filters"}
    assert set(body["filters"]) == {"kind", "flag", "ack", "patient"}


def test_inbox_summary_contract(client):
    r = client.get("/inbox/summary", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"results", "continuum", "generated_at"}
    assert set(body["results"]) == {
        "unacknowledged", "critical_unacknowledged", "module",
        "nav_hint"}
    assert set(body["continuum"]) == {
        "open_tasks", "breached", "module", "nav_hint"}
    assert set(body["results"]["nav_hint"]) == {"module", "label"}


# ---------------------------------------------------------------------------
# Indicators — value + records drill (AnalyticsPage)
# ---------------------------------------------------------------------------

def test_indicator_value_and_records_contract(client):
    key = "continuum-boarding-total"
    r = client.get(f"/indicators/{key}/value", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    value = r.json()
    assert set(value) == {"key", "value", "unit", "target", "status",
                          "evaluated_at", "source", "detail"}
    assert set(value["source"]) == {"type", "name"}

    r = client.get(f"/indicators/{key}/records", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"key", "definition", "value", "records",
                         "records_supported", "reason"}
    assert set(body["definition"]) == {
        "name", "title", "source", "direction", "thresholds", "unit"}
    # The drill embeds the same value payload the card consumed.
    assert set(body["value"]) == set(value)
    assert body["value"]["value"] == value["value"]


# ---------------------------------------------------------------------------
# Org hierarchy (registration cascades, UnitBoard rounds units, Home)
# ---------------------------------------------------------------------------

def test_org_hierarchy_contract(client):
    r = client.get("/org-hierarchy", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"version", "regions", "orphans"}
    assert set(body["orphans"]) == {"location", "department", "unit"}
    unit = None
    for region in body["regions"]:
        assert set(region) == {"code", "display", "locations"}
        for location in region["locations"]:
            assert {"code", "display", "departments"} <= set(location)
            for department in location["departments"]:
                assert set(department) == {"code", "display", "units"}
                for candidate in department["units"]:
                    assert set(candidate) == {"code", "display",
                                              "bed_count"}
                    unit = unit or candidate
    assert unit is not None, "seeded tree must contain a unit"


# ---------------------------------------------------------------------------
# Web source hygiene — the case-collision class (parent-named check)
# ---------------------------------------------------------------------------

def _web_src_root():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "web", "src"))


def test_web_src_no_case_colliding_basenames():
    root = _web_src_root()
    assert os.path.isdir(root), root
    by_dir = defaultdict(list)
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            by_dir[dirpath].append(name)
    collisions = []
    for dirpath, names in by_dir.items():
        seen = {}
        for name in names:
            stem = name.rsplit(".", 1)[0].lower()
            if stem in seen and seen[stem] != name:
                collisions.append(
                    f"{os.path.relpath(dirpath, root)}: "
                    f"{seen[stem]} vs {name}")
            seen[stem] = name
    assert collisions == [], (
        "same-directory basenames differing only by case break "
        "imports on case-insensitive filesystems: "
        + "; ".join(collisions))


def test_web_src_session_security_rename_pinned():
    root = _web_src_root()
    auth = os.path.join(root, "auth")
    assert os.path.isfile(os.path.join(auth, "sessionSecurity.ts"))
    assert not os.path.exists(os.path.join(auth, "idleLock.ts")), (
        "idleLock.ts must never come back — it collides with "
        "IdleLock.tsx on case-insensitive filesystems")
    assert os.path.isfile(os.path.join(auth, "IdleLock.tsx"))
