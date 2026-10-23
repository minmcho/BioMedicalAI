"""Phase 17, WS-COMP — BD-55 breach operations.

A synthetic breach walked register -> governed 4-factor assessment ->
config-driven notification clocks -> notification -> evidence pack,
plus clock escalation to a Task, awareness-timestamp immutability,
and the GDPR 72-hour clock set for EU incidents.
"""
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER

FACTORS = {
    "factor1-nature-extent": "Laptop with discharge summaries for 12 patients",
    "factor2-nature-of-phi": "Names, MRNs and diagnoses — high sensitivity",
    "factor3-unauthorised-person": "Unknown finder; device not recovered",
    "factor4-mitigation": "Remote wipe confirmed; no evidence of access",
    "risk-level": "not-low",
    "conclusion": "Breach — probability of compromise is not low",
}


def _incident(client, headers=ADMIN, **overrides):
    body = {"title": "Lost laptop", "description": "Synthetic incident",
            "jurisdiction": "US", "affected_count": 12,
            "discovered_at": "2026-01-01T09:00:00Z",
            "awareness_at": "2026-01-01T12:00:00Z"}
    body.update(overrides)
    r = client.post("/breach/incidents", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_register_create_and_clock_math_us(client):
    inc = _incident(client)
    clocks = {c["kind"]: c for c in inc["clocks"]}
    assert set(clocks) == {"hipaa_individual", "hipaa_hhs"}
    # 60 days from AWARENESS (2026-01-01T12:00Z), not discovery.
    assert clocks["hipaa_individual"]["due_at"].startswith("2026-03-02T12:00")
    assert clocks["hipaa_hhs"]["due_at"].startswith("2026-03-02T12:00")
    assert clocks["hipaa_individual"]["state"] in ("pending", "due_soon", "breached")
    assert clocks["hipaa_individual"]["satisfied"] is False


def test_register_role_gates(client):
    assert client.post("/breach/incidents", headers=CLINICIAN,
                       json={"title": "x", "discovered_at": "2026-01-01T00:00:00Z"}
                       ).status_code == 403
    inc = _incident(client)
    assert client.get("/breach/incidents", headers=VIEWER).status_code == 200
    assert client.get(f"/breach/incidents/{inc['incident_id']}",
                      headers=CLINICIAN).status_code == 200


def test_awareness_and_discovery_are_immutable(client):
    inc = _incident(client)
    iid = inc["incident_id"]
    r = client.put(f"/breach/incidents/{iid}", headers=ADMIN,
                   json={"awareness_at": "2026-02-01T00:00:00Z"})
    assert r.status_code == 422, r.text
    r = client.put(f"/breach/incidents/{iid}", headers=ADMIN,
                   json={"discovered_at": "2025-12-01T00:00:00Z"})
    assert r.status_code == 422, r.text
    # A mutable field still edits, and re-stating the SAME awareness is fine.
    r = client.put(f"/breach/incidents/{iid}", headers=ADMIN,
                   json={"title": "Lost laptop (updated)",
                         "awareness_at": "2026-01-01T12:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Lost laptop (updated)"
    assert r.json()["awareness_at"].startswith("2026-01-01T12:00")


def test_assessment_requires_all_governed_form_items(client):
    inc = _incident(client)
    iid = inc["incident_id"]
    incomplete = dict(FACTORS)
    del incomplete["factor3-unauthorised-person"]
    r = client.post(f"/breach/incidents/{iid}/assessment", headers=CONFIG,
                    json={"factors": incomplete})
    assert r.status_code == 422, r.text
    assert "factor3-unauthorised-person" in r.text
    r = client.post(f"/breach/incidents/{iid}/assessment", headers=CONFIG,
                    json={"factors": FACTORS})
    assert r.status_code == 201, r.text
    out = r.json()
    assert out["form_key"] == "breach-4factor"
    assert out["form_version"] >= 1
    assert out["is_breach"] is True
    assert out["factors"]["factor1-nature-extent"] == FACTORS["factor1-nature-extent"]
    got = client.get(f"/breach/incidents/{iid}/assessment", headers=ADMIN)
    assert got.status_code == 200 and got.json()["id"] == out["id"]


def test_end_to_end_register_assessment_clock_notification_evidence(client):
    inc = _incident(client)
    iid = inc["incident_id"]
    assert client.post(f"/breach/incidents/{iid}/assessment", headers=ADMIN,
                       json={"factors": FACTORS}).status_code == 201
    # Wrong clock kind for this jurisdiction is refused.
    r = client.post(f"/breach/incidents/{iid}/notifications", headers=ADMIN,
                    json={"kind": "gdpr_supervisory_authority", "recipient": "SA"})
    assert r.status_code == 422, r.text
    r = client.post(f"/breach/incidents/{iid}/notifications", headers=ADMIN,
                    json={"kind": "hipaa_individual", "recipient": "Affected individuals",
                          "channel": "mail", "sent_at": "2026-02-15T10:00:00Z"})
    assert r.status_code == 201, r.text
    after = client.get(f"/breach/incidents/{iid}", headers=ADMIN).json()
    clocks = {c["kind"]: c for c in after["clocks"]}
    assert clocks["hipaa_individual"]["satisfied"] is True
    assert clocks["hipaa_individual"]["state"] == "satisfied"
    assert clocks["hipaa_hhs"]["satisfied"] is False
    pack = client.get(f"/breach/incidents/{iid}/evidence-pack", headers=ADMIN)
    assert pack.status_code == 200, pack.text
    pack = pack.json()
    assert pack["incident"]["incident_id"] == iid
    assert len(pack["assessments"]) == 1
    assert len(pack["notifications"]) == 1
    assert pack["notifications"][0]["kind"] == "hipaa_individual"
    actions = {e["action"] for e in pack["audit_extract"]}
    assert "breach.incident.create" in actions
    assert "breach.assessment.create" in actions
    assert "breach.notification.record" in actions
    assert pack["audit_extract_total"] >= 3


def test_gdpr_clock_set_for_eu_incident(client):
    inc = _incident(client, jurisdiction="EU",
                    discovered_at="2026-01-01T00:00:00Z",
                    awareness_at="2026-01-01T00:00:00Z")
    clocks = {c["kind"]: c for c in inc["clocks"]}
    assert set(clocks) == {"gdpr_supervisory_authority", "gdpr_individual"}
    # 72 hours from awareness.
    assert clocks["gdpr_supervisory_authority"]["due_at"].startswith("2026-01-04T00:00")


def test_phipa_clock_set(client):
    inc = _incident(client, jurisdiction="CA-ON",
                    discovered_at="2026-01-01T00:00:00Z",
                    awareness_at="2026-01-01T00:00:00Z")
    kinds = {c["kind"] for c in inc["clocks"]}
    assert kinds == {"phipa_commissioner", "phipa_individual"}


def test_breached_clock_escalates_to_task_once(client):
    inc = _incident(client, discovered_at="2025-01-01T00:00:00Z",
                    awareness_at="2025-01-01T00:00:00Z")
    iid = inc["incident_id"]
    assert {c["state"] for c in inc["clocks"]} == {"breached"}
    r = client.post(f"/breach/incidents/{iid}/check-clocks", headers=ADMIN)
    assert r.status_code == 200, r.text
    first = r.json()["escalations"]
    assert len(first) == 2 and all(e.get("task_id") for e in first)
    r = client.post(f"/breach/incidents/{iid}/check-clocks", headers=ADMIN)
    second = r.json()["escalations"]
    assert len(second) == 2 and all(e["task"] == "already-open" for e in second)
    # The escalation Tasks are real FHIR Tasks.
    tasks = client.get("/fhir/Task", headers=CLINICIAN).json()
    descriptions = [e["resource"].get("description") or ""
                    for e in tasks["entry"]]
    assert sum(1 for d in descriptions if iid in d) == 2
