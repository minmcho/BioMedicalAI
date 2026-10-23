"""Phase 17, BD-52 (CMP-31) — snooping / access-pattern analytics.

Seeded scenarios MUST be detected and open privacy-workqueue cases
(coworker/VIP/family access without a treatment relationship,
volume anomaly, after-hours pattern); the false-positive control
(ordinary own-unit access) must pass clean. Synthetic audit data
only — the analysis reads the hash-chained audit, never chart
content.
"""
import hashlib
from datetime import datetime, timezone

from sqlalchemy import select

from app.audit import GENESIS_HASH, _canonical
from app.models import (AuditLog, CareTeam, Empanelment, FhirResource,
                        PrivacyCase, StaffMember, TeamMembership,
                        TeamPatientAssignment)
from tests.conftest import ADMIN, CLINICIAN, CONFIG

NOW = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)  # Mon noon UTC
DAY = datetime(2026, 10, 19, 10, 0, tzinfo=timezone.utc)
NIGHT = datetime(2026, 10, 19, 2, 30, tzinfo=timezone.utc)


def _seed_event(session_factory, actor, action, entity, payload, at):
    with session_factory() as s:
        last = s.scalars(select(AuditLog)
                         .order_by(AuditLog.id.desc())).first()
        prev = last.entry_hash if last else GENESIS_HASH
        material = prev + actor + action + entity + _canonical(payload)
        entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
        s.add(AuditLog(actor=actor, action=action, entity=entity,
                       payload=payload, prev_hash=prev,
                       entry_hash=entry_hash, created_at=at))
        s.commit()


def _seed_patient(session_factory, pid, family, given, vip=False):
    body = {"resourceType": "Patient", "id": pid,
            "name": [{"family": family, "given": [given]}]}
    if vip:
        body["meta"] = {"tag": [{"system": "urn:opencare:flags",
                                 "code": "VIP"}]}
    with session_factory() as s:
        s.add(FhirResource(resource_type="Patient", resource_id=pid,
                           version=1, body=body,
                           patient_ref=f"Patient/{pid}"))
        s.commit()


def _seed_staff(session_factory, staff_id, username, full_name,
                unit="4 West"):
    with session_factory() as s:
        s.add(StaffMember(staff_id=staff_id, username=username,
                          full_name=full_name, job_role="clinician",
                          unit=unit, status="active"))
        s.commit()


def _touches(session_factory, actor, patient_ref, count, at):
    for i in range(count):
        _seed_event(session_factory, actor, "fhir.update", patient_ref,
                    {"resource_type": "Patient", "version": i + 2},
                    at)


def _run(client, headers=CONFIG):
    r = client.post("/privacy-monitor/run", headers=headers,
                    json={"now": "2026-10-19T12:00:00+00:00"})
    assert r.status_code == 200, r.text
    return r.json()


def _cases(client, actor=None, rule=None):
    items = client.get("/privacy-monitor/cases?status=open",
                       headers=CONFIG).json()["items"]
    if actor is not None:
        items = [i for i in items if i["actor"] == actor]
    if rule is not None:
        items = [i for i in items if i["rule"] == rule]
    return items


# ---------------------------------------------------------------------------

def test_vip_access_without_relationship_opens_case(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-snoop", "dr-snoop", "Sam Snoop")
    _seed_patient(f, "vip-1", "Celebrini", "Vera", vip=True)
    _touches(f, "dr-snoop", "Patient/vip-1", 2, DAY)
    run = _run(client)
    assert run["chain_valid"] is True
    cases = _cases(client, actor="dr-snoop", rule="vip_access")
    assert len(cases) == 1
    assert cases[0]["patient_ref"] == "Patient/vip-1"
    assert cases[0]["severity"] == "high"
    assert cases[0]["status"] == "open"
    assert "VIP" in cases[0]["evidence"]["signal"]
    assert cases[0]["evidence"]["touches"] == 2


def test_coworker_access_without_relationship_opens_case(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-snoop", "dr-snoop", "Sam Snoop")
    _seed_staff(f, "staff-nina", "nina-nurse", "Nina Nurse")
    _seed_patient(f, "cow-1", "Nurse", "Nina")
    _touches(f, "dr-snoop", "Patient/cow-1", 3, DAY)
    _run(client)
    cases = _cases(client, actor="dr-snoop", rule="coworker_access")
    assert len(cases) == 1
    assert cases[0]["patient_ref"] == "Patient/cow-1"
    assert "staff member" in cases[0]["evidence"]["signal"]


def test_family_access_without_relationship_opens_case(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-snoop", "dr-snoop", "Sam Snoop")
    _seed_patient(f, "fam-1", "Snoop", "Patricia")
    _touches(f, "dr-snoop", "Patient/fam-1", 2, DAY)
    _run(client)
    cases = _cases(client, actor="dr-snoop", rule="family_access")
    assert len(cases) == 1
    assert "surname" in cases[0]["evidence"]["signal"]


def test_volume_anomaly_opens_case_under_published_threshold(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-bulk", "dr-bulk", "Ben Bulk")
    # Publish a tighter governed threshold (BD-52: thresholds are data).
    draft = client.post("/studio/config/integration/privacy-monitoring",
                        headers=ADMIN, json={
                            "enabled": True, "lookbackHours": 720,
                            "rules": {
                                "vipAccess": {"enabled": False},
                                "coworkerAccess": {"enabled": False},
                                "familyAccess": {"enabled": False},
                                "volumeAnomaly": {"enabled": True,
                                                  "maxDistinctPatients": 5,
                                                  "windowHours": 24},
                                "afterHours": {"enabled": False}}})
    assert draft.status_code == 201, draft.text
    pub = client.post(
        "/studio/config/integration/privacy-monitoring/versions/"
        f"{draft.json()['version']}/publish", headers=ADMIN)
    assert pub.status_code == 200, pub.text
    for i in range(7):
        _seed_patient(f, f"bulk-{i}", f"Bulk{i}", "Pat")
        _touches(f, "dr-bulk", f"Patient/bulk-{i}", 1, DAY)
    _run(client)
    cases = _cases(client, actor="dr-bulk", rule="volume_anomaly")
    assert len(cases) == 1
    assert cases[0]["evidence"]["distinct_patients"] == 7
    assert cases[0]["evidence"]["threshold"] == 5


def test_after_hours_pattern_opens_case(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-night", "dr-night", "Ned Night")
    _seed_patient(f, "night-1", "Owl", "Olivia")
    _touches(f, "dr-night", "Patient/night-1", 4, NIGHT)
    _run(client)
    cases = _cases(client, actor="dr-night", rule="after_hours")
    assert len(cases) == 1
    assert cases[0]["evidence"]["events"] == 4
    assert cases[0]["evidence"]["threshold"] == 3


def test_ordinary_own_unit_access_passes_clean(client):
    """The false-positive control: empanelled PCP + team-assigned
    own-unit access in daytime must open NO case — not even for a
    VIP patient the accessor is responsible for."""
    f = client.app.state.session_factory
    _seed_staff(f, "staff-panel", "dr-panel", "Paula Panel", unit="4 West")
    with f() as s:
        s.add(CareTeam(team_id="team-4w", name="4 West Team",
                       team_type="unit", unit="4 West"))
        s.add(TeamMembership(membership_id="mem-1", team_id="team-4w",
                             staff_id="staff-panel",
                             role_in_team="attending", status="active",
                             created_by="dev-admin"))
        s.commit()
    _seed_patient(f, "own-1", "Owen", "Oscar")
    _seed_patient(f, "team-1", "Tess", "Tara")
    _seed_patient(f, "vip-own", "Vaughn", "Vic", vip=True)
    with f() as s:
        s.add(Empanelment(empanelment_id="emp-1",
                          patient_ref="Patient/own-1",
                          pcp_staff_id="staff-panel", team_id="team-4w",
                          status="active", assigned_by="dev-admin"))
        s.add(Empanelment(empanelment_id="emp-2",
                          patient_ref="Patient/vip-own",
                          pcp_staff_id="staff-panel", team_id="team-4w",
                          status="active", assigned_by="dev-admin"))
        s.add(TeamPatientAssignment(assignment_id="asg-1",
                                   patient_ref="Patient/team-1",
                                   team_id="team-4w", role="primary",
                                   state="active",
                                   requested_by="dev-admin"))
        s.commit()
    _touches(f, "dr-panel", "Patient/own-1", 5, DAY)
    _touches(f, "dr-panel", "Patient/team-1", 5, DAY)
    _touches(f, "dr-panel", "Patient/vip-own", 3, DAY)
    run = _run(client)
    assert run["chain_valid"] is True
    panel_cases = _cases(client, actor="dr-panel")
    assert panel_cases == []
    # And nobody else tripped either — the whole run is clean.
    all_open = client.get("/privacy-monitor/cases?status=open",
                          headers=CONFIG).json()["items"]
    assert all_open == []


def test_runs_are_idempotent_and_disposition_closes(client):
    f = client.app.state.session_factory
    _seed_staff(f, "staff-snoop", "dr-snoop", "Sam Snoop")
    _seed_patient(f, "vip-9", "Celebrini", "Vera", vip=True)
    _touches(f, "dr-snoop", "Patient/vip-9", 2, DAY)
    first = _run(client)
    assert first["cases_opened_count"] == 1
    second = _run(client)
    assert second["cases_opened_count"] == 0
    case_id = first["cases_opened"][0]["case_id"]
    r = client.post(f"/privacy-monitor/cases/{case_id}/disposition",
                    headers=CONFIG,
                    json={"outcome": "false_positive",
                          "note": "reviewed: emergency coverage, documented"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "closed"
    assert r.json()["disposition"] == "false_positive"
    # Dispositioning twice is refused.
    assert client.post(f"/privacy-monitor/cases/{case_id}/disposition",
                       headers=CONFIG,
                       json={"outcome": "dismissed"}).status_code == 409
    assert client.get("/privacy-monitor/cases?status=open",
                      headers=CONFIG).json()["items"] == []


def test_policy_endpoint_and_clinician_role_gate(client):
    pol = client.get("/privacy-monitor/policy", headers=CONFIG)
    assert pol.status_code == 200, pol.text
    pol = pol.json()
    assert pol["rules"]["volumeAnomaly"]["maxDistinctPatients"] == 20
    assert "privacy-monitoring@v1" in pol["source"]
    assert client.get("/privacy-monitor/policy",
                      headers=CLINICIAN).status_code == 403
    assert client.post("/privacy-monitor/run",
                       headers=CLINICIAN).status_code == 403
    # Clinician CAN see the break-glass queue link in the workqueue.
    queue = client.get("/privacy-monitor/cases?status=open",
                       headers=CONFIG).json()
    assert queue["break_glass_queue"] == "/privacy/review-queue"
