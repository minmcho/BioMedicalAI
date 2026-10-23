"""Phase 16 (coordinator wiring, WS-F1 follow-through) — pharmacogenomics
in the pharmacy DUR flow.

WS-F1 delivered app.genomics.evaluate_pgx; the coordinator wired it into
app.pharmacy_ops at integration:
  - the verification-queue item carries `pgx_findings` beside
    `monitoring_blockers` / `monitoring_warnings` (_queue_item);
  - verify_order REFUSES (422, naming the finding) when a finding's
    action is "avoid"; flag/reduce findings are advisory and do not
    block verification (the monitoring-warning pattern).

Rules come from the published terminology/pharmacogenomics config
(EXAMPLE rules seeded by app.genomics). Synthetic data only.
"""
from .conftest import ADMIN, CLINICIAN

FLUOROURACIL = "4850"    # DPYD rule: avoid
CLOPIDOGREL = "309362"   # CYP2C19 rule: flag


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Pgx", "given": ["Wiring"]}],
        "birthDate": "1975-05-05", "gender": "female"}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _variant(client, pid, **overrides):
    payload = {"patientRef": f"Patient/{pid}", "gene": "CYP2C19",
               "hgvsC": "c.681G>A", "hgvsP": "p.Pro227=",
               "zygosity": "het", "interpretation": "pathogenic",
               "sourceLab": "Synthetic Reference Laboratory"}
    payload.update(overrides)
    r = client.post("/genomics/variants", json=payload, headers=CLINICIAN)
    assert r.status_code == 201, r.text


def _signed_order(client, pid, code, display):
    r = client.post("/orders", json={
        "type": "medication", "code": code, "code_display": display,
        "patient": pid}, headers=CLINICIAN)
    assert r.status_code == 201, r.text
    oid = r.json()["order"]["id"]
    for to in ("signed", "active"):
        r = client.post(f"/orders/{oid}/transition",
                        json={"to_status": to}, headers=CLINICIAN)
        assert r.status_code == 200, r.text
    return oid


RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"


def _admit_to_formulary(client, code, display):
    """Publish a new formulary version listing `code` as formulary —
    the governed Studio path a site uses (the seeded EXAMPLE-rule
    drugs are not on the seeded formulary)."""
    effective = client.get(
        "/studio/config/terminology/effective/formulary",
        headers=ADMIN).json()
    payload = effective["payload"] if "payload" in effective else effective
    if not any(c.get("code") == code for c in payload["concepts"]):
        payload["concepts"].append({
            "code": code, "display": display, "active": True,
            "system": RXNORM, "stockTracked": False,
            "formularyStatus": "formulary"})
    r = client.post("/studio/config/terminology/formulary",
                    json=payload, headers=ADMIN)
    assert r.status_code == 201, r.text
    version = r.json()["version"]
    r = client.post(
        f"/studio/config/terminology/formulary/versions/{version}/publish",
        headers=ADMIN)
    assert r.status_code == 200, r.text


def _queue_item(client, oid):
    queue = client.get("/pharmacy/verification-queue",
                       headers=CLINICIAN).json()
    for item in queue["items"]:
        if item["order_id"] == oid:
            return item
    raise AssertionError(f"order {oid} not in verification queue")


def test_avoid_finding_blocks_verification_and_is_named(client):
    # No formulary admission on purpose: the PGx avoid refusal must
    # fire BEFORE (and therefore never be masked by) the formulary
    # refusal — see the precedence note in pharmacy_ops.verify_order.
    _patient(client, "pgx-avoid")
    _variant(client, "pgx-avoid", gene="DPYD", hgvsC="c.1905+1G>A",
             hgvsP=None)
    oid = _signed_order(client, "pgx-avoid", FLUOROURACIL,
                        "Fluorouracil 500 MG Injection")
    item = _queue_item(client, oid)
    assert "pgx_findings" in item
    (finding,) = [f for f in item["pgx_findings"] if f["gene"] == "DPYD"]
    assert finding["action"] == "avoid"
    r = client.post(f"/pharmacy/verification/{oid}/verify",
                    json={"restriction_acknowledged": True},
                    headers=CLINICIAN)
    assert r.status_code == 422, r.text
    assert "DPYD" in r.json()["detail"]
    assert "Pharmacogenomic" in r.json()["detail"]


def test_flag_finding_is_advisory_and_verification_proceeds(client):
    _patient(client, "pgx-flag")
    _variant(client, "pgx-flag")  # CYP2C19 pathogenic het (defaults)
    _admit_to_formulary(client, CLOPIDOGREL,
                        "Clopidogrel 75 MG Oral Tablet")
    oid = _signed_order(client, "pgx-flag", CLOPIDOGREL,
                        "Clopidogrel 75 MG Oral Tablet")
    item = _queue_item(client, oid)
    (finding,) = [f for f in item["pgx_findings"]
                  if f["gene"] == "CYP2C19"]
    assert finding["action"] == "flag"
    r = client.post(f"/pharmacy/verification/{oid}/verify",
                    json={"restriction_acknowledged": True},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text


def test_no_variants_means_no_findings_and_clean_verification(client):
    _patient(client, "pgx-none")
    _admit_to_formulary(client, FLUOROURACIL,
                        "Fluorouracil 500 MG Injection")
    oid = _signed_order(client, "pgx-none", FLUOROURACIL,
                        "Fluorouracil 500 MG Injection")
    item = _queue_item(client, oid)
    assert item["pgx_findings"] == []
    r = client.post(f"/pharmacy/verification/{oid}/verify",
                    json={"restriction_acknowledged": True},
                    headers=CLINICIAN)
    assert r.status_code == 201, r.text
