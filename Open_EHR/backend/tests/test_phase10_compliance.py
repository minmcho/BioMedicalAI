"""Phase 10, Track J — compliance framework: requirements register,
regulatory packs as governed config, audit-linked evidence, and the
tracer self-audit. Synthetic data only.
"""
from tests.conftest import ADMIN, CLINICIAN, CONFIG


def _publish_existing(client, kind, key, version, headers=ADMIN):
    r = client.post(f"/studio/config/{kind}/{key}/versions/{version}/publish",
                    headers=headers)
    assert r.status_code == 200, r.text


def test_register_seeded_with_hipaa_and_onc(client):
    out = client.get("/compliance/requirements", headers=CLINICIAN).json()
    # Phase 11 extended the register with the pharmacy bodies
    # (DEA x2, USP x2, DSCSA x1): 10 -> 15 seeded entries. Phase 12
    # added six EMTALA / discharge-planning entries under new bodies,
    # so the total and the body set are floors/subsets now, matching
    # how the register is designed to grow phase by phase.
    assert out["total"] >= 15
    assert {"HIPAA", "ONC/Cures", "DEA", "USP", "DSCSA"} <= set(out["bodies"])
    citations = {i["citation"] for i in out["items"]}
    assert "45 CFR 164.312(b)" in citations            # audit controls
    assert "45 CFR 164.312(a)(2)(ii)" in citations     # break-the-glass
    assert "45 CFR 164.312(a)(2)(i)" in citations      # unique user id (Front Door)
    assert "45 CFR 170.315(g)(10)" in citations        # FHIR API
    assert "45 CFR 170.315(b)(10)" in citations        # EHI export
    hipaa = client.get("/compliance/requirements?body=HIPAA",
                       headers=ADMIN).json()
    # 6 through Phase 10; the Front Door pass added HIPAA
    # 164.312(a)(2)(i) unique user identification (account system).
    # Phase 17 WS-SEC added three more HIPAA entries (CMP-30 key
    # management, CMP-31 snooping monitoring, CMP-32 log redaction).
    assert hipaa["total"] == 10


def test_register_crud_is_governed(client):
    r = client.post("/compliance/requirements", headers=CLINICIAN, json={
        "body": "CLIA", "citation": "42 CFR 493.1105", "control": "x"})
    assert r.status_code == 403  # writes are studio.manage
    r = client.post("/compliance/requirements", headers=ADMIN, json={
        "body": "CLIA", "citation": "42 CFR 493.1105",
        "title": "Record retention", "control": "Retain test records 2 years",
        "implementation_ref": "lab module", "evidence_source": "audit:lab."})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    r = client.put(f"/compliance/requirements/{rid}", headers=ADMIN, json={
        "body": "CLIA", "citation": "42 CFR 493.1105",
        "title": "Record retention (updated)",
        "control": "Retain test records 2 years",
        "implementation_ref": "lab module", "evidence_source": "audit:lab."})
    assert r.status_code == 200
    assert r.json()["title"] == "Record retention (updated)"
    bad = client.post("/compliance/requirements", headers=ADMIN,
                      json={"body": "CLIA", "control": "x"})
    assert bad.status_code == 422  # citation is mandatory


def test_regulatory_packs_seeded_as_drafts(client):
    packs = client.get("/studio/config?kind=regulatory_pack",
                       headers=ADMIN).json()
    by_key = {p["key"]: p for p in packs}
    assert by_key["hipaa-baseline"]["status"] == "draft"
    assert by_key["onc-cures"]["status"] == "draft"
    params = by_key["hipaa-baseline"]["payload"]["parameters"]
    assert params["breachNotificationDays"] == 60
    assert params["justificationWindowHours"] == 48
    assert "45 CFR 164.312(b)" in by_key["hipaa-baseline"]["payload"]["requirements"]


def test_published_hipaa_pack_drives_break_glass_policy(client):
    # Before publication: built-in defaults, honestly labelled.
    policy = client.get("/privacy/break-glass/policy", headers=CONFIG).json()
    assert policy["duration_minutes"] == 60
    assert "built-in defaults" in policy["source"]
    _publish_existing(client, "regulatory_pack", "hipaa-baseline", 1)
    policy = client.get("/privacy/break-glass/policy", headers=CONFIG).json()
    assert policy["source"].startswith("regulatory_pack/hipaa-baseline@v1")
    # A site tightens the duration by publishing a new pack version —
    # configuration, not code.
    current = client.get("/studio/config/regulatory_pack/hipaa-baseline",
                         headers=ADMIN).json()
    payload = dict(current[-1]["payload"])
    payload["parameters"] = {**payload["parameters"],
                             "breakGlassDurationMinutes": 30,
                             "justificationWindowHours": 24}
    r = client.post("/studio/config/regulatory_pack/hipaa-baseline",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    _publish_existing(client, "regulatory_pack", "hipaa-baseline",
                      r.json()["version"])
    policy = client.get("/privacy/break-glass/policy", headers=CONFIG).json()
    assert policy["duration_minutes"] == 30
    assert policy["justification_window_hours"] == 24


def test_evidence_resolves_against_the_audit_chain(client):
    # Requirement 2 (seed order) is the break-the-glass entry whose
    # evidence source is audit:breakglass.open — generate an opening.
    client.post("/fhir/Patient",
                json={"resourceType": "Patient", "id": "pat-ev",
                      "name": [{"family": "Synthetic"}]}, headers=CLINICIAN)
    r = client.post("/privacy/break-glass", headers=CLINICIAN, json={
        "patient_ref": "Patient/pat-ev", "reason": "trauma activation"})
    assert r.status_code == 201, r.text
    items = client.get("/compliance/requirements", headers=ADMIN).json()["items"]
    bg_req = next(i for i in items if i["citation"] == "45 CFR 164.312(a)(2)(ii)")
    ev = client.get(f"/compliance/requirements/{bg_req['id']}/evidence",
                    headers=ADMIN).json()
    assert ev["kind"] == "audit" and ev["total"] >= 1
    assert ev["entries"][0]["action"] == "breakglass.open"
    # A config: source resolves to the published config object.
    mn_req = next(i for i in items if i["citation"] == "45 CFR 164.502(b)")
    ev = client.get(f"/compliance/requirements/{mn_req['id']}/evidence",
                    headers=ADMIN).json()
    assert ev["kind"] == "config"
    # A file: source is reported as a reference, never faked into data.
    tls_req = next(i for i in items if i["citation"] == "45 CFR 164.312(e)(1)")
    ev = client.get(f"/compliance/requirements/{tls_req['id']}/evidence",
                    headers=ADMIN).json()
    assert ev["kind"] == "reference" and "never faked" in ev["note"]
    # Evidence is governance data: clinicians cannot pull audit extracts.
    assert client.get(f"/compliance/requirements/{bg_req['id']}/evidence",
                      headers=CLINICIAN).status_code == 403


def test_tracer_walks_a_patient_and_checks_controls(client):
    client.post("/fhir/Patient",
                json={"resourceType": "Patient", "id": "pat-tr",
                      "name": [{"family": "Synthetic"}]}, headers=CLINICIAN)
    # Open break-glass and leave it unreviewed: the tracer must FAIL
    # that control while access-logging passes.
    r = client.post("/privacy/break-glass", headers=CLINICIAN, json={
        "patient_ref": "Patient/pat-tr", "reason": "code blue"})
    access_id = r.json()["access_id"]
    out = client.post("/compliance/tracer", headers=ADMIN,
                      json={"patient_ref": "Patient/pat-tr"}).json()
    by_control = {c["control"]: c["status"] for c in out["checks"]}
    assert by_control["chart-access-logged"] == "pass"
    assert by_control["break-glass-reviewed"] == "fail"
    assert by_control["critical-results-completed"] == "not-applicable"
    assert out["summary"]["fail"] == 1
    # Review it: the same tracer now passes the control.
    client.post(f"/privacy/review-queue/{access_id}/review",
                headers=CONFIG, json={"outcome": "justified"})
    out = client.post("/compliance/tracer", headers=ADMIN,
                      json={"patient_ref": "Patient/pat-tr"}).json()
    by_control = {c["control"]: c["status"] for c in out["checks"]}
    assert by_control["break-glass-reviewed"] == "pass"
    # Unknown patient -> 404; the tracer itself is studio.manage only.
    assert client.post("/compliance/tracer", headers=ADMIN,
                       json={"patient_ref": "Patient/nope"}).status_code == 404
    assert client.post("/compliance/tracer", headers=CLINICIAN,
                       json={"patient_ref": "Patient/pat-tr"}).status_code == 403
