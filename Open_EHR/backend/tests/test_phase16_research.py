"""Phase 16, WS-F2 — research & R&D operations (Scope H), CTMS coverage
analysis + billing grids (BD-42), and 21 CFR Part 11 e-signatures
(BD-46). Synthetic data only.

Covers: signature binding + tamper refusal (409), manifestation
content, the Part 11 pack-parameter flip (meaning required only once
the pack is PUBLISHED), study lifecycle + the activation gate on
undetermined procedures, coverage determinations (incl. signed),
billing-grid separation with exact totals + release export, and the
GCP / GLP / Part 11 packs seeded as drafts.
"""
import hashlib

# Importing the modules guarantees their tables + the esignature
# snapshot registry are populated in this process regardless of mount
# order (the coordinator mounts both routers in app.main).
from app import esignature as _esignature  # noqa: F401
from app import research_ops as _research_ops  # noqa: F401
from tests.conftest import ADMIN, CLINICIAN, CONFIG, VIEWER


def _study(client, protocol="PROTO-001", title="Synthetic trial",
           headers=CLINICIAN):
    r = client.post("/research/studies", headers=headers, json={
        "protocolId": protocol, "title": title, "phase": "II"})
    assert r.status_code == 201, r.text
    return r.json()


def _procedure(client, study_id, code="PROC-A", headers=CLINICIAN):
    r = client.post(f"/research/studies/{study_id}/procedures",
                    headers=headers,
                    json={"procedureCode": code,
                          "description": f"Synthetic {code}"})
    assert r.status_code == 201, r.text
    return r.json()


def _sign(client, record_type, record_id, snapshot_hash, meaning="I approve",
          headers=CLINICIAN):
    body = {"recordType": record_type, "recordId": record_id,
            "snapshotHash": snapshot_hash}
    if meaning is not None:
        body["meaning"] = meaning
    return client.post("/esign/sign", headers=headers, json=body)


def _publish_pack(client, key, version=1):
    r = client.post(
        f"/studio/config/regulatory_pack/{key}/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# E-signature: binding, tamper, manifestation (BD-46)
# ---------------------------------------------------------------------------

def test_signature_binding_and_manifestation(client):
    study = _study(client)
    r = _sign(client, "ResearchStudy", study["id"], study["snapshot_hash"],
              meaning="Protocol approved for activation review")
    assert r.status_code == 201, r.text
    sig = r.json()
    assert sig["signer"] == "dev-clinician"
    assert sig["meaning"] == "Protocol approved for activation review"
    assert sig["snapshot_hash"] == study["snapshot_hash"]
    # binding_hash = sha256(snapshotHash + signer + meaning + timestamp)
    expected = hashlib.sha256(
        f"{sig['snapshot_hash']}{sig['signer']}{sig['meaning']}"
        f"{sig['signed_at']}".encode("utf-8")).hexdigest()
    assert sig["binding_hash"] == expected
    assert len(sig["binding_hash"]) == 64
    # Manifestation returns signer / meaning / timestamp / binding.
    m = client.get(f"/esign/manifest/ResearchStudy/{study['id']}",
                   headers=CLINICIAN)
    assert m.status_code == 200, m.text
    man = m.json()
    assert man["manifestation"]["signer"] == "dev-clinician"
    assert man["manifestation"]["meaning"] == \
        "Protocol approved for activation review"
    assert man["manifestation"]["timestamp"] == sig["signed_at"]
    assert man["manifestation"]["binding"] == sig["binding_hash"]
    assert man["manifest_fields"] == ["signer", "meaning", "timestamp"]


def test_sign_refused_when_record_changed_since_snapshot(client):
    study = _study(client, protocol="PROTO-TAMPER")
    stale_hash = study["snapshot_hash"]
    # Edit the record after the signer reviewed the snapshot.
    r = client.put(f"/research/studies/{study['id']}", headers=CLINICIAN,
                   json={"title": "Amended title after review"})
    assert r.status_code == 200, r.text
    assert r.json()["snapshot_hash"] != stale_hash
    r = _sign(client, "ResearchStudy", study["id"], stale_hash)
    assert r.status_code == 409, r.text
    # Signing the CURRENT snapshot works.
    fresh = client.get(f"/research/studies/{study['id']}",
                       headers=CLINICIAN).json()
    r = _sign(client, "ResearchStudy", study["id"], fresh["snapshot_hash"])
    assert r.status_code == 201, r.text


def test_sign_unknown_record_type_and_record(client):
    r = _sign(client, "NotARecord", "1", "ab" * 32)
    assert r.status_code == 422, r.text
    study = _study(client, protocol="PROTO-404")
    r = _sign(client, "ResearchStudy", "999999", study["snapshot_hash"])
    assert r.status_code == 404, r.text
    m = client.get("/esign/manifest/ResearchStudy/999999", headers=CLINICIAN)
    assert m.status_code == 404


def test_part11_meaning_required_only_when_pack_published(client):
    study = _study(client, protocol="PROTO-FLIP")
    # Pack is a DRAFT: its parameters bind nothing — signing without a
    # meaning succeeds.
    r = _sign(client, "ResearchStudy", study["id"], study["snapshot_hash"],
              meaning=None)
    assert r.status_code == 201, r.text
    # Publish the seeded draft: now requireMeaning drives the endpoint.
    _publish_pack(client, "cfr-part-11", 1)
    study2 = _study(client, protocol="PROTO-FLIP-2")
    r = _sign(client, "ResearchStudy", study2["id"],
              study2["snapshot_hash"], meaning=None)
    assert r.status_code == 422, r.text
    r = _sign(client, "ResearchStudy", study2["id"],
              study2["snapshot_hash"], meaning="Reviewed and approved")
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Study lifecycle, protocol approval, activation gate (BD-42)
# ---------------------------------------------------------------------------

def test_protocol_approval_requires_signature(client):
    study = _study(client, protocol="PROTO-APPR")
    # Approval without any signature -> 422.
    r = client.post(f"/research/studies/{study['id']}/approve",
                    headers=CLINICIAN, json={})
    assert r.status_code == 422, r.text
    # A signature id that does not exist -> 422.
    r = client.post(f"/research/studies/{study['id']}/approve",
                    headers=CLINICIAN, json={"signatureId": 424242})
    assert r.status_code == 422, r.text
    # Sign, then approve with the signature id.
    sig = _sign(client, "ResearchStudy", study["id"],
                study["snapshot_hash"]).json()
    r = client.post(f"/research/studies/{study['id']}/approve",
                    headers=CLINICIAN, json={"signatureId": sig["id"]})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True
    assert r.json()["approval_signature_id"] == sig["id"]
    assert r.json()["status"] == "draft"  # approval != activation


def test_protocol_approval_via_inline_esign_block(client):
    study = _study(client, protocol="PROTO-INLINE")
    r = client.post(f"/research/studies/{study['id']}/approve",
                    headers=CLINICIAN, json={"esign": {
                        "meaning": "PI approval",
                        "snapshotHash": study["snapshot_hash"]}})
    assert r.status_code == 200, r.text
    assert r.json()["approved"] is True
    # Inline block with a stale hash -> 409 (record changed since review).
    study2 = _study(client, protocol="PROTO-INLINE-2")
    r = client.post(f"/research/studies/{study2['id']}/approve",
                    headers=CLINICIAN, json={"esign": {
                        "meaning": "PI approval",
                        "snapshotHash": "00" * 32}})
    assert r.status_code == 409, r.text


def test_activation_gate_lists_undetermined_procedures(client):
    study = _study(client, protocol="PROTO-GATE")
    p1 = _procedure(client, study["id"], "PROC-A")
    p2 = _procedure(client, study["id"], "PROC-B")
    # Not approved yet -> activation refused.
    r = client.post(f"/research/studies/{study['id']}/activate",
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    sig = _sign(client, "ResearchStudy", study["id"],
                study["snapshot_hash"]).json()
    client.post(f"/research/studies/{study['id']}/approve",
                headers=CLINICIAN, json={"signatureId": sig["id"]})
    # Determine only the first procedure: the gate refuses and LISTS
    # the undetermined one.
    r = client.post(f"/research/procedures/{p1['id']}/determination",
                    headers=CLINICIAN, json={
                        "determination": "research",
                        "rationale": "Protocol-mandated, no clinical use"})
    assert r.status_code == 201, r.text
    r = client.post(f"/research/studies/{study['id']}/activate",
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    undet = detail["undetermined_procedures"]
    assert [u["procedure_id"] for u in undet] == [p2["id"]]
    assert undet[0]["procedure_code"] == "PROC-B"
    # Coverage view agrees.
    cov = client.get(f"/research/studies/{study['id']}/coverage",
                     headers=CLINICIAN).json()
    assert cov["coverage_complete"] is False
    assert [u["procedure_id"] for u in cov["undetermined"]] == [p2["id"]]
    # Determine the second (mixed), then activation succeeds.
    r = client.post(f"/research/procedures/{p2['id']}/determination",
                    headers=CLINICIAN, json={
                        "determination": "mixed",
                        "rationale": "Part research, part standard care"})
    assert r.status_code == 201, r.text
    r = client.post(f"/research/studies/{study['id']}/activate",
                    headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "active"
    r = client.post(f"/research/studies/{study['id']}/close",
                    headers=CLINICIAN)
    assert r.status_code == 200 and r.json()["status"] == "closed"


def test_coverage_determination_validation_and_signed(client):
    study = _study(client, protocol="PROTO-DET")
    proc = _procedure(client, study["id"], "PROC-X")
    # Bad determination value -> 422.
    r = client.post(f"/research/procedures/{proc['id']}/determination",
                    headers=CLINICIAN, json={"determination": "free"})
    assert r.status_code == 422, r.text
    # Signed determination: sign the procedure snapshot, attach the id.
    sig = _sign(client, "StudyProcedure", proc["id"], proc["snapshot_hash"],
                meaning="Coverage determination attested").json()
    r = client.post(f"/research/procedures/{proc['id']}/determination",
                    headers=CLINICIAN, json={
                        "determination": "clinical",
                        "rationale": "Standard of care regardless of trial",
                        "signatureId": sig["id"]})
    assert r.status_code == 201, r.text
    det = r.json()
    assert det["determination"] == "clinical"
    assert det["determined_by"] == "dev-clinician"
    assert det["signature_id"] == sig["id"]
    # A signature over a DIFFERENT record cannot be attached.
    other = _procedure(client, study["id"], "PROC-Y")
    r = client.post(f"/research/procedures/{other['id']}/determination",
                    headers=CLINICIAN, json={
                        "determination": "research", "rationale": "x",
                        "signatureId": sig["id"]})
    assert r.status_code == 422, r.text


def test_study_crud_and_capability_gates(client):
    study = _study(client, protocol="PROTO-CRUD")
    assert study["status"] == "draft"
    assert study["pi_username"] == "dev-clinician"
    # Duplicate protocol id -> 409.
    r = client.post("/research/studies", headers=CLINICIAN, json={
        "protocolId": "PROTO-CRUD", "title": "Duplicate"})
    assert r.status_code == 409, r.text
    # Missing title -> 422.
    r = client.post("/research/studies", headers=ADMIN, json={
        "protocolId": "PROTO-NOTITLE"})
    assert r.status_code == 422, r.text
    # Viewer may read but not write (research.work); patient is refused.
    lst = client.get("/research/studies", headers=VIEWER)
    assert lst.status_code == 200, lst.text
    r = client.post("/research/studies", headers=VIEWER, json={
        "protocolId": "PROTO-VIEW", "title": "Nope"})
    assert r.status_code == 403, r.text
    r = client.get("/research/studies", headers={
        "Authorization": "Bearer patient-token"})
    assert r.status_code == 403, r.text
    r = client.post("/esign/sign", headers=VIEWER, json={
        "recordType": "ResearchStudy", "recordId": study["id"],
        "snapshotHash": study["snapshot_hash"], "meaning": "x"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# Billing grid (BD-42): separation, exact totals, release export
# ---------------------------------------------------------------------------

def _grid_study(client):
    study = _study(client, protocol="PROTO-GRID")
    p1 = _procedure(client, study["id"], "LAB-1")
    p2 = _procedure(client, study["id"], "IMG-1")
    return study, p1, p2


def test_billing_grid_separation_exact_totals_and_release(client):
    study, p1, p2 = _grid_study(client)
    def line(proc, code, amount, billed):
        r = client.post(f"/research/studies/{study['id']}/grid-lines",
                        headers=CLINICIAN, json={
                            "procedureId": proc["id"], "chargeCode": code,
                            "amount": amount, "billedTo": billed})
        assert r.status_code == 201, r.text
        return r.json()
    line(p1, "85025", 100.00, "research")
    line(p1, "80053", 250.50, "research")
    line(p2, "71046", 75.25, "standard")
    grid = client.get(f"/research/studies/{study['id']}/billing-grid",
                      headers=CLINICIAN).json()
    assert grid["research_total"] == 350.50
    assert grid["standard_total"] == 75.25
    assert grid["grand_total"] == 425.75
    assert grid["research_count"] == 2 and grid["standard_count"] == 1
    # Release: tagged charge-line export payload with the same totals.
    rel = client.post(
        f"/research/studies/{study['id']}/billing-grid/release",
        headers=CLINICIAN)
    assert rel.status_code == 200, rel.text
    payload = rel.json()
    assert payload["protocol_id"] == "PROTO-GRID"
    assert payload["grand_total"] == 425.75
    assert payload["research_total"] == 350.50
    assert payload["standard_total"] == 75.25
    tags = sorted((l["charge_code"], l["tag"])
                  for l in payload["charge_lines"])
    assert tags == [("71046", "standard"), ("80053", "research"),
                    ("85025", "research")]
    assert all(l["source"] == "opencare-research-grid"
               for l in payload["charge_lines"])


def test_billing_grid_line_validation(client):
    study, p1, _ = _grid_study(client)
    other = _study(client, protocol="PROTO-GRID-OTHER")
    foreign = _procedure(client, other["id"], "FRN-1")
    # Procedure of another study -> 422.
    r = client.post(f"/research/studies/{study['id']}/grid-lines",
                    headers=CLINICIAN, json={
                        "procedureId": foreign["id"], "chargeCode": "85025",
                        "amount": 10.0, "billedTo": "research"})
    assert r.status_code == 422, r.text
    # Bad billedTo / negative amount -> 422.
    r = client.post(f"/research/studies/{study['id']}/grid-lines",
                    headers=CLINICIAN, json={
                        "procedureId": p1["id"], "chargeCode": "85025",
                        "amount": 10.0, "billedTo": "sponsor"})
    assert r.status_code == 422, r.text
    r = client.post(f"/research/studies/{study['id']}/grid-lines",
                    headers=CLINICIAN, json={
                        "procedureId": p1["id"], "chargeCode": "85025",
                        "amount": -5.0, "billedTo": "research"})
    assert r.status_code == 422, r.text
    # Releasing an empty grid -> 422.
    empty = _study(client, protocol="PROTO-GRID-EMPTY")
    r = client.post(f"/research/studies/{empty['id']}/billing-grid/release",
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------------------
# Regulatory packs: seeded as DRAFTS with real parameters
# ---------------------------------------------------------------------------

def test_regulatory_packs_seeded_as_drafts(client):
    packs = client.get("/studio/config?kind=regulatory_pack",
                       headers=ADMIN).json()
    by_key = {}
    for p in packs:
        by_key.setdefault(p["key"], []).append(p)
    for key in ("gcp-ich-e6", "glp-21cfr58-oecd", "cfr-part-11"):
        assert key in by_key, f"pack {key} not seeded"
        assert by_key[key][0]["status"] == "draft", \
            f"pack {key} must be seeded as a DRAFT, not published"
    gcp = by_key["gcp-ich-e6"][0]["payload"]["parameters"]
    assert gcp["monitoringVisitCadenceDays"] == 90
    assert "protocol" in gcp["essentialDocuments"]
    assert "informed-consent-form" in gcp["essentialDocuments"]
    glp = by_key["glp-21cfr58-oecd"][0]["payload"]["parameters"]
    assert glp["rawDataRetentionYears"] == 5
    assert glp["qaAuditCadenceDays"] == 180
    p11 = by_key["cfr-part-11"][0]["payload"]["parameters"]
    assert p11["requireMeaning"] is True
    assert p11["manifestFields"] == ["signer", "meaning", "timestamp"]
    # Nothing published yet: the effective lookup 404s for all three.
    for key in ("gcp-ich-e6", "glp-21cfr58-oecd", "cfr-part-11"):
        r = client.get(f"/studio/config/regulatory_pack/effective/{key}",
                       headers=CONFIG)
        assert r.status_code == 404, (key, r.text)
