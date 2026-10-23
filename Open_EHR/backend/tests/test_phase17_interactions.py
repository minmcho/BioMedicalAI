"""Phase 17 (BD-70, PHR-09) — drug–drug interaction checking as a
bring-your-own-licensed-source integration.

What is proven here, and ONLY here, against a TEST FIXTURE source:

- the seeded production config keeps interactionKnowledgeSource =
  None and the no-source status path is byte-identical to the
  Phase 11 integration point (explicit 'not checked', zero pairs,
  zero findings);
- a governed loader loads a deployer-supplied source named in the
  published pharmacy-ops config (name + file reference + format +
  licence attestation) into a queryable pair index, audits the load,
  and REFUSES sources that lack the licence attestation, declare an
  unsupported format, or are malformed — checking stays inert on
  refusal, with the refusal stated;
- pair-checking executes against the loaded source in BOTH clinical
  surfaces: pharmacy verification / DUR (queue item, DUR signals,
  verify response) and CPOE order placement (placement response);
  interacting pairs flag with severity + source attribution,
  non-interacting pairs pass, and 'checked, nothing found' is
  distinguishable from 'not checked'.

FIXTURE PROVENANCE — TEST FIXTURE — NOT CLINICAL DATA: the source
used by these tests is tests/fixtures/interactions_fixture.json, a
test artefact containing a small number of pairs drawn from
universally documented pharmacology for test purposes only, under a
test-only attestation ('TEST FIXTURE — NOT CLINICAL DATA'). It is
never seeded into the application and is never a knowledge base.
No licensed interaction source exists in this environment, so no
production evidence for BD-70 can exist here — see RESULTS.md.
"""
import json
from pathlib import Path

from .conftest import ADMIN, CLINICIAN

FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" \
    / "interactions_fixture.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text())
SOURCE_NAME = FIXTURE["sourceName"]
LICENCE_HOLDER = FIXTURE["licenceHolder"]

WARFARIN = "11289"
ASPIRIN = "1191"
SERTRALINE = "1048467"
APIXABAN = "1351886"
LISINOPRIL = "1049221"

NO_SOURCE_STATUS = {
    "status": "no-source", "configured": False, "source": None,
    "message": ("No drug-interaction knowledge source is "
                "configured (terminology/pharmacy-ops "
                "'interactionKnowledgeSource' is empty). "
                "Interaction checking is INERT: no pair is "
                "checked and no severity is asserted.")}


def _fixture_source(**overrides):
    source = {"name": SOURCE_NAME, "reference": str(FIXTURE_PATH),
              "format": FIXTURE["format"], "licenceHolder": LICENCE_HOLDER}
    source.update(overrides)
    return source


def _publish_ops_source(client, source):
    effective = client.get(
        "/studio/config/terminology/effective/pharmacy-ops",
        headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    payload["interactionKnowledgeSource"] = source
    # The Studio terminology validator requires a concepts list; the
    # seeded pharmacy-ops payload is seeded directly and carries
    # none, so a Studio publish carries this shim concept (ignored
    # by every pharmacy-ops reader).
    payload.setdefault("concepts", [{
        "code": "pharmacy-ops", "display": "Pharmacy operations",
        "active": True}])
    r = client.post("/studio/config/terminology/pharmacy-ops",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/pharmacy-ops/versions/{version}"
        "/publish", headers=ADMIN)
    assert r.status_code == 200, r.text


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Test", "given": ["P17"]}],
        "birthDate": "1985-03-04", "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _place(client, pid, code, display=None):
    r = client.post("/orders", json={
        "type": "medication", "code": code, "code_display": display,
        "patient": pid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return r.json()


def _activate(client, oid):
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text


def _active_order(client, pid, code, display=None):
    placed = _place(client, pid, code, display)
    oid = placed["order"]["id"]
    _activate(client, oid)
    return oid


def _audit_actions(client):
    return [e["action"] for e in
            client.get("/studio/audit?limit=200", headers=ADMIN).json()]


# ------------------------------------------------------------- no source

def test_no_source_status_is_byte_identical_and_inert(client):
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status == NO_SOURCE_STATUS
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": [WARFARIN, ASPIRIN]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "no-source"
    assert out["pairs_checked"] == 0
    assert out["findings"] == []
    effective = client.get(
        "/studio/config/terminology/effective/pharmacy-ops",
        headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    assert payload["interactionKnowledgeSource"] is None
    # CPOE without a source: inert, and the response says so.
    _patient(client, "p17-nosrc")
    placed = _place(client, "p17-nosrc", WARFARIN, "Warfarin (fixture)")
    assert placed["interaction_findings"] == []
    assert placed["interaction_checking"] == NO_SOURCE_STATUS


# ------------------------------------------------------- loader + check

def test_fixture_loads_and_pair_check_executes(client):
    _publish_ops_source(client, _fixture_source())
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "loaded"
    assert status["configured"] is True
    assert status["source"] == SOURCE_NAME
    assert status["licence_holder"] == LICENCE_HOLDER
    assert status["pairs_loaded"] == 3

    # The explicit loader act succeeds and is audited.
    r = client.post("/pharmacy/interactions/load", headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "loaded"
    assert "pharmacy.interactions.load" in _audit_actions(client)

    # A documented interacting pair flags with severity + attribution.
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": [WARFARIN, ASPIRIN, LISINOPRIL]},
        headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["status"] == "loaded"
    assert out["pairs_checked"] == 3
    (finding,) = out["findings"]
    assert {finding["drug_a"], finding["drug_b"]} == {WARFARIN, ASPIRIN}
    assert finding["severity"] == "major"
    assert finding["source"] == SOURCE_NAME
    assert finding["licence_holder"] == LICENCE_HOLDER
    assert "TEST FIXTURE" in finding["summary"]
    assert finding["mechanism"]

    # 'Checked, nothing found' is distinguishable from 'not checked'.
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": [WARFARIN, LISINOPRIL]}, headers=CLINICIAN)
    out = r.json()
    assert out["status"] == "loaded"
    assert out["pairs_checked"] == 1
    assert out["findings"] == []

    # Severity passes through from the source untouched.
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": ["36567", "clarithromycin-fixture"]},
        headers=CLINICIAN)
    (finding,) = r.json()["findings"]
    assert finding["severity"] == "contraindicated"
    assert finding["source"] == SOURCE_NAME


def test_unlicensed_source_is_refused(client):
    source = _fixture_source()
    del source["licenceHolder"]
    _publish_ops_source(client, source)
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "source-refused"
    assert status["configured"] is False
    assert "licenceHolder" in status["refusal_reason"]
    r = client.post("/pharmacy/interactions/check", json={
        "medication_codes": [WARFARIN, ASPIRIN]}, headers=CLINICIAN)
    out = r.json()
    assert out["status"] == "source-refused"
    assert out["pairs_checked"] == 0
    assert out["findings"] == []
    # The loader act refuses with 422 and audits the refusal.
    r = client.post("/pharmacy/interactions/load", headers=CLINICIAN)
    assert r.status_code == 422
    assert "licence" in r.json()["detail"].lower()
    assert "pharmacy.interactions.load_refused" in _audit_actions(client)


def test_bare_name_source_is_refused_no_attestation(client):
    _publish_ops_source(client, "Some Commercial Knowledge Base")
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "source-refused"
    assert status["configured"] is False


def test_malformed_and_missing_sources_are_refused(client, tmp_path):
    bad = tmp_path / "bad-source.json"
    bad.write_text("{ this is not json")
    _publish_ops_source(client, _fixture_source(reference=str(bad)))
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "source-refused"
    assert "not valid JSON" in status["refusal_reason"]

    _publish_ops_source(client, _fixture_source(
        reference=str(tmp_path / "does-not-exist.json")))
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "source-refused"
    assert "could not be read" in status["refusal_reason"]

    _publish_ops_source(client, _fixture_source(format="csv"))
    status = client.get("/pharmacy/interactions/status",
                        headers=CLINICIAN).json()
    assert status["status"] == "source-refused"
    assert "format" in status["refusal_reason"]


# ------------------------------------------------- verification surface

def test_verification_and_dur_surfaces_flag_interactions(client):
    _publish_ops_source(client, _fixture_source())
    _patient(client, "p17-vq")
    _active_order(client, "p17-vq", SERTRALINE,
                  "Sertraline 50 MG Oral Tablet")
    apixaban = _active_order(client, "p17-vq", APIXABAN,
                             "Apixaban 5 MG Oral Tablet")

    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == apixaban][0]
    (finding,) = item["interaction_findings"]
    assert {finding["drug_a"], finding["drug_b"]} == {SERTRALINE, APIXABAN}
    assert finding["severity"] == "major"
    assert finding["source"] == SOURCE_NAME
    assert finding["checked_against"] == SERTRALINE

    dur = client.get("/pharmacy/dur-queue", headers=CLINICIAN).json()
    dur_item = [i for i in dur["items"] if i["order_id"] == apixaban][0]
    interaction_signals = [s for s in dur_item["signals"]
                           if s["type"] == "interaction"]
    assert len(interaction_signals) == 1
    assert interaction_signals[0]["severity"] == "major"
    assert interaction_signals[0]["source"] == SOURCE_NAME
    assert dur["interaction_checking"]["status"] == "loaded"

    # Verification itself proceeds (findings flag; the pharmacist
    # disposes) and the findings ride the verify response.
    # Gap Wave A (PHARM-M1): apixaban is seeded high-alert, so the
    # pharmacist verifies past that hard stop with a governed
    # override — the interaction findings still only FLAG (BD-70).
    r = client.post(f"/pharmacy/verification/{apixaban}/verify",
                    json={"restriction_acknowledged": True,
                          "override_reason_code": "benefit-outweighs-risk",
                          "override_note": "Interaction findings reviewed; "
                                           "benefit outweighs risk."},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
    (finding,) = r.json()["interaction_findings"]
    assert finding["severity"] == "major"
    assert finding["source"] == SOURCE_NAME
    assert r.json()["interaction_checking"]["status"] == "loaded"


def test_verification_clean_when_no_interaction(client):
    _publish_ops_source(client, _fixture_source())
    _patient(client, "p17-clean")
    _active_order(client, "p17-clean", LISINOPRIL,
                  "Lisinopril 10 MG Oral Tablet")
    metformin = _active_order(client, "p17-clean", "847232",
                              "Metformin hydrochloride 500 MG Oral Tablet")
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    item = [i for i in queue["items"] if i["order_id"] == metformin][0]
    assert item["interaction_findings"] == []
    r = client.post(f"/pharmacy/verification/{metformin}/verify",
                    json={}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    assert r.json()["interaction_findings"] == []


# ---------------------------------------------------------- CPOE surface

def test_cpoe_placement_flags_interaction_and_passes_clean(client):
    _publish_ops_source(client, _fixture_source())
    _patient(client, "p17-cpoe")
    _active_order(client, "p17-cpoe", WARFARIN, "Warfarin (fixture)")

    placed = _place(client, "p17-cpoe", ASPIRIN, "Aspirin (fixture)")
    (finding,) = placed["interaction_findings"]
    assert {finding["drug_a"], finding["drug_b"]} == {WARFARIN, ASPIRIN}
    assert finding["severity"] == "major"
    assert finding["source"] == SOURCE_NAME
    assert placed["interaction_checking"]["status"] == "loaded"
    assert any(w.startswith("interaction:") for w in placed["warnings"])

    placed = _place(client, "p17-cpoe", LISINOPRIL,
                    "Lisinopril 10 MG Oral Tablet")
    assert placed["interaction_findings"] == []
    assert not any(w.startswith("interaction:")
                   for w in placed["warnings"])
