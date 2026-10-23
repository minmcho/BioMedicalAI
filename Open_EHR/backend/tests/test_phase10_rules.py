"""Phase 10, Track B — the rules / automation engine.

Rules are governed Studio config (kind=rule) binding a server-defined
EVENT + structured CONDITION to a server-defined ACTION. Two proofs of
generality ship seeded: the critical-value notification (the Phase 2
escalation re-expressed through the engine — the notification record
is now OPENED by a rule firing at release) and the census-threshold
alert (a capacity Task at >= 90% occupancy). Everything here is
synthetic data.
"""
from sqlalchemy import select

from app.models import AuditLog, LabCriticalNotification
from tests.conftest import ADMIN, CLINICIAN, CONFIG

BMP = "51990-0"
BMP_RESULTS = [
    {"code": "2345-7", "value": 95},
    {"code": "3094-0", "value": 12},
    {"code": "2160-0", "value": 1.0},
    {"code": "2951-2", "value": 130},
    {"code": "2823-3", "value": 6.4},   # potassium — critical high (HH)
]


def _audit(client):
    factory = client.app.state.session_factory
    with factory() as session:
        return [(row.action, row.payload) for row in
                session.scalars(select(AuditLog)).all()]


def _publish(client, kind, key, payload):
    r = client.post(f"/studio/config/{kind}/{key}", json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=ADMIN)
    assert r.status_code == 200, r.text
    return version


def _release_critical_report(client):
    client.post("/fhir/Patient",
                json={"resourceType": "Patient", "id": "pat-1",
                      "name": [{"family": "Synthetic", "given": ["Rule"]}]},
                headers=CLINICIAN)
    r = client.post("/lab/orders",
                    json={"code": BMP, "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    order_id = r.json()["id"]
    for target in ("collected", "received"):
        r = client.post(f"/lab/orders/{order_id}/transition",
                        json={"to_status": target}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": BMP_RESULTS}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    report_id = r.json()["report"]["id"]
    assert client.post(f"/lab/reports/{report_id}/verify",
                       headers=CLINICIAN).status_code == 200
    assert client.post(f"/lab/reports/{report_id}/release",
                       headers=CLINICIAN).status_code == 200
    return report_id


def test_rules_seeded_published_and_registry_visible(client):
    rules = client.get("/studio/config?kind=rule", headers=ADMIN).json()
    by_key = {r["key"]: r for r in rules}
    assert by_key["critical-value-notification"]["status"] == "published"
    assert by_key["census-threshold-alert"]["status"] == "published"
    # Phase 11 added the renal-dose-review rule on the new
    # pharmacy.dur.screened event (seeded published like the others).
    assert by_key["renal-dose-review"]["status"] == "published"
    registry = client.get("/rules/registry", headers=CLINICIAN).json()
    # Phase 13 added the workforce.credential.expiring event via the
    # published credential-expiry-alert rule (seeded published).
    # Phase 14 adds the device events (temperature excursion + vitals
    # breach) via the seeded published device rules.
    # Phase 15 adds the RFID zone-mismatch event, the radiology
    # critical-result overdue event, and the results-inbox critical
    # SLA event, each with a seeded published rule.
    assert set(registry["events"]) == {"lab.report.released",
                                       "inpatient.census.changed",
                                       "pharmacy.dur.screened",
                                       "workforce.credential.expiring",
                                       "device.temperature.excursion",
                                       "device.vitals.breach",
                                       "rfid.zone.mismatch",
                                       "radiology.critical.overdue",
                                       "inbox.critical.unacknowledged"}
    assert "create_task" in registry["actions"]


def test_rule_validation_rejects_unknown_event_action_and_shape(client):
    base = {"name": "x", "event": "lab.report.released",
            "condition": {"signal": "critical", "op": "eq", "value": True},
            "action": {"type": "audit_alert", "params": {"note": "n"}}}
    bad_event = {**base, "event": "patient.deleted"}
    assert client.post("/studio/config/rule/bad-event", json=bad_event,
                       headers=ADMIN).status_code == 422
    bad_action = {**base, "action": {"type": "run_code", "params": {}}}
    assert client.post("/studio/config/rule/bad-action", json=bad_action,
                       headers=ADMIN).status_code == 422
    bad_op = {**base, "condition": {"signal": "critical", "op": "regex",
                                    "value": ".*"}}
    assert client.post("/studio/config/rule/bad-op", json=bad_op,
                       headers=ADMIN).status_code == 422
    no_title = {**base, "action": {"type": "create_task", "params": {}}}
    assert client.post("/studio/config/rule/no-title", json=no_title,
                       headers=ADMIN).status_code == 422


def test_critical_release_opens_notification_via_rule(client):
    report_id = _release_critical_report(client)
    # The notification record EXISTS (state pending) because the rule
    # opened it at release — before Phase 10 it was only derived.
    factory = client.app.state.session_factory
    with factory() as session:
        record = session.scalars(select(LabCriticalNotification).where(
            LabCriticalNotification.report_id == report_id)).first()
        assert record is not None and record.state == "pending"
    # The firing is on the audit chain, naming rule + version.
    fired = [p for a, p in _audit(client) if a == "rules.fired"]
    assert any(p["rule"] == "critical-value-notification"
               and p["detail"].get("notification") == "opened"
               for p in fired)
    # Observable behaviour is unchanged: the critical worklist shows the
    # report pending, and the read-back notify flow completes as before.
    queue = client.get("/lab/critical-reports", headers=CLINICIAN).json()
    item = next(i for i in queue["items"] if i["report"]["id"] == report_id)
    assert item["notification"]["state"] == "pending"
    r = client.post(f"/lab/reports/{report_id}/notify",
                    json={"notified_to": "Dr Synthetic", "read_back": True},
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["notification"]["state"] == "notified"


def test_noncritical_release_does_not_fire(client):
    client.post("/fhir/Patient",
                json={"resourceType": "Patient", "id": "pat-1",
                      "name": [{"family": "Synthetic"}]}, headers=CLINICIAN)
    r = client.post("/lab/orders",
                    json={"code": BMP, "subject": {"reference": "Patient/pat-1"}},
                    headers=CLINICIAN)
    order_id = r.json()["id"]
    for target in ("collected", "received"):
        client.post(f"/lab/orders/{order_id}/transition",
                    json={"to_status": target}, headers=CLINICIAN)
    normal = [{"code": "2345-7", "value": 95}, {"code": "3094-0", "value": 12},
              {"code": "2160-0", "value": 1.0}, {"code": "2951-2", "value": 140},
              {"code": "2823-3", "value": 4.2}]
    r = client.post(f"/lab/orders/{order_id}/results",
                    json={"results": normal}, headers=CLINICIAN)
    report_id = r.json()["report"]["id"]
    client.post(f"/lab/reports/{report_id}/verify", headers=CLINICIAN)
    client.post(f"/lab/reports/{report_id}/release", headers=CLINICIAN)
    factory = client.app.state.session_factory
    with factory() as session:
        record = session.scalars(select(LabCriticalNotification).where(
            LabCriticalNotification.report_id == report_id)).first()
        assert record is None  # the rule correctly did not fire


def test_manual_evaluate_census_rule_creates_task(client):
    r = client.post("/rules/evaluate", headers=ADMIN, json={
        "event": "inpatient.census.changed",
        "signals": {"occupancy_pct": 95.0, "patient_ref": "Patient/pat-1"}})
    assert r.status_code == 200, r.text
    assert r.json()["fired"] == 1
    assert r.json()["firings"][0]["rule"] == "census-threshold-alert"
    tasks = client.get("/fhir/Task", headers=CLINICIAN).json()
    descriptions = [e["resource"].get("description")
                    for e in tasks.get("entry", [])]
    assert any("capacity review" in (d or "") for d in descriptions)
    # Below threshold: nothing fires.
    r = client.post("/rules/evaluate", headers=ADMIN, json={
        "event": "inpatient.census.changed", "signals": {"occupancy_pct": 40}})
    assert r.json()["fired"] == 0
    # Manual evaluation is a studio.manage surface.
    assert client.post("/rules/evaluate", headers=CLINICIAN, json={
        "event": "inpatient.census.changed",
        "signals": {"occupancy_pct": 99}}).status_code == 403


def test_custom_rule_fires_on_real_adt_event(client):
    """Generality: a site-authored rule (occupied >= 1 -> audit alert)
    fires on a real admission, with no code change."""
    _publish(client, "rule", "any-admission-alert", {
        "name": "Any admission alert",
        "event": "inpatient.census.changed",
        "condition": {"signal": "occupied", "op": "gte", "value": 1},
        "action": {"type": "audit_alert",
                   "params": {"note": "admission happened"}},
        "enabled": True})
    client.post("/fhir/Patient",
                json={"resourceType": "Patient", "id": "pat-adt",
                      "name": [{"family": "Synthetic"}]}, headers=CLINICIAN)
    r = client.post("/inpatient/admissions",
                    json={"patient": "Patient/pat-adt", "bed": "MS-101A"},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    alerts = [p for a, p in _audit(client) if a == "rules.alert"]
    assert any(p["note"] == "admission happened" for p in alerts)
    fired = [p for a, p in _audit(client) if a == "rules.fired"]
    assert any(p["rule"] == "any-admission-alert" for p in fired)


def test_disabled_rule_does_not_fire(client):
    _publish(client, "rule", "sleeping-rule", {
        "name": "Sleeping rule",
        "event": "inpatient.census.changed",
        "condition": {"signal": "occupied", "op": "gte", "value": 0},
        "action": {"type": "audit_alert", "params": {"note": "must not fire"}},
        "enabled": False})
    r = client.post("/rules/evaluate", headers=ADMIN, json={
        "event": "inpatient.census.changed", "signals": {"occupied": 5}})
    fired = [f for f in r.json()["firings"] if f["rule"] == "sleeping-rule"]
    assert fired == []
