"""Ingest a Synthea-style FHIR R4 bundle into OMOP-shaped rows.

Deliberately narrow: Patient -> person, Encounter -> visit_occurrence,
Condition -> condition_occurrence, Observation -> measurement. This is the
same mapping the DataArchitecture lakehouse uses, so the enterprise
deployment and the Databricks proof share one mapping contract.
"""
from datetime import datetime


def _entries(bundle):
    return [e["resource"] for e in bundle.get("entry", [])]


def _year(date_str):
    return datetime.fromisoformat(date_str.replace("Z", "+00:00")).year


def to_omop(bundle):
    """Return {'person': [...], 'visit_occurrence': [...], ...} row dicts."""
    out = {"person": [], "visit_occurrence": [],
           "condition_occurrence": [], "measurement": []}
    for res in _entries(bundle):
        rtype = res.get("resourceType")
        if rtype == "Patient":
            out["person"].append({
                "person_id": res["id"],
                "gender": res.get("gender"),
                "year_of_birth": _year(res["birthDate"]) if res.get("birthDate") else None,
            })
        elif rtype == "Encounter":
            out["visit_occurrence"].append({
                "visit_occurrence_id": res["id"],
                "person_id": res["subject"]["reference"].split("/")[-1],
                "visit_start": res["period"]["start"],
            })
        elif rtype == "Condition":
            out["condition_occurrence"].append({
                "condition_occurrence_id": res["id"],
                "person_id": res["subject"]["reference"].split("/")[-1],
                "condition_name": res["code"]["coding"][0]["display"],
            })
        elif rtype == "Observation":
            value = res.get("valueQuantity", {})
            out["measurement"].append({
                "measurement_id": res["id"],
                "person_id": res["subject"]["reference"].split("/")[-1],
                "measurement_name": res["code"]["coding"][0]["display"],
                "value": value.get("value"),
                "unit": value.get("unit"),
            })
    return out
