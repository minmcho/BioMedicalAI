"""Phase 15 (G) — clinician Results Inbox (/inbox).

Everything is seeded via the real API surface: patients, Observations
and DiagnosticReports through the generic FHIR endpoints, admissions
through /inpatient/admissions, staff through the seeded directory
(dev-clinician = staff-001, unit-medsurg). Released = final status +
no lab-unreleased visibility tag (app.fhir's rule, reused — never
re-implemented here either).
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AuditLog
from app.results_inbox import ResultAcknowledgement
from sqlalchemy import select

from .conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

NOW = datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _verify_staff_for_privileges(client, staff_id):
    """Wave A (WF-B1) fixture: attending assignment now requires the
    attending's licence to be primary-source verified with the
    demanded exclusion kinds clear — record that PSV for a SEEDED
    staff member (licence found via the credentials list)."""
    today = datetime.now(timezone.utc).date().isoformat()
    items = client.get("/people/credentials", headers=ADMIN).json()["items"]
    held = [i for i in items if i["staff_id"] == staff_id
            and i["type"] == "medical-licence"]
    assert held, f"seeded staff {staff_id} has no medical licence"
    latest = max(held, key=lambda i: i["expires_on"])
    resp = client.post(
        f"/people/credentials/{latest['credential_id']}/verifications",
        json={"source_code": "state-licence-board",
              "method": "direct-source-query", "outcome": "verified",
              "verified_on": today, "evidence": "SYN-CONF-1"},
        headers=ADMIN)
    assert resp.status_code == 201, resp.text
    for kind in ("licence-board", "oig", "sam"):
        resp = client.post(
            f"/people/staff/{staff_id}/exclusion-checks",
            json={"kind": kind, "outcome": "clear",
                  "checked_on": today, "evidence": "SYN-EV-1"},
            headers=ADMIN)
        assert resp.status_code == 201, resp.text


@pytest.fixture()
def app_client(tmp_path):
    app = create_app(database_url=f"sqlite:///{tmp_path}/test.db")
    with TestClient(app) as c:
        yield c, app


def make_patient(client, pid, name="Synthetic Patient"):
    r = client.post("/fhir/Patient", headers=ADMIN, json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synthetic", "given": [name]}],
        "gender": "female", "birthDate": "1980-01-01"})
    assert r.status_code == 201, r.text
    return pid


def make_obs(client, oid, patient, code="2345-7", display="Glucose",
             value=100.0, unit="mg/dL", interp=None, when=None,
             unreleased=False):
    body = {
        "resourceType": "Observation", "id": oid, "status": "final",
        "code": {"coding": [{"system": "http://loinc.org", "code": code,
                             "display": display}], "text": display},
        "subject": {"reference": f"Patient/{patient}"},
        "effectiveDateTime": iso(when or NOW),
        "valueQuantity": {"value": value, "unit": unit},
        "referenceRange": [{"low": {"value": 70, "unit": unit},
                            "high": {"value": 99, "unit": unit}}],
    }
    if interp:
        body["interpretation"] = [{
            "coding": [{"system": "http://terminology.hl7.org/CodeSystem/"
                                   "v3-ObservationInterpretation",
                        "code": interp, "display": interp}], "text": interp}]
    if unreleased:
        body["meta"] = {"tag": [{"system": "urn:opencare:visibility",
                                 "code": "lab-unreleased"}]}
    r = client.post("/fhir/Observation", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return oid


def make_report(client, rid, patient, obs_ids=(), *, code="51990-0",
                display="Basic metabolic panel", when=None, status="final",
                category="LAB", critical=False, unreleased=False,
                conclusion=None, imaging=False):
    body = {
        "resourceType": "DiagnosticReport", "id": rid, "status": status,
        "category": [{"coding": [{
            "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
            "code": category, "display": category}]}],
        "code": {"coding": [{"system": "http://loinc.org", "code": code,
                             "display": display}], "text": display},
        "subject": {"reference": f"Patient/{patient}"},
        "effectiveDateTime": iso(when or NOW),
        "result": [{"reference": f"Observation/{o}"} for o in obs_ids],
    }
    if critical:
        body["extension"] = [{"url": "urn:opencare:extension/critical-result",
                              "valueBoolean": True}]
    if conclusion:
        body["conclusion"] = conclusion
    if imaging:
        body["imagingStudy"] = [{"reference": "ImagingStudy/study-x"}]
    if unreleased:
        body["meta"] = {"tag": [{"system": "urn:opencare:visibility",
                                 "code": "lab-unreleased"}]}
    r = client.post("/fhir/DiagnosticReport", headers=ADMIN, json=body)
    assert r.status_code == 201, r.text
    return rid


def inbox(client, headers=CLINICIAN, **params):
    r = client.get("/inbox/results", headers=headers, params=params)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Worklist: released-only visibility, kinds, flags, ordering, filters
# ---------------------------------------------------------------------------

def test_worklist_released_only_and_visibility(client):
    make_patient(client, "p1")
    make_obs(client, "o-norm", "p1", value=90, interp="N")
    make_report(client, "r-norm", "p1", ["o-norm"])
    # Unreleased lab draft: tagged AND preliminary — invisible.
    make_obs(client, "o-draft", "p1", value=500, interp="HH", unreleased=True)
    make_report(client, "r-draft", "p1", ["o-draft"], status="preliminary",
                critical=True, unreleased=True)
    # A final report still carrying the tag is invisible too.
    make_report(client, "r-tagged", "p1", [], status="final", unreleased=True)
    items = inbox(client)["items"]
    ids = [i["result_id"] for i in items]
    assert ids == ["r-norm"]
    assert items[0]["kind"] == "lab"
    assert items[0]["flag"] is None  # interpretation N is stored, not a flag
    assert items[0]["patient_ref"] == "Patient/p1"
    assert items[0]["acknowledged"] is False


def test_worklist_kinds_flags_and_ordering(client):
    make_patient(client, "p1")
    make_patient(client, "p2")
    old = NOW - timedelta(hours=5)
    # critical (newest), abnormal (older), plain imaging (middle time)
    make_obs(client, "o-crit", "p1", code="2823-3", display="Potassium",
             value=6.4, unit="mmol/L", interp="HH", when=NOW)
    make_report(client, "r-crit", "p1", ["o-crit"], when=NOW)
    make_obs(client, "o-abn", "p2", value=152, interp="H", when=old)
    make_report(client, "r-abn", "p2", ["o-abn"], when=old)
    make_report(client, "r-img", "p2", [], when=NOW - timedelta(hours=1),
                category="RAD", code="CT-HEAD", display="CT head",
                conclusion="No acute abnormality.", imaging=True)
    make_report(client, "r-path", "p1", [], when=NOW - timedelta(hours=2),
                category="SP", code="11529-5", display="Surgical pathology study",
                conclusion="Adenocarcinoma, margins negative.")
    items = inbox(client)["items"]
    assert [i["result_id"] for i in items] == ["r-crit", "r-abn", "r-img", "r-path"]
    by_id = {i["result_id"]: i for i in items}
    assert by_id["r-crit"]["flag"] == "critical"
    assert by_id["r-abn"]["flag"] == "abnormal"
    assert by_id["r-img"]["kind"] == "imaging"
    assert by_id["r-img"]["flag"] is None  # no flag stored — never invented
    assert by_id["r-path"]["kind"] == "pathology"
    assert by_id["r-path"]["flag"] is None
    # Filters
    assert [i["result_id"] for i in inbox(client, kind="imaging")["items"]] == ["r-img"]
    assert [i["result_id"] for i in inbox(client, kind="pathology")["items"]] == ["r-path"]
    assert [i["result_id"] for i in inbox(client, flag="critical")["items"]] == ["r-crit"]
    assert [i["result_id"] for i in inbox(client, flag="abnormal")["items"]] == ["r-abn"]
    assert {i["result_id"] for i in inbox(client, flag="none")["items"]} == {"r-img", "r-path"}
    assert [i["result_id"] for i in inbox(client, patient="p1")["items"]] == ["r-crit", "r-path"]
    # Critical extension alone (no observations) also derives critical.
    make_report(client, "r-crit-ext", "p2", [], critical=True)
    assert inbox(client, flag="critical")["total"] == 2


def test_worklist_gate_and_empty(client):
    # A fresh DB shows no results, and the page is fhir.inbox-gated.
    assert inbox(client)["total"] == 0
    r = client.get("/inbox/results", headers=VIEWER)
    assert r.status_code == 403
    r = client.get("/inbox/results", headers={"Authorization": "Bearer patient-token"})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Acknowledgement: idempotent, per-clinician, audited once
# ---------------------------------------------------------------------------

def test_acknowledge_idempotent_and_per_clinician(app_client):
    client, app = app_client
    make_patient(client, "p1")
    make_obs(client, "o1", "p1", interp="H", value=140)
    make_report(client, "r1", "p1", ["o1"])
    r = client.post("/inbox/results/lab/r1/acknowledge", headers=CLINICIAN,
                    json={"note": "seen"})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["already_acknowledged"] is False
    assert first["acknowledged_by"] == "dev-clinician"
    again = client.post("/inbox/results/lab/r1/acknowledge",
                        headers=CLINICIAN, json={"note": "again"}).json()
    assert again["already_acknowledged"] is True
    assert again["acknowledged_at"] == first["acknowledged_at"]
    assert again["note"] == "seen"  # the ORIGINAL record
    with app.state.session_factory() as session:
        rows = session.scalars(select(ResultAcknowledgement)).all()
        assert len(rows) == 1
        audits = session.scalars(select(AuditLog).where(
            AuditLog.action == "inbox.acknowledge")).all()
        assert len(audits) == 1
    # Worklist ack state is per-clinician.
    assert inbox(client, headers=CLINICIAN)["items"][0]["acknowledged"] is True
    assert inbox(client, headers=ADMIN)["items"][0]["acknowledged"] is False
    assert inbox(client, ack="unacknowledged")["total"] == 0
    assert inbox(client, headers=ADMIN, ack="unacknowledged")["total"] == 1
    assert inbox(client, ack="acknowledged")["total"] == 1


def test_acknowledge_unreleased_or_wrong_kind_404(client):
    make_patient(client, "p1")
    make_report(client, "r-draft", "p1", [], status="preliminary", unreleased=True)
    r = client.post("/inbox/results/lab/r-draft/acknowledge", headers=CLINICIAN, json={})
    assert r.status_code == 404
    make_report(client, "r-img", "p1", [], category="RAD", imaging=True)
    r = client.post("/inbox/results/lab/r-img/acknowledge", headers=CLINICIAN, json={})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Detail + trend
# ---------------------------------------------------------------------------

def test_detail_with_ranges_conclusion_and_trend(client):
    make_patient(client, "p1")
    t1, t2, t3 = (NOW - timedelta(days=30), NOW - timedelta(days=10), NOW)
    for oid, when, value in (("o-old", t1, 120), ("o-mid", t2, 110),
                             ("o-new", t3, 105)):
        make_obs(client, oid, "p1", value=value, interp="H", when=when)
        make_report(client, f"r-{oid}", "p1", [oid], when=when,
                    conclusion="Improving glucose control.")
    r = client.get("/inbox/results/lab/r-o-new", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    detail = r.json()
    assert detail["conclusion"] == "Improving glucose control."
    obs = detail["observations"][0]
    assert obs["valueQuantity"]["value"] == 105
    assert obs["referenceRange"][0]["low"]["value"] == 70
    assert obs["referenceRange"][0]["high"]["value"] == 99
    assert detail["patient_summary"]["id"] == "p1"
    assert detail["patient_summary"]["name"] == "Synthetic Patient Synthetic"
    trend = detail["trend"]
    assert [t["value"] for t in trend] == [120, 110, 105]  # oldest -> newest
    assert [t["code"] for t in trend] == ["2345-7"] * 3
    # Unreleased detail is a 404.
    make_report(client, "r-hidden", "p1", [], status="preliminary", unreleased=True)
    assert client.get("/inbox/results/lab/r-hidden",
                      headers=CLINICIAN).status_code == 404


# ---------------------------------------------------------------------------
# Actions: follow-up task, message patient, escalate
# ---------------------------------------------------------------------------

def test_actions_task_message_escalate(client):
    make_patient(client, "p1")
    make_obs(client, "o1", "p1", interp="HH", value=600)
    make_report(client, "r1", "p1", ["o1"], critical=True)
    # Follow-up task: a real FHIR Task for the patient.
    r = client.post("/inbox/results/lab/r1/follow-up-task", headers=CLINICIAN,
                    json={"description": "Repeat glucose in 2 hours"})
    assert r.status_code == 201, r.text
    task = r.json()["task"]
    assert task["resourceType"] == "Task"
    assert task["for"]["reference"] == "Patient/p1"
    assert task["description"] == "Repeat glucose in 2 hours"
    got = client.get(f"/fhir/Task/{task['id']}", headers=CLINICIAN)
    assert got.status_code == 200
    # Message patient: lands in the patient's engagement thread.
    r = client.post("/inbox/results/lab/r1/message-patient", headers=CLINICIAN,
                    json={"text": "Your glucose result needs a repeat test."})
    assert r.status_code == 201, r.text
    thread_id = r.json()["thread_id"]
    thread = client.get(f"/messages/threads/{thread_id}", headers=CLINICIAN)
    assert thread.status_code == 200
    texts = [m["text"] for m in thread.json()["messages"]]
    assert "Your glucose result needs a repeat test." in texts
    portal = client.get("/messages/inbox", headers=CLINICIAN,
                        params={"patient": "p1"})
    assert portal.json()["total"] >= 1
    # Escalate: Task to the configured audience, marked escalation.
    r = client.post("/inbox/results/lab/r1/escalate", headers=CLINICIAN,
                    json={"reason": "no response"})
    assert r.status_code == 201, r.text
    assert r.json()["audience"] == "charge-nurse"
    etask = r.json()["task"]
    assert etask["owner"]["display"] == "charge-nurse"
    urls = [e["url"] for e in etask.get("extension") or []]
    assert "urn:opencare:extension/escalation" in urls
    # Actions are fhir.inbox-gated too.
    r = client.post("/inbox/results/lab/r1/escalate", headers=VIEWER, json={})
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# SLA scan + seeded rule
# ---------------------------------------------------------------------------

def _critical(client, rid, patient, when, oid=None):
    oid = oid or f"o-{rid}"
    make_obs(client, oid, patient, code="2823-3", display="Potassium",
             value=6.4, unit="mmol/L", interp="HH", when=when)
    make_report(client, rid, patient, [oid], when=when)


def _tasks(client):
    r = client.get("/fhir/Task", headers=ADMIN)
    assert r.status_code == 200
    return [e["resource"] for e in r.json()["entry"]]


def test_sla_scan_escalates_only_overdue_unacknowledged(client):
    make_patient(client, "p1")
    make_patient(client, "p2")
    make_patient(client, "p3")
    _critical(client, "r-old", "p1", NOW - timedelta(hours=10))     # overdue
    _critical(client, "r-fresh", "p2", NOW - timedelta(hours=1))    # inside SLA (4h)
    _critical(client, "r-acked", "p3", NOW - timedelta(hours=10))   # overdue but acked
    client.post("/inbox/results/lab/r-acked/acknowledge", headers=CLINICIAN, json={})
    before = len(_tasks(client))
    r = client.post("/inbox/scan", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["sla_hours"] == 4
    assert [e["result_ref"] for e in out["escalated"]] == ["DiagnosticReport/r-old"]
    tasks = _tasks(client)
    assert len(tasks) == before + 1
    esc = [t for t in tasks if t.get("for", {}).get("reference") == "Patient/p1"
           and "SLA" in (t.get("description") or "")]
    assert len(esc) == 1
    assert esc[0]["owner"]["display"] == "charge-nurse"
    # Scan is idempotent: a second scan escalates nothing new.
    out2 = client.post("/inbox/scan", headers=CLINICIAN).json()
    assert out2["escalated_count"] == 0
    assert len(_tasks(client)) == before + 1
    # Acknowledging the overdue one after escalation does not un-create
    # history.


def test_sla_rule_is_published(client):
    r = client.get("/studio/config", headers=CONFIG, params={"kind": "rule"})
    assert r.status_code == 200, r.text
    by_key = {row["key"]: row for row in r.json()}
    rule = by_key["inbox-critical-sla-escalation"]
    assert rule["status"] == "published"
    payload = rule["payload"]
    assert payload["event"] == "inbox.critical.unacknowledged"
    assert payload["action"]["type"] == "create_task"
    assert payload["action"]["params"]["ownerRole"] == "charge-nurse"
    cfg = client.get("/studio/config", headers=CONFIG,
                     params={"kind": "integration"})
    inbox_cfg = {row["key"]: row for row in cfg.json()}["inbox"]
    assert inbox_cfg["status"] == "published"
    assert inbox_cfg["payload"]["slaHours"] == 4
    assert inbox_cfg["payload"]["pageSize"] == 50


# ---------------------------------------------------------------------------
# Personal scoping: mine (attending) + unit
# ---------------------------------------------------------------------------

def test_scope_mine_and_unit(client):
    make_patient(client, "p-mine")
    make_patient(client, "p-other")
    make_patient(client, "p-icu")
    for pid in ("p-mine", "p-other", "p-icu"):
        make_obs(client, f"o-{pid}", pid, interp="H", value=130)
        make_report(client, f"r-{pid}", pid, [f"o-{pid}"])
    # dev-clinician is staff-001 on unit-medsurg (seeded directory).
    # WF-B1: both attendings' licences must be PSV-verified first.
    _verify_staff_for_privileges(client, "staff-001")
    _verify_staff_for_privileges(client, "staff-002")
    a = client.post("/inpatient/admissions", headers=ADMIN, json={
        "patient": "p-mine", "bed": "MS-101A", "attending_staff_id": "staff-001"})
    assert a.status_code == 201, a.text
    b = client.post("/inpatient/admissions", headers=ADMIN, json={
        "patient": "p-other", "bed": "MS-101B", "attending_staff_id": "staff-002"})
    assert b.status_code == 201, b.text
    c = client.post("/inpatient/admissions", headers=ADMIN, json={
        "patient": "p-icu", "bed": "ICU-201", "attending_staff_id": "staff-002"})
    assert c.status_code == 201, c.text
    mine = inbox(client, scope="mine")["items"]
    assert {i["result_id"] for i in mine} == {"r-p-mine"}
    unit = inbox(client, scope="unit")["items"]
    # unit-medsurg holds p-mine + p-other (both med-surg beds), not ICU.
    assert {i["result_id"] for i in unit} == {"r-p-mine", "r-p-other"}
    assert inbox(client)["total"] == 3  # unscoped still sees all
    r = client.get("/inbox/results", headers=CLINICIAN, params={"scope": "bogus"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Summary cross-link
# ---------------------------------------------------------------------------

def test_summary_cross_link(client):
    make_patient(client, "p1")
    _critical(client, "r-crit", "p1", NOW)
    make_obs(client, "o2", "p1", interp="H", value=130)
    make_report(client, "r-abn", "p1", ["o2"])
    client.post("/inbox/results/lab/r-abn/follow-up-task", headers=CLINICIAN,
                json={"description": "Call patient"})
    r = client.get("/inbox/summary", headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["results"]["unacknowledged"] == 2
    assert out["results"]["critical_unacknowledged"] == 1
    assert out["results"]["nav_hint"]["module"] == "command"
    assert out["continuum"]["open_tasks"] >= 1
    assert "breached" in out["continuum"]
    assert out["continuum"]["nav_hint"]["module"] == "inbox"
    client.post("/inbox/results/lab/r-crit/acknowledge", headers=CLINICIAN, json={})
    out2 = client.get("/inbox/summary", headers=CLINICIAN).json()
    assert out2["results"]["critical_unacknowledged"] == 0
    assert out2["results"]["unacknowledged"] == 1


# ---------------------------------------------------------------------------
# Living demo seed guard
# ---------------------------------------------------------------------------

def test_demo_seed_only_when_demo_patients_exist(app_client):
    client, app = app_client
    # Fresh DB: no demo patients -> no demo results.
    assert inbox(client)["total"] == 0


def test_demo_seed_with_demo_patients(tmp_path):
    from seed.seed import ClientTransport, load_bundle
    import json
    from pathlib import Path
    app = create_app(database_url=f"sqlite:///{tmp_path}/demo.db")
    with TestClient(app) as client:
        bundle = json.loads(Path(__file__).resolve().parents[1]
                            .joinpath("seed/synthea_bundle.json").read_text())
        load_bundle(bundle, ClientTransport(client, ADMIN))
        # A restart re-runs ensure defaults against the populated store.
    app2 = create_app(database_url=f"sqlite:///{tmp_path}/demo.db")
    with TestClient(app2) as client:
        items = inbox(client)["items"]
        demo = [i for i in items if i["result_id"].startswith("inbox-demo-")]
        kinds = {i["result_id"]: i for i in demo}
        assert "inbox-demo-rep-crit" in kinds
        assert kinds["inbox-demo-rep-crit"]["flag"] == "critical"
        assert kinds["inbox-demo-rep-crit"]["acknowledged"] is False
        assert kinds["inbox-demo-rep-glu"]["flag"] == "abnormal"
        assert kinds["inbox-demo-rep-ct"]["kind"] == "imaging"
        # And re-running ensure (another restart) does not duplicate.
    app3 = create_app(database_url=f"sqlite:///{tmp_path}/demo.db")
    with TestClient(app3) as client:
        demo = [i for i in inbox(client, limit=200)["items"]
                if i["result_id"].startswith("inbox-demo-")]
        assert len(demo) == 4
