"""Phase 13 — workforce core + audience dashboards + live analytics.

The workforce module (app.people): staff directory, credentials with an
expiry worklist whose windows come from published config, a rules-engine
scan that raises renewal Tasks before expiry, rosters built on shift
templates from config, on-call schedules with a who-is-on-call-now
lookup, audience dashboards seeded as published config on the Phase 10
mechanism, and analytics computed from LIVE module data (beds/ADT,
roster, credentials, safety, quality) — never fixtures.

Synthetic data only.
"""
from datetime import datetime, timezone

from .conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER


def _publish(client, kind, key, payload, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload,
                    headers=headers)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def _workforce_config(client):
    r = client.get("/people/config", headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Directory + credentials
# ---------------------------------------------------------------------------

def test_directory_seeded_and_gated(client):
    r = client.get("/people/staff", headers=CLINICIAN)
    assert r.status_code == 200
    body = r.json()
    # 18 through Phase 13; the Front Door pass seeds staff-019, the
    # synthetic front-desk registration clerk.
    assert body["total"] == 19
    names = {s["staff_id"]: s for s in body["items"]}
    assert names["staff-001"]["full_name"] == "Maya Chen, MD"
    assert names["staff-001"]["username"] == "dev-clinician"
    assert names["staff-016"]["status"] == "leave"
    # Patient role cannot see the workforce directory.
    assert client.get("/people/staff", headers=PATIENT).status_code == 403
    r = client.get("/people/staff/staff-007", headers=VIEWER)
    assert r.status_code == 200
    assert r.json()["credentials"], "detail carries the member's credentials"


def test_staff_create_validates_against_config(client):
    r = client.post("/people/staff", json={
        "staff_id": "staff-100", "full_name": "New Nurse, RN",
        "job_role": "nurse", "unit": "unit-icu",
        "service_line": "critical-care"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    assert client.post("/people/staff", json={
        "staff_id": "staff-100", "full_name": "Dup", "job_role": "nurse",
        "unit": "unit-icu", "service_line": "critical-care"},
        headers=ADMIN).status_code == 409
    # Job role outside the published config concepts is refused.
    r = client.post("/people/staff", json={
        "staff_id": "staff-101", "full_name": "Bad Role",
        "job_role": "wizard", "unit": "unit-icu",
        "service_line": "critical-care"}, headers=ADMIN)
    assert r.status_code == 422
    # Clinician cannot write workforce records.
    assert client.post("/people/staff", json={
        "staff_id": "staff-102", "full_name": "Nope", "job_role": "nurse",
        "unit": "unit-icu", "service_line": "critical-care"},
        headers=CLINICIAN).status_code == 403


def test_credential_worklist_windows_from_config(client):
    r = client.get("/people/credentials/worklist", headers=CLINICIAN)
    assert r.status_code == 200
    body = r.json()
    assert body["worklist_window_days"] == 90
    assert body["alert_window_days"] == 30
    assert body["expired"] == 1  # the one seeded lapsed licence
    by_status = {i["credential_id"]: i for i in body["items"]}
    expired = [i for i in body["items"] if i["status"] == "expired"]
    assert expired and expired[0]["staff_id"] == "staff-004"
    assert all(i["days_to_expiry"] <= 90 for i in body["items"])
    # The +120-day credential (cred-008) and the +95-day one
    # (cred-017) sit outside the 90-day worklist window.
    assert "cred-008" not in by_status
    assert "cred-017" not in by_status
    # Sorted soonest first: the expired credential leads.
    assert body["items"][0]["status"] == "expired"

    # Republish the workforce config with a 14-day worklist window:
    # the worklist shrinks — windows are config, not code.
    payload = _workforce_config(client)
    payload["expiry"] = {"worklistWindowDays": 14, "alertWindowDays": 7}
    _publish(client, "terminology", "workforce", payload)
    r = client.get("/people/credentials/worklist", headers=CLINICIAN)
    body = r.json()
    assert body["worklist_window_days"] == 14
    assert all(i["days_to_expiry"] <= 14 for i in body["items"])
    assert body["total"] < 15


def test_credential_scan_alerts_via_rules_engine_once(client):
    before = client.get("/fhir/Task", headers=ADMIN).json()["total"]
    r = client.post("/people/credentials/scan", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["scanned"] == 27
    # Seeded alert-window credentials: +20, +15, +10, -5, +25, +28, +12.
    assert body["alerted"] == 7
    after = client.get("/fhir/Task", headers=ADMIN).json()
    assert after["total"] == before + 7
    titles = [e["resource"]["description"] for e in after["entry"]]
    assert any("Credential expiring" in t for t in titles)
    # Idempotent: a second scan alerts nobody new.
    r = client.post("/people/credentials/scan", headers=ADMIN)
    assert r.json()["alerted"] == 0
    assert client.get("/fhir/Task", headers=ADMIN).json()["total"] == before + 7
    # Clinicians cannot run the scan.
    assert client.post("/people/credentials/scan",
                       headers=CLINICIAN).status_code == 403


def test_credential_create_validates_type_and_dates(client):
    r = client.post("/people/credentials", json={
        "credential_id": "cred-900", "staff_id": "staff-003",
        "type": "rn-licence", "number": "SYN-RN-900",
        "issued_on": "2026-01-01", "expires_on": "2028-01-01"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    assert client.post("/people/credentials", json={
        "credential_id": "cred-901", "staff_id": "staff-003",
        "type": "pilot-licence", "expires_on": "2028-01-01"},
        headers=ADMIN).status_code == 422
    assert client.post("/people/credentials", json={
        "credential_id": "cred-902", "staff_id": "staff-999",
        "type": "bls", "expires_on": "2028-01-01"},
        headers=ADMIN).status_code == 404
    assert client.post("/people/credentials", json={
        "credential_id": "cred-903", "staff_id": "staff-003",
        "type": "bls", "expires_on": "not-a-date"},
        headers=ADMIN).status_code == 422


# ---------------------------------------------------------------------------
# Rosters + on-call
# ---------------------------------------------------------------------------

def test_roster_create_computes_shift_instants_and_blocks_duplicates(client):
    today = datetime.now(timezone.utc).date().isoformat()
    r = client.post("/people/roster", json={
        "shift_id": "shift-x1", "staff_id": "staff-006",
        "unit": "unit-icu", "date": today, "template": "night"},
        headers=ADMIN)
    assert r.status_code == 201, r.text
    body = r.json()
    # Night template 19:00 -> next-day 07:00 (overnight roll).
    assert body["starts_at"].endswith("T19:00:00+00:00")
    assert body["ends_at"][8:10] != body["starts_at"][8:10]
    assert body["ends_at"].endswith("T07:00:00+00:00")
    assert client.post("/people/roster", json={
        "shift_id": "shift-x2", "staff_id": "staff-006",
        "unit": "unit-icu", "date": today, "template": "night"},
        headers=ADMIN).status_code == 409
    assert client.post("/people/roster", json={
        "shift_id": "shift-x3", "staff_id": "staff-006",
        "unit": "unit-icu", "date": today, "template": "graveyard"},
        headers=ADMIN).status_code == 422


def test_on_call_now_lookup_and_overlap_guard(client):
    r = client.get("/people/on-call/now", headers=CLINICIAN)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 6 and body["covered"] == 5
    by_line = {s["service_line"]: s for s in body["services"]}
    assert by_line["medicine"]["primary"]["staff_id"] == "staff-001"
    assert by_line["medicine"]["backup"]["staff_id"] == "staff-015"
    # Surgery is honestly uncovered — no fake coverage.
    assert by_line["surgery"]["primary"] is None
    # Overlapping the seeded medicine primary window is refused.
    now = datetime.now(timezone.utc)
    from datetime import timedelta
    r = client.post("/people/on-call", json={
        "assignment_id": "oc-x1", "service_line": "medicine",
        "staff_id": "staff-002", "role_on_call": "primary",
        "starts_at": now.isoformat(),
        "ends_at": (now + timedelta(days=1)).isoformat()},
        headers=ADMIN)
    assert r.status_code == 409
    # A backup for the uncovered line is accepted.
    r = client.post("/people/on-call", json={
        "assignment_id": "oc-x2", "service_line": "surgery",
        "staff_id": "staff-002", "role_on_call": "backup",
        "starts_at": "2030-01-01T00:00:00+00:00",
        "ends_at": "2030-01-08T00:00:00+00:00"}, headers=ADMIN)
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Audience dashboards + role-aware home
# ---------------------------------------------------------------------------

def test_audience_dashboards_seeded_published(client):
    r = client.get("/studio/config/dashboard/effective", headers=ADMIN)
    assert r.status_code == 200
    keys = {row["key"] for row in r.json()}
    for key in ("audience-clinician-day", "audience-nurse-unit",
                "audience-unit-manager", "audience-department-head",
                "audience-executive"):
        assert key in keys
    eff = client.get("/studio/config/dashboard/effective/audience-executive",
                     headers=ADMIN)
    assert eff.status_code == 200
    payload = eff.json()["payload"]
    assert payload["audience"] == "executive"
    displays = {w["display"] for w in payload["widgets"]}
    assert {"big-number", "gauge", "line", "table"} <= displays
    # Bar charts ride the nurse/unit dashboard (unit occupancy) and
    # the department-head dashboard (census by day).
    eff = client.get(
        "/studio/config/dashboard/effective/audience-nurse-unit",
        headers=ADMIN)
    displays = {w["display"] for w in eff.json()["payload"]["widgets"]}
    assert "bar" in displays


def test_home_dashboard_resolves_per_user(client):
    # Dev admin is linked to the executive staff record.
    r = client.get("/people/home-dashboard", headers=ADMIN)
    body = r.json()
    assert body["audience"] == "executive"
    assert body["dashboard_key"] == "audience-executive"
    assert body["dashboard"] is not None
    # Dev clinician -> clinician audience via the directory link.
    r = client.get("/people/home-dashboard", headers=CLINICIAN)
    assert r.json()["audience"] == "clinician"
    assert r.json()["dashboard_key"] == "audience-clinician-day"
    # Dev viewer -> unit manager.
    r = client.get("/people/home-dashboard", headers=VIEWER)
    assert r.json()["audience"] == "unit-manager"
    # Configurator has no directory link: role map applies.
    r = client.get("/people/home-dashboard", headers=CONFIG)
    assert r.json()["audience"] == "department-head"
    # Patients get no staff home.
    assert client.get("/people/home-dashboard",
                      headers=PATIENT).status_code == 403


def test_home_dashboard_falls_back_when_unpublished(client):
    # Point the executive audience at a dashboard key that does not
    # exist: the endpoint returns dashboard null (shell falls back).
    payload = _workforce_config(client)
    payload["audienceDashboards"]["executive"] = "audience-missing"
    _publish(client, "terminology", "workforce", payload)
    r = client.get("/people/home-dashboard", headers=ADMIN)
    body = r.json()
    assert body["audience"] == "executive"
    assert body["dashboard"] is None


def test_dashboard_validator_accepts_chart_widgets_and_rejects_bad_ones(client):
    good = {"title": "T", "widgets": [
        {"id": "w1", "title": "Series", "endpoint": "/people/analytics/units",
         "display": "bar", "labelField": "unit",
         "series": [{"field": "occupied", "label": "Occupied"}]},
        {"id": "w2", "title": "Gauge", "endpoint": "/people/analytics/coverage",
         "display": "gauge", "field": "overall_pct", "max": 100,
         "thresholds": {"warn": 90, "critical": 75}},
        {"id": "w3", "title": "Grid", "endpoint": "/people/analytics/units",
         "display": "table",
         "columns": [{"field": "unit", "title": "Unit"}]}]}
    r = client.post("/studio/config/dashboard/test-charts",
                    json=good, headers=ADMIN)
    assert r.status_code == 201, r.text
    bad = {"title": "T", "widgets": [
        {"id": "w1", "title": "X", "endpoint": "/x", "display": "pie"}]}
    r = client.post("/studio/config/dashboard/test-bad",
                    json=bad, headers=ADMIN)
    assert r.status_code == 422
    bad2 = {"title": "T", "widgets": [
        {"id": "w1", "title": "X", "endpoint": "/x", "display": "bar",
         "series": [{"field": "a"}]}]}  # bar without labelField
    r = client.post("/studio/config/dashboard/test-bad2",
                    json=bad2, headers=ADMIN)
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Analytics — computed from live module data
# ---------------------------------------------------------------------------

def _patient(client, pid="pat-1"):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": ["Ana"]}],
        "gender": "female", "birthDate": "1980-04-02"}, headers=ADMIN)
    assert r.status_code == 201, r.text


def test_analytics_reflect_live_admissions(client):
    r = client.get("/people/analytics/overview", headers=ADMIN)
    assert r.json()["occupied"] == 0
    _patient(client)
    r = client.post("/inpatient/admissions",
                    json={"patient": "pat-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.get("/people/analytics/overview", headers=ADMIN)
    body = r.json()
    assert body["occupied"] == 1
    assert body["admissions_today"] == 1
    assert body["beds_total"] == 18
    units = client.get("/people/analytics/units", headers=ADMIN).json()
    medsurg = [u for u in units["items"] if u["unit"] == "unit-medsurg"][0]
    assert medsurg["occupied"] == 1 and medsurg["beds_total"] == 12
    series = client.get("/people/analytics/census-series?days=1",
                        headers=ADMIN).json()
    assert series["items"][0]["admissions"] == 1
    assert series["items"][0]["census"] == 1


def test_coverage_fill_rate_semantics(client):
    r = client.get("/people/analytics/coverage", headers=ADMIN)
    body = r.json()
    # Seeded roster: med-surg 5/3, icu 3/2, ed 4/3, clinic 1/2.
    # Fill-rate: (3 + 2 + 3 + 1) / 10 = 90 — over-staffing one unit
    # does not cover another unit's gap.
    assert body["overall_pct"] == 90.0
    assert sum(i["required"] for i in body["items"]) == 10
    assert sum(i["scheduled"] for i in body["items"]) == 13
    clinic = [i for i in body["items"] if i["unit"] == "clinic"][0]
    assert clinic["coverage_pct"] == 50.0


def test_service_lines_rollup(client):
    r = client.get("/people/analytics/service-lines", headers=ADMIN)
    by_line = {i["service_line"]: i for i in r.json()["items"]}
    # Medicine had 7 active staff through Phase 13; the Front Door
    # pass seeds staff-019 (front desk, clinic, medicine) -> 8.
    assert by_line["medicine"]["staff"] == 8
    assert by_line["medicine"]["on_call"] == "Maya Chen, MD"
    assert by_line["surgery"]["on_call"] is None
    assert by_line["surgery"]["staff"] == 0
