"""Phase 15 — platform depth (app.platform_depth): BD-33 immunisation
record + VXU, BD-29 service-line/episode costing over the warehouse,
BD-30 dashboards rewired to the indicator library, BD-32 report
designer backend support (dataset schema + parity proof).

BD-33 note: `Immunization` is not yet in app.fhir.SUPPORTED (see the
workstream report's INTEGRATION-NEEDED), so these tests exercise
this module's own endpoints, which write through
app.fhir.create_resource into the same FHIR store.

Synthetic data only.
"""
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import etl, hl7
from app import warehouse as wh
from app.configstore import list_config_versions, published_config
from app.db import Base
from app.main import create_app
from app.platform_depth import ensure_platform_depth_defaults
from tests.conftest import ADMIN, CLINICIAN, CONFIG, PATIENT, VIEWER

ACTCODE = "http://terminology.hl7.org/CodeSystem/v3-ActCode"
LOINC = "http://loinc.org"
CPT = "http://www.ama-assn.org/go/cpt"


@pytest.fixture()
def client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/t.db")
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as c:
        c.test_app = app
        yield c


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "identifier": [{"system": "urn:opencare:synthetic-mrn",
                        "value": f"MRN-{pid}"}],
        "name": [{"family": "Synthetic15", "given": ["Depth"]}],
        "gender": "female", "birthDate": "1990-06-15"}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return pid


def _immunise(client, pid="pat-1", **overrides):
    body = {"patient": pid, "vaccine_code": "141", "lot_number": "LOT-42",
            "date": "2026-10-18T15:30:00Z", "status": "completed",
            "dose_value": 0.5, "dose_unit": "mL",
            "manufacturer": "Synthetic Vaccines Inc",
            "route_code": "47625008", "route_display": "Intravenous route",
            "site_code": "368208006", "site_display": "Left arm"}
    body.update(overrides)
    r = client.post("/immunisations", json=body, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# BD-33: VXU builder (golden, in the Phase 8 fixture style)
# ---------------------------------------------------------------------------

PATIENT_BODY = {"resourceType": "Patient", "id": "pat-1",
                "identifier": [{"system": "urn:opencare:synthetic-mrn",
                                "value": "MRN-pat-1"}],
                "name": [{"family": "Synthetic15", "given": ["Depth"]}],
                "gender": "female", "birthDate": "1990-06-15"}

IMM_BODY = {"resourceType": "Immunization", "id": "imm-abc",
            "status": "completed",
            "vaccineCode": {"coding": [{
                "system": "http://hl7.org/fhir/sid/cvx",
                "code": "141", "display": "Influenza, seasonal, injectable"}]},
            "patient": {"reference": "Patient/pat-1"},
            "occurrenceDateTime": "2026-10-18T15:30:00Z",
            "lotNumber": "LOT-42",
            "doseQuantity": {"value": 0.5, "unit": "mL"},
            "manufacturer": {"display": "Synthetic Vaccines Inc"},
            "route": {"coding": [{"system": "http://snomed.info/sct",
                                  "code": "47625008",
                                  "display": "Intravenous route"}]},
            "site": {"coding": [{"system": "http://snomed.info/sct",
                                 "code": "368208006", "display": "Left arm"}]}}


def _segments(message):
    return [line.split("|") for line in message.split("\r")]


def test_build_vxu_golden():
    message = hl7.build_vxu(
        IMM_BODY, PATIENT_BODY, control_id="VXU0001",
        sending_application="OPENCARE", sending_facility="OPENCARE HOSPITAL",
        receiving_application="STATE-IIS",
        receiving_facility="SYNTHETIC STATE IMMUNISATION REGISTRY")
    segs = _segments(message)
    assert [s[0] for s in segs] == ["MSH", "PID", "ORC", "RXA", "RXR"]
    msh = segs[0]
    assert msh[8] == "VXU^V04"                       # MSH-9
    assert "VXU0001" in msh                          # MSH-10 control id
    assert msh[2] == "OPENCARE" and msh[4] == "STATE-IIS"
    pid = segs[1]
    assert pid[3].startswith("pat-1") and "MRN-pat-1" in pid[3]
    assert pid[5] == "Synthetic15^Depth"
    orc = segs[2]
    assert orc[1] == "RE" and orc[2] == "imm-abc"
    rxa = segs[3]
    assert rxa[1] == "0" and rxa[2] == "1"
    assert rxa[3] == "20261018153000"                # RXA-3 administration dt
    assert rxa[5] == "141^Influenza, seasonal, injectable^CVX"  # RXA-5
    assert rxa[6] == "0.5" and rxa[7] == "mL"        # RXA-6/7 amount+units
    assert rxa[15] == "LOT-42"                       # RXA-15 lot number
    assert rxa[17] == "^Synthetic Vaccines Inc^MVX"  # RXA-17 manufacturer
    assert rxa[20] == "CP"                           # RXA-20 complete
    rxr = segs[4]
    assert rxr[1].startswith("47625008^Intravenous route")
    assert rxr[2].startswith("368208006^Left arm")
    # The shared reader round-trips it.
    parsed = hl7.parse_segments(message)
    assert hl7.message_type_of(parsed) == "VXU^V04"
    assert hl7.control_id_of(parsed) == "VXU0001"
    assert hl7.patient_id_of(parsed) == "pat-1"


def test_build_vxu_without_route_has_no_rxr():
    body = {k: v for k, v in IMM_BODY.items() if k not in ("route", "site")}
    message = hl7.build_vxu(body, PATIENT_BODY, control_id="VXU0002",
                            sending_application="OPENCARE",
                            sending_facility="OPENCARE HOSPITAL")
    assert [s[0] for s in _segments(message)] == ["MSH", "PID", "ORC", "RXA"]


# ---------------------------------------------------------------------------
# BD-33: capture, validation, list, portal scoping
# ---------------------------------------------------------------------------

def test_immunisation_capture_and_validation(client):
    _patient(client, "pat-1")
    # Unknown vaccine code -> 422 against the published CVX list.
    r = client.post("/immunisations", json={
        "patient": "pat-1", "vaccine_code": "999", "lot_number": "L1",
        "date": "2026-10-18T10:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 422 and "vaccines" in r.json()["detail"]
    # Unknown patient -> 404; bad status -> 422; missing lot -> 422.
    r = client.post("/immunisations", json={
        "patient": "ghost", "vaccine_code": "141", "lot_number": "L1",
        "date": "2026-10-18T10:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 404
    r = client.post("/immunisations", json={
        "patient": "pat-1", "vaccine_code": "141", "lot_number": "L1",
        "date": "2026-10-18T10:00:00Z", "status": "done"}, headers=CLINICIAN)
    assert r.status_code == 422
    r = client.post("/immunisations", json={
        "patient": "pat-1", "vaccine_code": "141",
        "date": "2026-10-18T10:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 422
    # Viewer may read but not capture (imm.work).
    r = client.post("/immunisations", json={
        "patient": "pat-1", "vaccine_code": "141", "lot_number": "L1",
        "date": "2026-10-18T10:00:00Z"}, headers=VIEWER)
    assert r.status_code == 403

    created = _immunise(client, "pat-1")
    assert created["resourceType"] == "Immunization"
    assert created["status"] == "completed"
    assert created["vaccineCode"]["coding"][0]["code"] == "141"
    assert created["vaccineCode"]["coding"][0]["system"] == \
        "http://hl7.org/fhir/sid/cvx"
    assert created["patient"] == {"reference": "Patient/pat-1"}
    assert created["lotNumber"] == "LOT-42"
    assert created["occurrenceDateTime"] == "2026-10-18T15:30:00Z"

    r = client.get("/immunisations", params={"patient": "pat-1"},
                   headers=VIEWER)
    assert r.status_code == 200 and r.json()["total"] == 1
    assert r.json()["items"][0]["id"] == created["id"]
    r = client.get(f"/immunisations/{created['id']}", headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["id"] == created["id"]
    assert client.get("/immunisations/imm-nope",
                      headers=CLINICIAN).status_code == 404
    # The patient role has no staff imm.read.
    assert client.get("/immunisations", headers=PATIENT).status_code == 403


def test_portal_immunisations_own_record_only(client):
    _patient(client, "pat-1")
    _patient(client, "pat-2")
    mine = _immunise(client, "pat-1")
    _immunise(client, "pat-2", vaccine_code="03")
    r = client.get("/portal/immunisations", headers=PATIENT)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["patient_ref"] == "Patient/pat-1"
    assert body["total"] == 1
    assert body["items"][0]["id"] == mine["id"]
    # Staff roles do not use the patient portal view.
    assert client.get("/portal/immunisations",
                      headers=CLINICIAN).status_code == 403


def test_vxu_generation_queues_for_gateway(client):
    _patient(client, "pat-1")
    created = _immunise(client, "pat-1")
    r = client.post(f"/immunisations/{created['id']}/vxu", json={},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["registry"] == "synthetic-state-iis"
    # No enabled VXU-subscribed interop endpoint exists in the seed
    # config, so the honest outcome is the local queued-for-gateway
    # outbox — never a claimed send.
    assert item["queued_via"] == "local-outbox"
    assert item["status"] == "queued-for-gateway"
    raw = item["raw"]
    assert [s[0] for s in _segments(raw)] == ["MSH", "PID", "ORC", "RXA", "RXR"]
    assert "VXU^V04" in raw and "LOT-42" in raw
    assert hl7.message_type_of(hl7.parse_segments(raw)) == "VXU^V04"

    outbox = client.get("/immunisations/vxu-outbox", headers=CLINICIAN)
    assert outbox.status_code == 200
    assert outbox.json()["total"] == 1
    row = outbox.json()["items"][0]
    assert row["status"] == "queued-for-gateway"
    assert row["immunisation_id"] == created["id"]
    assert row["control_id"] == item["control_id"]
    audit = client.get("/studio/audit", headers=ADMIN).json()
    assert any(a["action"] == "immunisation.vxu.queue" for a in audit)
    assert any(a["action"] == "immunisation.create" for a in audit)
    # Unknown registry name and unknown record are honest errors.
    r = client.post(f"/immunisations/{created['id']}/vxu",
                    json={"registry": "nope"}, headers=CLINICIAN)
    assert r.status_code == 404
    assert client.post("/immunisations/imm-nope/vxu", json={},
                       headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# BD-29: costing over the real ETL -> warehouse path, exact totals
# ---------------------------------------------------------------------------

def _encounter(client, eid, pid, cls, start, end):
    r = client.post("/fhir/Encounter", json={
        "resourceType": "Encounter", "id": eid, "status": "finished",
        "class": {"system": ACTCODE, "code": cls},
        "subject": {"reference": f"Patient/{pid}"},
        "period": {"start": start, "end": end}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _observation(client, oid, pid, enc, code, value):
    r = client.post("/fhir/Observation", json={
        "resourceType": "Observation", "id": oid, "status": "final",
        "code": {"coding": [{"system": LOINC, "code": code}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{enc}"},
        "effectiveDateTime": "2026-01-02T08:00:00Z",
        "valueQuantity": {"value": value, "unit": "mg/dL"}},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _procedure(client, prid, pid, enc, code, display):
    r = client.post("/fhir/Procedure", json={
        "resourceType": "Procedure", "id": prid, "status": "completed",
        "code": {"coding": [{"system": CPT, "code": code,
                             "display": display}]},
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{enc}"},
        "performedDateTime": "2026-01-02T09:00:00Z"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_costing_service_lines_exact_totals(client, monkeypatch, tmp_path):
    wh_url = f"sqlite:///{tmp_path}/wh.db"
    monkeypatch.setenv("WAREHOUSE_DATABASE_URL", wh_url)
    _patient(client, "cost-1")
    # Inpatient visit: 2026-01-01T08:00Z -> 2026-01-04T08:00Z = 3 bed-days.
    _encounter(client, "enc-imp1", "cost-1", "IMP",
               "2026-01-01T08:00:00Z", "2026-01-04T08:00:00Z")
    _encounter(client, "enc-amb1", "cost-1", "AMB",
               "2026-01-02T09:00:00Z", "2026-01-02T10:00:00Z")
    _observation(client, "obs-imp1", "cost-1", "enc-imp1", "2345-7", 100)
    _observation(client, "obs-imp2", "cost-1", "enc-imp1", "2823-3", 4.2)
    _observation(client, "obs-amb1", "cost-1", "enc-amb1", "2345-7", 90)
    _procedure(client, "proc-img1", "cost-1", "enc-imp1", "71046",
               "Chest X-ray, 2 views")
    _procedure(client, "proc-ecg1", "cost-1", "enc-amb1", "93005",
               "Electrocardiogram")

    wh_factory = wh.warehouse_session_factory(wh_url)
    stats = etl.run_etl(client.test_app.state.session_factory, wh_factory)
    assert stats["dead_letter"] == 0, stats

    r = client.get("/costing/service-lines", headers=ADMIN)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["currency"] == "USD"
    lines = {entry["service_line"]: entry for entry in body["service_lines"]}
    # Hand-computed fixture math (seed rates: bed-day IMP $1200,
    # lab-test $28.50, imaging-study $240):
    #   inpatient  = 3*1200 + 2*28.50 + 1*240 = 3897.00
    #   ambulatory = 1*28.50 = 28.50 (ECG is an unpriced procedure)
    inpatient = lines["inpatient"]
    assert inpatient["visits"] == 1
    assert inpatient["activities"]["bed-day"] == {"count": 3, "cost": 3600.0}
    assert inpatient["activities"]["lab-test"] == {"count": 2, "cost": 57.0}
    assert inpatient["activities"]["imaging-study"] == {"count": 1,
                                                         "cost": 240.0}
    assert inpatient["activities"]["or-minute"] == {"count": 0, "cost": 0.0}
    assert inpatient["total_cost"] == 3897.0
    ambulatory = lines["ambulatory"]
    assert ambulatory["visits"] == 1
    assert ambulatory["activities"]["bed-day"] == {"count": 0, "cost": 0.0}
    assert ambulatory["activities"]["lab-test"] == {"count": 1, "cost": 28.5}
    assert ambulatory["unpriced_procedures"] == 1
    assert ambulatory["total_cost"] == 28.5
    assert body["total_cost"] == 3925.5
    assert any("OR-minute" in note for note in body["notes"])
    # The care-coordination episode table exists (coord_episodes) but
    # holds no episodes yet: honest zeroes/nulls, never estimates.
    assert body["episodes"]["available"] is True
    assert body["episodes"]["source"] == ["coord_episodes"]
    assert body["episodes"]["total"] == 0
    assert inpatient["episodes"] is None
    assert inpatient["cost_per_episode"] is None

    # An episode linked to the inpatient encounter attributes its
    # line's cost per episode through the episode's encounter link.
    from app.care_coordination import Episode
    with client.test_app.state.session_factory() as session:
        session.add(Episode(
            episode_id="ep-cost-1", patient_ref="Patient/cost-1",
            title="Synthetic costing episode", status="active",
            links=[{"ref": "Encounter/enc-imp1",
                    "reference": "Encounter/enc-imp1",
                    "type": "encounter"}],
            created_by="test"))
        session.commit()
    body = client.get("/costing/service-lines", headers=ADMIN).json()
    lines = {entry["service_line"]: entry for entry in body["service_lines"]}
    assert body["episodes"]["total"] == 1
    assert body["episodes"]["by_service_line"] == {"inpatient": 1}
    assert lines["inpatient"]["episodes"] == 1
    assert lines["inpatient"]["cost_per_episode"] == 3897.0
    assert lines["ambulatory"]["episodes"] is None
    assert lines["ambulatory"]["cost_per_episode"] is None


# ---------------------------------------------------------------------------
# BD-30: dashboards rewired to the indicator library
# ---------------------------------------------------------------------------

def test_dashboard_indicator_trace_and_values(client):
    r = client.get("/platform-depth/dashboard-indicators", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    trace = r.json()
    by_key = {d["key"]: d for d in trace["dashboards"]}
    for key in ("operations-overview", "audience-clinician-day",
                "audience-nurse-unit", "audience-unit-manager",
                "audience-department-head", "audience-executive",
                "audience-front-desk", "discharge-readiness", "unit-safety"):
        assert key in by_key, f"dashboard {key} missing from trace"
    # operations-overview: every widget resolves through an indicator.
    ops = by_key["operations-overview"]
    assert ops["widgets"]
    assert all(w["disposition"] == "indicator" and w["applied"]
               for w in ops["widgets"])
    # Every non-rewired widget, on every dashboard, carries a reason.
    for dash in trace["dashboards"]:
        for widget in dash["widgets"]:
            if widget["disposition"] == "live-endpoint":
                assert widget["reason"], (dash["key"], widget["id"])
    # Every indicator a widget resolves through is published and its
    # value endpoint returns a value or an honest unknown.
    indicator_keys = {w["indicator"] for d in trace["dashboards"]
                      for w in d["widgets"]
                      if w["disposition"] == "indicator"}
    assert {"operations-census", "operations-critical-open",
            "operations-verification-queue", "safety-open-events"} \
        <= indicator_keys
    for key in sorted(indicator_keys):
        definition = client.get(f"/indicators/definitions/{key}",
                                headers=ADMIN)
        assert definition.status_code == 200, key
        assert definition.json()["published"] is not None, key
        value = client.get(f"/indicators/{key}/value", headers=CLINICIAN)
        assert value.status_code == 200, (key, value.text)
        out = value.json()
        assert "status" in out
        assert out["value"] is not None or out["status"] == "unknown"
    # The published operations-overview config itself was republished
    # with widgets resolving through the indicator endpoints.
    with client.test_app.state.session_factory() as session:
        cfg = published_config(session, "dashboard", "operations-overview")
        assert cfg is not None and cfg.version >= 2
        for widget in cfg.payload["widgets"]:
            assert widget["indicator"]
            assert widget["endpoint"] == \
                f"/indicators/{widget['indicator']}/value"
            assert widget["sourceEndpoint"]


# ---------------------------------------------------------------------------
# BD-30 remainder (Phase 17, WS-RESID): the extended indicator library
# ---------------------------------------------------------------------------

# (dashboard, widget id) -> indicator key for every widget rewired by
# the Phase 17 remainder (21 widget instances, 18 bound governed
# indicator definitions — admissions/ready serve two dashboards
# each; two further definitions, workforce-coverage-pct and
# workforce-credentials-expiring, are published but deliberately NOT
# bound — see RESID_SCOPED below).
RESID_REWIRED = {
    ("audience-clinician-day", "admissions"): "workforce-admissions-today",
    ("audience-clinician-day", "ready"): "continuum-discharge-ready",
    ("audience-department-head", "staff"): "workforce-staff-active",
    ("audience-executive", "occupancy"): "workforce-occupancy-pct",
    ("audience-executive", "admissions"): "workforce-admissions-today",
    ("audience-executive", "discharges"): "workforce-discharges-today",
    ("audience-front-desk", "appointments"): "frontdesk-appointments-total",
    ("audience-front-desk", "awaiting-checkin"): "frontdesk-awaiting-checkin",
    ("audience-front-desk", "awaiting-room"): "frontdesk-awaiting-room",
    ("audience-front-desk", "waitlist"): "frontdesk-waitlist-open",
    ("audience-front-desk", "registrations"):
        "frontdesk-registrations-today",
    ("audience-nurse-unit", "ready"): "continuum-discharge-ready",
    ("discharge-readiness", "ready"): "continuum-discharge-ready",
    ("discharge-readiness", "awaiting"): "continuum-discharge-board-total",
    ("discharge-readiness", "boarding"): "continuum-boarding-total",
    ("radiology-operations", "worklist-total"): "radiology-worklist-total",
    ("radiology-operations", "tat-breaches"): "radiology-tat-breaches",
    ("radiology-operations", "unreported-stat"): "radiology-unreported-stat",
    ("radiology-operations", "critical-open"): "radiology-critical-total",
    ("unit-safety", "events"): "safety-total-events",
    ("unit-safety", "open-rca"): "safety-rca-open",
}

# The only widgets that may stay on live endpoints: multi-row tables
# and series charts, which no scalar indicator can honestly supply.
RESID_REASONED = {
    ("audience-clinician-day", "flow"),
    ("audience-department-head", "service-lines"),
    ("audience-department-head", "flow"),
    ("audience-department-head", "quality"),
    ("audience-executive", "census-trend"),
    ("audience-executive", "quality"),
    ("audience-executive", "service-lines"),
    ("audience-front-desk", "today-list"),
    ("audience-nurse-unit", "units"),
    ("audience-nurse-unit", "occupancy"),
    ("audience-unit-manager", "coverage-table"),
    ("audience-unit-manager", "census-trend"),
}

# Scalar widgets that stay on live endpoints for a different named
# reason: they are UNIT-SCOPED on the nurse / unit-manager home
# dashboards (app.people rewrites their /people/analytics endpoints
# with the caller's unit), and the governed indicators for their
# quantities are house-wide — rewiring would silently change the
# figure those audiences see from their unit's to the house's.
RESID_SCOPED = {
    ("audience-nurse-unit", "coverage"),
    ("audience-unit-manager", "coverage"),
    ("audience-unit-manager", "expiring"),
}


def test_dashboard_trace_complete_after_resid_rewiring(client):
    r = client.get("/platform-depth/dashboard-indicators", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    trace = r.json()
    # The trace covers every seeded widget (Phase 15 counted 41
    # across 9 dashboards; the radiology-operations dashboard seeded
    # by the same phase brings the seeded total to 45 across 10).
    assert trace["widgets_total"] >= 41
    assert trace["trace_complete"] is True
    assert trace["widgets_with_reasons"] == \
        trace["widgets_on_live_endpoints"]
    assert trace["widgets_on_indicators"] + \
        trace["widgets_on_live_endpoints"] == trace["widgets_total"]
    by_pair = {}
    for dash in trace["dashboards"]:
        for widget in dash["widgets"]:
            by_pair[(dash["key"], widget["id"])] = widget
    # Every Phase 17 rewiring is present and applied in the served
    # (published) dashboard config, not just traceable in principle.
    for pair, indicator_key in RESID_REWIRED.items():
        entry = by_pair[pair]
        assert entry["disposition"] == "indicator", pair
        assert entry["indicator"] == indicator_key, pair
        assert entry["applied"] is True, pair
        assert entry["endpoint"] == f"/indicators/{indicator_key}/value"
    # Every widget left on a live endpoint is one of the named
    # table/chart widgets or one of the named unit-scoped widgets,
    # and carries a specific reason in the trace.
    for pair in RESID_REASONED | RESID_SCOPED:
        entry = by_pair[pair]
        assert entry["disposition"] == "live-endpoint", pair
        assert entry["reason"], pair
    for pair in RESID_SCOPED:
        assert "Unit-scoped widget" in by_pair[pair]["reason"], pair
    live_pairs = {pair for pair, entry in by_pair.items()
                  if entry["disposition"] == "live-endpoint"}
    assert live_pairs == RESID_REASONED | RESID_SCOPED
    # Principled invariant: a live-endpoint widget is either a table
    # or a series chart (never a scalar an indicator could supply)
    # or one of the named unit-scoped widgets above.
    with client.test_app.state.session_factory() as session:
        for cfg in __import__("app.configstore", fromlist=["x"]) \
                .published_configs(session, "dashboard"):
            for widget in cfg.payload.get("widgets") or []:
                pair = (cfg.key, widget.get("id"))
                if pair in live_pairs - RESID_SCOPED:
                    assert widget.get("display") in ("table", "line",
                                                     "bar"), \
                        (pair, widget.get("display"))
    # Every extended indicator is published exactly once — including
    # the two house-wide indicators that are published but
    # deliberately not bound to their unit-scoped widgets.
    for key in sorted(set(RESID_REWIRED.values())) + [
            "workforce-coverage-pct", "workforce-credentials-expiring"]:
        definition = client.get(f"/indicators/definitions/{key}",
                                headers=ADMIN)
        assert definition.status_code == 200, key
        assert definition.json()["published"] is not None, key


def _resid_state(client):
    """Discriminating non-zero state across the rewired families, so
    value equality below cannot pass on two coincident zeroes."""
    from datetime import datetime, timezone
    _patient(client, "bd30-1")
    # Admission today: admissions_today + board total move.
    r = client.post("/inpatient/admissions",
                    json={"patient": "bd30-1", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # Serious-harm safety event: total + open + RCA-open move.
    r = client.post("/continuum/safety/events", json={
        "description": "Patient fall with serious injury (synthetic)",
        "category": "fall", "harm_level": "permanent",
        "unit": "unit-medsurg"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["rca_required"] is True
    # Clinic booking today: front-desk appointments + awaiting move.
    today = datetime.now(timezone.utc).date().isoformat()
    r = client.post("/continuum/clinic/book", json={
        "patient": "Patient/bd30-1", "session": "am-clinic",
        "date": today}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    # STAT radiology study: RIS worklist total + unreported STAT move.
    r = client.post("/fhir/ServiceRequest", json={
        "resourceType": "ServiceRequest", "id": "sr-bd30", "status": "active",
        "intent": "order", "priority": "stat",
        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt",
                             "code": "71046", "display": "Chest X-ray"}]},
        "subject": {"reference": "Patient/bd30-1"}}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    r = client.post("/radiology/studies",
                    json={"service_request_id": "sr-bd30", "modality": "CT"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_rewired_widget_values_equal_indicator_values(client):
    _resid_state(client)
    with client.test_app.state.session_factory() as session:
        from app.configstore import published_configs
        rewired = [(cfg.key, w) for cfg in
                   published_configs(session, "dashboard")
                   for w in cfg.payload.get("widgets") or []
                   if isinstance(w, dict) and w.get("indicator")]
    assert len(rewired) >= 30  # 9 Phase 15 + 21 Phase 17 instances
    seen_values = {}
    for dash_key, widget in rewired:
        indicator_key = widget["indicator"]
        value = client.get(f"/indicators/{indicator_key}/value",
                           headers=ADMIN)
        assert value.status_code == 200, (dash_key, widget["id"],
                                          value.text)
        indicator_value = value.json()["value"]
        source = client.get(widget["sourceEndpoint"].split("?")[0],
                            headers=ADMIN)
        assert source.status_code == 200, (dash_key, widget["id"],
                                           source.text)
        body = source.json()
        if widget.get("sourceDisplay") == "total" \
                and widget.get("sourceField") is None:
            if widget["sourceEndpoint"].split("?")[0] == "/inpatient/census":
                # The census endpoint carries no top-level `total`
                # (the dashboard renderer would have shown "—" for
                # the pre-rewire widget); its hospital total is
                # totals.occupied — exactly what the census
                # indicator's named source computes.
                expected = body["totals"]["occupied"]
            else:
                expected = body.get("total", len(body.get("items") or []))
        else:
            expected = body[widget.get("sourceField") or "value"]
        assert indicator_value == expected, (
            dash_key, widget["id"], indicator_key, indicator_value,
            expected)
        seen_values[indicator_key] = indicator_value
    # The state seeded above really discriminates: these indicators
    # are non-zero (a crossed binding could not pass unnoticed).
    for key in ("workforce-admissions-today",
                "continuum-discharge-board-total", "safety-total-events",
                "safety-rca-open", "frontdesk-appointments-total",
                "frontdesk-awaiting-checkin",
                "frontdesk-registrations-today",
                "radiology-worklist-total", "radiology-unreported-stat",
                "workforce-staff-active"):
        assert seen_values[key] not in (None, 0, 0.0), \
            (key, seen_values.get(key))
    # The two published-but-unbound house indicators (unit-scoped
    # widgets — see RESID_SCOPED) also equal their source endpoints'
    # house-wide figures exactly.
    overview = client.get("/people/analytics/overview",
                          headers=ADMIN).json()
    expiring = client.get("/indicators/workforce-credentials-expiring"
                          "/value", headers=ADMIN).json()
    assert expiring["value"] == overview["credentials_expiring"]
    assert expiring["value"] not in (None, 0, 0.0)
    coverage = client.get("/people/analytics/coverage",
                          headers=ADMIN).json()
    coverage_ind = client.get("/indicators/workforce-coverage-pct"
                              "/value", headers=ADMIN).json()
    assert coverage_ind["value"] == coverage["overall_pct"] == 90.0


def test_platform_depth_ensure_is_idempotent(client):
    with client.test_app.state.session_factory() as session:
        before_dash = published_config(
            session, "dashboard", "operations-overview").version
        ensure_platform_depth_defaults(session)
        ensure_platform_depth_defaults(session)
        after_dash = published_config(
            session, "dashboard", "operations-overview").version
        assert after_dash == before_dash
        for kind, key in (("terminology", "vaccines"),
                          ("terminology", "activity-costs"),
                          ("terminology", "immunisation-registries"),
                          ("indicator", "operations-census"),
                          ("indicator", "operations-critical-open"),
                          ("indicator", "operations-verification-queue"),
                          ("indicator", "safety-open-events")):
            versions = list_config_versions(session, kind, key)
            assert len(versions) == 1, (kind, key, len(versions))
            assert versions[0].status.value == "published"


# ---------------------------------------------------------------------------
# BD-32: designer dataset schema + parity proof
# ---------------------------------------------------------------------------

def _publish_definition(client, key, payload):
    r = client.post(f"/report-engine/definitions/{key}", json=payload,
                    headers=CONFIG)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/report-engine/definitions/{key}/versions/{version}/publish",
        headers=CONFIG)
    assert r.status_code == 200, r.text


def _seed_patients(client):
    for pid, gender in (("pat-a", "female"), ("pat-b", "female"),
                        ("pat-c", "male")):
        r = client.post("/fhir/Patient", json={
            "resourceType": "Patient", "id": pid,
            "name": [{"family": "Synth", "given": [pid]}],
            "gender": gender, "birthDate": "1980-04-02"}, headers=ADMIN)
        assert r.status_code == 201, r.text


def test_designer_dataset_schema(client):
    _seed_patients(client)
    r = client.get("/designer/datasets", headers=ADMIN)
    assert r.status_code == 200, r.text
    items = {i["key"]: i for i in r.json()["items"]}
    assert {"patients", "appointments", "orders", "lab-results",
            "billing-lines"} <= set(items)
    detail = client.get("/designer/datasets/patients", headers=ADMIN)
    assert detail.status_code == 200
    schema = detail.json()
    fields = {f["name"]: f["type"] for f in schema["fields"]}
    assert set(fields) == {"id", "name", "gender", "birth_date"}
    assert fields["id"] == "string" and fields["gender"] == "string"
    assert "eq" in schema["filter_ops"]
    assert "pdf" in schema["formats"]
    assert client.get("/designer/datasets/nope",
                      headers=ADMIN).status_code == 404


def test_designer_composed_definition_parity(client):
    _seed_patients(client)
    # Hand-authored definition (the pre-designer way).
    hand = {"title": "Parity report", "dataset": "patients",
            "fields": ["id", "name", "gender"],
            "filters": [{"field": "gender", "op": "eq", "value": "female"}],
            "grouping": ["gender"], "formats": ["json", "pdf"]}
    _publish_definition(client, "parity-hand", hand)
    # Designer-composed definition: the palette schema is the only
    # source used to pick the dataset + fields; composition (fields,
    # a filter, grouping) goes through the same engine API.
    schema = client.get("/designer/datasets/patients",
                        headers=ADMIN).json()
    palette_fields = {f["name"] for f in schema["fields"]}
    chosen = [f for f in ("id", "name", "gender") if f in palette_fields]
    assert chosen == ["id", "name", "gender"]
    composed = {"title": "Parity report", "dataset": schema["key"],
                "fields": chosen,
                "filters": [{"field": "gender", "op": "eq",
                             "value": "female"}],
                "grouping": ["gender"], "formats": ["json", "pdf"]}
    _publish_definition(client, "parity-designer", composed)

    hand_run = client.post("/report-engine/parity-hand/run", json={},
                           headers=ADMIN)
    designer_run = client.post("/report-engine/parity-designer/run",
                               json={}, headers=ADMIN)
    assert hand_run.status_code == 200 and designer_run.status_code == 200
    hand_body, designer_body = hand_run.json(), designer_run.json()
    for field in ("title", "dataset", "row_count", "fields", "rows",
                  "groups"):
        assert designer_body[field] == hand_body[field], field
    assert hand_body["row_count"] == 2

    # PDF artefacts: identical structure (page count) and near-equal
    # size — the only variance is the generated-at timestamp and the
    # reportlab document id, both fixed-shape.
    hand_pdf = client.post("/report-engine/parity-hand/run",
                           json={"format": "pdf"}, headers=ADMIN)
    designer_pdf = client.post("/report-engine/parity-designer/run",
                               json={"format": "pdf"}, headers=ADMIN)
    for resp in (hand_pdf, designer_pdf):
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert resp.content[:5] == b"%PDF-"
        assert resp.content.rstrip().endswith(b"%%EOF")
    def _pages(content):
        match = re.search(rb"/Count (\d+)", content)
        return int(match.group(1)) if match else None
    assert _pages(hand_pdf.content) == _pages(designer_pdf.content) == 1
    assert abs(len(hand_pdf.content) - len(designer_pdf.content)) <= 256
