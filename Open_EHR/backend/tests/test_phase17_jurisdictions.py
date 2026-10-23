"""Phase 17, WS-COMP — BD-57/58/59 jurisdiction packs.

Part 2 (BD-57): disclosure refused without a Part 2 consent,
counselling notes segmented separately, proceedings use prohibited.
GDPR (BD-58): rights workflows — access in the configured window,
rectification by supplementary statement only, retention-bound
erasure refused-with-reason (the correct outcome), DPIA/RoPA from
the live inventory, packs seeded as DRAFTS.
PHIPA (BD-59): lock-box withhold blocks chart access, override
requires a reason and is logged + producible.
"""
from tests.conftest import ADMIN, CLINICIAN, CONFIG


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": [pid]}]}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return f"Patient/{pid}"


def _consent(client, ref, regime, kind, headers=CLINICIAN, **extra):
    body = {"patient_ref": ref, "regime": regime, "kind": kind}
    body.update(extra)
    r = client.post("/jurisdictions/consents", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Packs: seeded as governed DRAFTS, attestation external
# ---------------------------------------------------------------------------

def test_packs_seeded_as_drafts(client):
    r = client.get("/jurisdictions/packs", headers=ADMIN)
    assert r.status_code == 200, r.text
    items = {p["key"]: p for p in r.json()["items"]}
    assert set(items) == {"part2-42cfr", "gdpr-eu", "phipa-ontario"}
    for key, pack in items.items():
        assert pack["exists"] is True
        assert pack["published_version"] is None, key
        assert pack["status"] == "draft", key
        assert "WAIVER CANDIDATE" in pack["waiver_candidate"]
    assert items["gdpr-eu"]["payload"]["parameters"]["breachClockHours"] == 72
    assert items["gdpr-eu"]["payload"]["parameters"]["lawfulBasisArticle"] == "Art. 9(2)(h)"
    assert items["part2-42cfr"]["payload"]["parameters"]["consentModel"] \
        == "single-consent-2024-final-rule"
    # And they are real Studio configs, visible through governance.
    hist = client.get("/studio/config/regulatory_pack/gdpr-eu", headers=ADMIN)
    assert hist.status_code == 200, hist.text


# ---------------------------------------------------------------------------
# BD-57 — Part 2 disclosure gate
# ---------------------------------------------------------------------------

def test_part2_disclosure_requires_consent(client):
    ref = _patient(client, "p2-1")
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud", "purpose": "treatment"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is False
    assert any("Part 2 consent" in reason for reason in r.json()["reasons"])
    _consent(client, ref, "part2", "part2-consent")
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud", "purpose": "treatment"})
    assert r.json()["allowed"] is True


def test_part2_counselling_notes_segmented_separately(client):
    ref = _patient(client, "p2-2")
    _consent(client, ref, "part2", "part2-consent")
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud-counselling"})
    assert r.json()["allowed"] is False
    assert any("counselling" in reason for reason in r.json()["reasons"])
    _consent(client, ref, "part2", "part2-counselling-consent")
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud-counselling"})
    assert r.json()["allowed"] is True


def test_part2_proceedings_use_prohibited(client):
    ref = _patient(client, "p2-3")
    _consent(client, ref, "part2", "part2-consent")
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud",
                          "for_proceedings": True})
    assert r.json()["allowed"] is False
    assert any("proceedings" in reason for reason in r.json()["reasons"])
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud",
                          "for_proceedings": True, "court_order": True})
    assert r.json()["allowed"] is True


def test_part2_revoked_consent_no_longer_discloses(client):
    ref = _patient(client, "p2-4")
    directive = _consent(client, ref, "part2", "part2-consent")
    r = client.post(f"/jurisdictions/consents/{directive['directive_id']}/revoke",
                    headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    r = client.post("/jurisdictions/disclose", headers=CLINICIAN,
                    json={"patient_ref": ref, "segment": "sud"})
    assert r.json()["allowed"] is False


# ---------------------------------------------------------------------------
# BD-58 — GDPR rights workflows + artefacts
# ---------------------------------------------------------------------------

def _rights(client, ref, kind, headers=CLINICIAN):
    r = client.post("/jurisdictions/rights-requests", headers=headers,
                    json={"patient_ref": ref, "kind": kind})
    assert r.status_code == 201, r.text
    return r.json()


def test_gdpr_access_request_window_and_fulfilment(client):
    ref = _patient(client, "gd-1")
    req = _rights(client, ref, "access")
    assert req["status"] == "received"
    assert req["due_at"] is not None and req["overdue"] is False
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN, json={"outcome": "fulfilled"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "fulfilled"
    # Double disposition refused.
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN, json={"outcome": "fulfilled"})
    assert r.status_code == 409


def test_gdpr_rectification_only_by_supplementary_statement(client):
    ref = _patient(client, "gd-2")
    req = _rights(client, ref, "rectification")
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN, json={"outcome": "fulfilled"})
    assert r.status_code == 422, r.text
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN,
                    json={"outcome": "fulfilled",
                          "supplementary_statement": "Patient states the "
                          "recorded address is the previous address; current "
                          "address confirmed 2026-01-05."})
    assert r.status_code == 200, r.text
    assert "supplementary" not in r.json()["status"]
    assert r.json()["supplementary_statement"].startswith("Patient states")
    # The underlying record was NOT mutated.
    patient = client.get(f"/fhir/Patient/gd-2", headers=CLINICIAN).json()
    assert patient["name"][0]["family"] == "Synth"


def test_gdpr_erasure_retention_bound_refusal_is_the_outcome(client):
    ref = _patient(client, "gd-3")
    req = _rights(client, ref, "erasure")
    # Fulfilling erasure while retention binds is refused 409.
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN, json={"outcome": "fulfilled"})
    assert r.status_code == 409, r.text
    # The documented refusal is the correct, recorded outcome.
    r = client.post(f"/jurisdictions/rights-requests/{req['request_id']}/fulfil",
                    headers=CLINICIAN, json={"outcome": "refused"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "refused"
    assert "retention" in r.json()["refusal_reason"]


def test_gdpr_lawful_basis_directive_recorded(client):
    ref = _patient(client, "gd-4")
    d = _consent(client, ref, "gdpr", "gdpr-lawful-basis")
    assert d["basis"] == "Art. 9(2)(h)"
    listing = client.get(f"/jurisdictions/consents?patient_ref={ref}",
                         headers=CLINICIAN).json()
    assert listing["total"] == 1


def test_dpia_and_ropa_generated_from_live_inventory(client):
    _patient(client, "gd-5")
    dpia = client.get("/jurisdictions/dpia", headers=ADMIN)
    assert dpia.status_code == 200, dpia.text
    dpia = dpia.json()
    assert dpia["lawful_basis"]["article"] == "Art. 9(2)(h)"
    assert "Patient" in dpia["processing_scope"]["fhir_resource_types"]
    assert len(dpia["processing_scope"]["modules"]) >= 5
    ropa = client.get("/jurisdictions/ropa", headers=ADMIN).json()
    assert ropa["controller_processing"][0]["lawful_basis"] == "Art. 9(2)(h)"
    assert "Patient" in ropa["controller_processing"][0]["data_categories"]


# ---------------------------------------------------------------------------
# BD-59 — PHIPA lock-box
# ---------------------------------------------------------------------------

def test_phipa_circle_of_care_default_allows(client):
    ref = _patient(client, "ph-1")
    r = client.post("/jurisdictions/chart-access", headers=CLINICIAN,
                    json={"patient_ref": ref, "provider_ref": "Practitioner/dr-a"})
    assert r.status_code == 200 and r.json()["allowed"] is True
    assert r.json()["overridden"] is False


def test_phipa_lockbox_blocks_named_provider(client):
    ref = _patient(client, "ph-2")
    _consent(client, ref, "phipa", "phipa-withhold",
             provider_ref="Practitioner/dr-blocked")
    r = client.post("/jurisdictions/chart-access", headers=CLINICIAN,
                    json={"patient_ref": ref, "provider_ref": "Practitioner/dr-blocked"})
    assert r.status_code == 403, r.text
    # A different provider in the circle of care is unaffected.
    r = client.post("/jurisdictions/chart-access", headers=CLINICIAN,
                    json={"patient_ref": ref, "provider_ref": "Practitioner/dr-other"})
    assert r.status_code == 200 and r.json()["allowed"] is True


def test_phipa_override_requires_reason_and_is_producible(client):
    ref = _patient(client, "ph-3")
    _consent(client, ref, "phipa", "phipa-withhold", organisation="Org X")
    r = client.post("/jurisdictions/chart-access", headers=CLINICIAN,
                    json={"patient_ref": ref, "organisation": "Org X",
                          "override_reason": "Emergency treatment — patient "
                                             "unconscious, care cannot wait"})
    assert r.status_code == 200, r.text
    assert r.json()["allowed"] is True and r.json()["overridden"] is True
    report = client.get(f"/jurisdictions/overrides?patient_ref={ref}",
                        headers=ADMIN).json()
    assert report["total"] == 1
    assert report["items"][0]["reason"].startswith("Emergency treatment")
    assert report["items"][0]["actor"] == "dev-clinician"
