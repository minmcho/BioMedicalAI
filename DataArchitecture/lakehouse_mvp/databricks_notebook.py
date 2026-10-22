# Databricks notebook source
# MAGIC %md
# MAGIC # Lakehouse MVP — FHIR HbA1c medallion pipeline (Databricks Free Edition)
# MAGIC
# MAGIC Port of the locally unit-tested logic in `medallion.py` to Spark +
# MAGIC Delta Lake, with one MLflow run logging the data-quality counts.
# MAGIC This file is *not executed in the local clone* — it runs on
# MAGIC Databricks. The local module is the tested source of truth for the
# MAGIC transform rules; keep the two in step.
# MAGIC
# MAGIC Free Edition limits (verified 2026-10-13): Delta Lake and MLflow
# MAGIC work; Unity Catalog and the model registry are limited, so this
# MAGIC notebook uses plain Delta paths under /tmp — no UC tables.
# MAGIC
# MAGIC Nov 1 v0 freeze definition: this notebook runs end-to-end, the
# MAGIC silver and gold Delta tables exist, and one MLflow run is logged.
# MAGIC That run's URL is what the Teladoc application links to.

# COMMAND ----------

import mlflow

RAW_PATH = "/tmp/opencare_lakehouse/raw_observations.json"
BRONZE_PATH = "/tmp/opencare_lakehouse/bronze_observations"
SILVER_PATH = "/tmp/opencare_lakehouse/silver_measurement"
GOLD_PATH = "/tmp/opencare_lakehouse/gold_patient_summary"

# COMMAND ----------

# Setup: create the synthetic raw sample at RAW_PATH if absent.
# (The repo also carries this exact sample as raw_observations.json; a real
# uploaded file at RAW_PATH still wins — this cell only fills the gap.)
import json, os
os.makedirs(os.path.dirname(RAW_PATH), exist_ok=True)
if not os.path.exists(RAW_PATH):
    sample = [
        {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "subject": {"reference": "Patient/p1"},
         "effectiveDateTime": "2026-01-10T09:00:00Z",
         "valueQuantity": {"value": 5.4, "unit": "%"}},
        {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "subject": {"reference": "Patient/p1"},
         "effectiveDateTime": "2026-04-10T09:00:00Z",
         "valueQuantity": {"value": 5.9, "unit": "%"}},
        {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "subject": {"reference": "Patient/p1"},
         "effectiveDateTime": "2026-04-10T09:00:00Z",
         "valueQuantity": {"value": 5.9, "unit": "%"}},
        {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "subject": {"reference": "Patient/p2"},
         "effectiveDateTime": "2026-02-01T10:00:00Z",
         "valueQuantity": {"value": 7.2, "unit": "%"}},
        {"resourceType": "Observation", "status": "entered-in-error",
         "code": {"coding": [{"system": "http://loinc.org", "code": "4548-4"}]},
         "subject": {"reference": "Patient/p2"},
         "effectiveDateTime": "2026-03-01T10:00:00Z",
         "valueQuantity": {"value": 9.9, "unit": "%"}},
        {"resourceType": "Observation", "status": "final",
         "code": {"coding": [{"system": "http://loinc.org", "code": "2339-0"}]},
         "subject": {"reference": "Patient/p3"},
         "effectiveDateTime": "2026-02-15T08:30:00Z",
         "valueQuantity": {"value": 6.1, "unit": "%"}},
        {"resourceType": "Patient", "id": "p9"},
    ]
    with open(RAW_PATH, "w") as f:
        json.dump(sample, f)
print("raw sample ready at", RAW_PATH)

# COMMAND ----------

# Bronze: land as-is plus ingestion metadata.
from pyspark.sql import functions as F

bronze = (
    spark.read.json(RAW_PATH)
    .withColumn("_ingested_at", F.current_timestamp())
    .withColumn("_source", F.lit("synthetic-fhir"))
)
bronze.write.format("delta").mode("overwrite").save(BRONZE_PATH)

# COMMAND ----------

# Silver: same rules as medallion.silver_transform —
# final/amended HbA1c (LOINC 4548-4) only, OMOP-shaped, deduplicated.
silver = (
    bronze.filter(F.col("resourceType") == "Observation")
    .filter(~F.col("status").isin("entered-in-error", "cancelled"))
    .filter(F.expr("exists(code.coding, c -> c.code = '4548-4')"))
    .select(
        F.col("subject.reference").alias("person_ref"),
        F.lit(3004410).alias("measurement_concept_id"),
        F.col("effectiveDateTime").alias("measurement_datetime"),
        F.col("valueQuantity.value").cast("double").alias("value_as_number"),
        F.coalesce(F.col("valueQuantity.unit"), F.lit("%")).alias("unit"),
        F.col("_ingested_at"),
    )
    .filter(F.col("value_as_number").isNotNull() & F.col("person_ref").isNotNull())
    .dropDuplicates(["person_ref", "measurement_datetime", "value_as_number"])
)
silver.write.format("delta").mode("overwrite").save(SILVER_PATH)

# COMMAND ----------

# Gold: one row per patient — latest HbA1c + band (bands as in medallion.py).
gold = (
    silver.withColumn(
        "_rn",
        F.row_number().over(
            __import__("pyspark.sql.window", fromlist=["Window"]).Window.partitionBy(
                "person_ref"
            ).orderBy(F.col("measurement_datetime").desc())
        ),
    )
    .filter(F.col("_rn") == 1)
    .drop("_rn")
    .withColumnRenamed("value_as_number", "latest_hba1c_pct")
    .withColumn(
        "latest_hba1c_band",
        F.when(F.col("latest_hba1c_pct") < 5.7, "below_5_7")
        .when(F.col("latest_hba1c_pct") < 6.5, "5_7_to_6_4")
        .otherwise("6_5_and_above"),
    )
)
gold.write.format("delta").mode("overwrite").save(GOLD_PATH)

# COMMAND ----------

# One MLflow run logging the quality counts — the freeze artifact.
with mlflow.start_run(run_name="lakehouse-mvp-v0"):
    mlflow.log_metric("bronze_rows", bronze.count())
    mlflow.log_metric("silver_rows", silver.count())
    mlflow.log_metric("gold_rows", gold.count())
    mlflow.set_tag("pipeline", "fhir-hba1c-medallion")
    mlflow.set_tag("data", "synthetic-only")
