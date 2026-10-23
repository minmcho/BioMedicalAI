"""Phase 16 (WS-F1, Scope G) — genomics: reported variants + PGx.

Variants arrive as REPORTED results (no variant-calling pipeline
exists or is implied); recording one writes a FHIR Observation
through the FHIR store. PGx rules are the published
terminology/pharmacogenomics EXAMPLE seed (CYP2C19 loss-of-function
-> clopidogrel flag; DPYD loss-of-function -> fluorouracil avoid).

NOTE (integration): the coordinator mounts app.genomics.router and
calls ensure_genomics_defaults centrally; these tests use the
standard conftest `client` fixture and pass at the post-integration
gate.
"""
from tests.conftest import ADMIN, CLINICIAN, VIEWER

CLOPIDOGREL = "309362"      # seeded EXAMPLE PGx rule drug (CYP2C19)
FLUOROURACIL = "4850"       # seeded EXAMPLE PGx rule drug (DPYD)
METFORMIN = "847232"        # formulary drug with NO PGx rule


def _patient(client, pid):
    r = client.post("/fhir/Patient", json={
        "resourceType": "Patient", "id": pid,
        "name": [{"family": "Synth", "given": ["Genomics"]}]},
        headers=CLINICIAN)
    assert r.status_code == 201, r.text
    return pid


def _variant(client, patient, **overrides):
    payload = {"patientRef": f"Patient/{patient}", "gene": "CYP2C19",
               "hgvsC": "c.681G>A", "hgvsP": "p.Pro227=",
               "zygosity": "het", "interpretation": "pathogenic",
               "sourceLab": "Synthetic Reference Laboratory"}
    payload.update(overrides)
    return client.post("/genomics/variants", json=payload,
                       headers=CLINICIAN)


# ---------------------------------------------------------------------------
# Variant write + read-back (FHIR Observation components)
# ---------------------------------------------------------------------------

def test_variant_write_and_readback_components(client):
    _patient(client, "gen-1")
    r = _variant(client, "gen-1")
    assert r.status_code == 201, r.text
    body = r.json()
    obs = body["observation"]
    assert obs["resourceType"] == "Observation"
    assert obs["status"] == "final"
    assert obs["subject"] == {"reference": "Patient/gen-1"}
    categories = [c["code"] for cat in obs["category"]
                  for c in cat["coding"]]
    assert "laboratory" in categories
    panel = [c["code"] for c in obs["code"]["coding"]]
    assert panel == ["81247-9"]  # genetic variant panel marker

    by_code = {}
    for comp in obs["component"]:
        code = comp["code"]["coding"][0]["code"]
        by_code[code] = comp
    # Gene component: value carries the gene symbol.
    gene_concept = by_code["48018-6"]["valueCodeableConcept"]
    assert gene_concept["text"] == "CYP2C19"
    assert gene_concept["coding"][0]["code"] == "CYP2C19"
    # HGVS components are valueStrings, verbatim.
    assert by_code["48004-6"]["valueString"] == "c.681G>A"
    assert by_code["48005-3"]["valueString"] == "p.Pro227="
    # Zygosity + interpretation ride as coded concepts.
    zyg = by_code["53034-5"]["valueCodeableConcept"]
    assert zyg["text"] == "het"
    interp = by_code["variant-interpretation"]["valueCodeableConcept"]
    assert interp["coding"][0]["code"] == "pathogenic"
    assert obs["performer"] == [
        {"display": "Synthetic Reference Laboratory"}]

    # The Observation really landed in the FHIR store.
    stored = client.get(f"/fhir/Observation/{obs['id']}",
                        headers=CLINICIAN)
    assert stored.status_code == 200, stored.text
    assert stored.json()["id"] == obs["id"]

    # Read-back through the genomics endpoint parses the components.
    r = client.get("/genomics/patients/gen-1/variants", headers=VIEWER)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["patient_ref"] == "Patient/gen-1"
    assert out["total"] == 1
    (variant,) = out["variants"]
    assert variant["gene"] == "CYP2C19"
    assert variant["hgvs_c"] == "c.681G>A"
    assert variant["hgvs_p"] == "p.Pro227="
    assert variant["zygosity"] == "het"
    assert variant["interpretation"] == "pathogenic"
    assert variant["source_lab"] == "Synthetic Reference Laboratory"
    assert variant["observation_id"] == obs["id"]


def test_variant_without_hgvs_p_or_lab(client):
    _patient(client, "gen-2")
    r = _variant(client, "gen-2", gene="BRCA1", hgvsC="c.68_69delAG",
                 hgvsP=None, zygosity="hom", interpretation="vus",
                 sourceLab=None)
    assert r.status_code == 201, r.text
    out = client.get("/genomics/patients/gen-2/variants",
                     headers=CLINICIAN).json()
    (variant,) = out["variants"]
    assert variant["hgvs_p"] is None
    assert variant["source_lab"] is None
    assert variant["zygosity"] == "hom"
    assert variant["interpretation"] == "vus"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_variant_invalid_zygosity_and_interpretation_422(client):
    _patient(client, "gen-3")
    r = _variant(client, "gen-3", zygosity="compound-het")
    assert r.status_code == 422, r.text
    assert "zygosity" in r.json()["detail"]
    r = _variant(client, "gen-3", interpretation="probably-bad")
    assert r.status_code == 422, r.text
    assert "interpretation" in r.json()["detail"]
    # Neither invalid write recorded anything.
    out = client.get("/genomics/patients/gen-3/variants",
                     headers=CLINICIAN).json()
    assert out["total"] == 0


def test_variant_unknown_patient_404(client):
    r = _variant(client, "no-such-patient")
    assert r.status_code == 404, r.text
    r = client.get("/genomics/patients/no-such-patient/variants",
                   headers=CLINICIAN)
    assert r.status_code == 404, r.text


def test_variant_write_requires_work_capability(client):
    _patient(client, "gen-4")
    payload = {"patientRef": "Patient/gen-4", "gene": "CYP2C19",
               "hgvsC": "c.681G>A", "zygosity": "het",
               "interpretation": "pathogenic"}
    r = client.post("/genomics/variants", json=payload, headers=VIEWER)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# PGx check: seeded EXAMPLE rules
# ---------------------------------------------------------------------------

def test_pgx_check_flags_seeded_match_and_passes_non_match(client):
    _patient(client, "gen-5")
    assert _variant(client, "gen-5").status_code == 201
    # CYP2C19 loss-of-function (pathogenic) x clopidogrel -> flag.
    r = client.post("/genomics/pgx-check", json={
        "patientRef": "Patient/gen-5",
        "medicationCodes": [CLOPIDOGREL]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["total"] == 1
    (finding,) = out["findings"]
    assert finding["gene"] == "CYP2C19"
    assert finding["variant"] == "c.681G>A"
    assert finding["drug"] == CLOPIDOGREL
    assert finding["action"] == "flag"
    assert "CYP2C19" in finding["message"]

    # A medication with no PGx rule -> no findings.
    r = client.post("/genomics/pgx-check", json={
        "patientRef": "Patient/gen-5",
        "medicationCodes": [METFORMIN]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    assert r.json()["findings"] == []
    assert r.json()["total"] == 0


def test_pgx_check_avoid_rule_and_benign_variant_no_match(client):
    _patient(client, "gen-6")
    assert _variant(client, "gen-6", gene="DPYD",
                    hgvsC="c.1905+1G>A", hgvsP=None,
                    interpretation="pathogenic").status_code == 201
    r = client.post("/genomics/pgx-check", json={
        "patientRef": "Patient/gen-6",
        "medicationCodes": [FLUOROURACIL]}, headers=CLINICIAN)
    assert r.status_code == 200, r.text
    (finding,) = r.json()["findings"]
    assert finding["gene"] == "DPYD"
    assert finding["action"] == "avoid"
    assert finding["drug"] == FLUOROURACIL

    # A benign CYP2C19 variant is NOT loss-of-function evidence: the
    # clopidogrel rule does not fire for this patient.
    _patient(client, "gen-7")
    assert _variant(client, "gen-7", interpretation="benign",
                    zygosity="hom").status_code == 201
    r = client.post("/genomics/pgx-check", json={
        "patientRef": "Patient/gen-7",
        "medicationCodes": [CLOPIDOGREL]}, headers=CLINICIAN)
    assert r.json()["findings"] == []


def test_pgx_check_unknown_patient_404(client):
    r = client.post("/genomics/pgx-check", json={
        "patientRef": "Patient/ghost",
        "medicationCodes": [CLOPIDOGREL]}, headers=CLINICIAN)
    assert r.status_code == 404, r.text


# ---------------------------------------------------------------------------
# Gene cohort: exactly the carriers, refs + count only
# ---------------------------------------------------------------------------

def test_cohort_returns_exactly_the_carriers(client):
    _patient(client, "gen-8")
    _patient(client, "gen-9")
    _patient(client, "gen-10")
    assert _variant(client, "gen-8").status_code == 201
    assert _variant(client, "gen-8", hgvsC="c.636G>A",
                    interpretation="vus").status_code == 201
    assert _variant(client, "gen-9").status_code == 201
    # gen-10 carries a different gene only.
    assert _variant(client, "gen-10", gene="DPYD",
                    hgvsC="c.1905+1G>A", hgvsP=None).status_code == 201

    r = client.get("/genomics/cohort", params={"gene": "CYP2C19"},
                   headers=CLINICIAN)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["gene"] == "CYP2C19"
    # gen-8 appears once despite two CYP2C19 variants.
    assert out["patient_refs"] == ["Patient/gen-8", "Patient/gen-9"]
    assert out["count"] == 2
    # Refs + count only: no variant detail rides the cohort response.
    assert set(out) == {"gene", "patient_refs", "count"}

    r = client.get("/genomics/cohort", params={"gene": "TPMT"},
                   headers=CLINICIAN)
    assert r.json()["patient_refs"] == []
    assert r.json()["count"] == 0
    r = client.get("/genomics/cohort", headers=CLINICIAN)
    assert r.status_code == 422, r.text


def test_seeded_pgx_config_is_published_example(client):
    rows = client.get(
        "/studio/config/terminology/effective/pharmacogenomics",
        headers=ADMIN)
    assert rows.status_code == 200, rows.text
    payload = rows.json()["payload"]
    assert "EXAMPLE" in payload["description"]
    rules = {(r["gene"], r["drugCode"]): r for r in payload["rules"]}
    assert rules[("CYP2C19", CLOPIDOGREL)]["action"] == "flag"
    assert rules[("DPYD", FLUOROURACIL)]["action"] == "avoid"
