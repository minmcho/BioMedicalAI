"""Medallion (bronze -> silver -> gold) transforms for synthetic FHIR
Observation data, in dependency-free Python.

Why this exists: the Teladoc JR21143 posting (Principal Data & AI Platform
Engineer, DataOps) names production Databricks/Snowflake as Min's one hard
gap. This module is the *logic core* of the lakehouse MVP: the same
transforms are expressed for Databricks/Spark/Delta in
``databricks_notebook.py``. The logic is developed and unit-tested here,
with no Spark and no cloud account, so the Nov 1 v0 freeze definition —
silver table + one gold table + one MLflow run + README — is a port of
proven code, not new code written against a deadline.

Data: synthetic HbA1c Observation-shaped dicts only. No real PHI.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

# LOINC 4548-4 = Hemoglobin A1c/Hemoglobin.total in Blood.
HBA1C_LOINC = "4548-4"

# OMOP concept for HbA1c measurement (LOINC-derived), documented in
# DataArchitecture/FHIR_OMOP_MAPPING_NOTES.md.
HBA1C_OMOP_CONCEPT_ID = 3004410

# ADA-aligned bands used for the gold aggregation. These are population
# reporting bands, not a diagnosis of any individual.
def hba1c_band(value_pct: float) -> str:
    if value_pct < 5.7:
        return "below_5_7"
    if value_pct < 6.5:
        return "5_7_to_6_4"
    return "6_5_and_above"


def bronze_ingest(raw_records: list[dict], ingested_at: str | None = None) -> list[dict]:
    """Bronze: land records as-is, plus ingestion metadata. No filtering —
    bronze is the auditable copy of what arrived."""
    ts = ingested_at or datetime.now(timezone.utc).isoformat()
    return [
        {**record, "_ingested_at": ts, "_source": record.get("_source", "synthetic-fhir")}
        for record in raw_records
    ]


def _is_valid_observation(record: dict) -> bool:
    if record.get("resourceType") != "Observation":
        return False
    if record.get("status") in ("entered-in-error", "cancelled"):
        return False
    coding = (record.get("code") or {}).get("coding") or []
    if not any(c.get("code") == HBA1C_LOINC for c in coding):
        return False
    value = (record.get("valueQuantity") or {}).get("value")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    if not (record.get("subject") or {}).get("reference"):
        return False
    return bool(record.get("effectiveDateTime"))


def silver_transform(bronze: list[dict]) -> tuple[list[dict], dict]:
    """Silver: validate, normalise to an OMOP-measurement-shaped row, and
    deduplicate on (patient, datetime, value) keeping the latest ingest.
    Returns (rows, quality_report)."""
    rejected = 0
    by_key: dict[tuple, dict] = {}
    for record in bronze:
        if not _is_valid_observation(record):
            rejected += 1
            continue
        value = float(record["valueQuantity"]["value"])
        row = {
            "person_ref": record["subject"]["reference"],
            "measurement_concept_id": HBA1C_OMOP_CONCEPT_ID,
            "measurement_datetime": record["effectiveDateTime"],
            "value_as_number": value,
            "unit": record["valueQuantity"].get("unit", "%"),
            "_ingested_at": record.get("_ingested_at"),
        }
        key = (row["person_ref"], row["measurement_datetime"], row["value_as_number"])
        existing = by_key.get(key)
        if existing is None or (row["_ingested_at"] or "") >= (existing["_ingested_at"] or ""):
            by_key[key] = row
    rows = sorted(by_key.values(), key=lambda r: (r["person_ref"], r["measurement_datetime"]))
    report = {
        "bronze_rows": len(bronze),
        "silver_rows": len(rows),
        "rejected_rows": rejected,
        "duplicate_rows": len(bronze) - rejected - len(rows),
    }
    return rows, report


def gold_patient_summary(silver: list[dict]) -> list[dict]:
    """Gold: one row per patient — latest HbA1c, its band, measurement
    count, and first/last measurement dates. Aggregate-shaped output a
    reviewer can check in minutes; no row-level clinical claims."""
    by_patient: dict[str, list[dict]] = defaultdict(list)
    for row in silver:
        by_patient[row["person_ref"]].append(row)
    summary = []
    for person_ref, rows in sorted(by_patient.items()):
        ordered = sorted(rows, key=lambda r: r["measurement_datetime"])
        latest = ordered[-1]
        summary.append(
            {
                "person_ref": person_ref,
                "measurement_count": len(ordered),
                "first_measurement_date": ordered[0]["measurement_datetime"],
                "latest_measurement_date": latest["measurement_datetime"],
                "latest_hba1c_pct": latest["value_as_number"],
                "latest_hba1c_band": hba1c_band(latest["value_as_number"]),
            }
        )
    return summary


def run_pipeline(raw_records: list[dict], ingested_at: str | None = None) -> dict:
    bronze = bronze_ingest(raw_records, ingested_at=ingested_at)
    silver, report = silver_transform(bronze)
    gold = gold_patient_summary(silver)
    return {"bronze": bronze, "silver": silver, "gold": gold, "quality": report}
