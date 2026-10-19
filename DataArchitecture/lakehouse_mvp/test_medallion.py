"""Tests for the lakehouse MVP medallion transforms.

Runs under both pytest and stdlib unittest:
    python3 -m unittest test_medallion -v
"""

import unittest

from medallion import gold_patient_summary, hba1c_band, run_pipeline, silver_transform


def obs(patient, dt, value, status="final", code="4548-4"):
    return {
        "resourceType": "Observation",
        "status": status,
        "code": {"coding": [{"system": "http://loinc.org", "code": code}]},
        "subject": {"reference": f"Patient/{patient}"},
        "effectiveDateTime": dt,
        "valueQuantity": {"value": value, "unit": "%"},
    }


SAMPLE = [
    obs("p1", "2026-01-10T09:00:00Z", 5.4),
    obs("p1", "2026-04-10T09:00:00Z", 5.9),
    obs("p1", "2026-04-10T09:00:00Z", 5.9),  # exact duplicate
    obs("p2", "2026-02-01T10:00:00Z", 7.2),
    obs("p2", "2026-03-01T10:00:00Z", 9.9, status="entered-in-error"),
    obs("p3", "2026-02-15T08:30:00Z", 6.1, code="2339-0"),  # not HbA1c
    {"resourceType": "Patient", "id": "p9"},  # not an Observation
]


class TestMedallion(unittest.TestCase):
    def test_pipeline_counts(self):
        result = run_pipeline(SAMPLE, ingested_at="2026-10-19T00:00:00Z")
        q = result["quality"]
        self.assertEqual(q["bronze_rows"], 7)
        self.assertEqual(q["rejected_rows"], 3)  # entered-in-error, wrong code, non-Observation
        self.assertEqual(q["duplicate_rows"], 1)
        self.assertEqual(q["silver_rows"], 3)

    def test_silver_is_omop_shaped(self):
        result = run_pipeline(SAMPLE, ingested_at="2026-10-19T00:00:00Z")
        row = result["silver"][0]
        self.assertEqual(row["measurement_concept_id"], 3004410)
        self.assertEqual(row["unit"], "%")
        self.assertIsInstance(row["value_as_number"], float)

    def test_gold_latest_and_band(self):
        result = run_pipeline(SAMPLE, ingested_at="2026-10-19T00:00:00Z")
        gold = {r["person_ref"]: r for r in result["gold"]}
        self.assertEqual(gold["Patient/p1"]["measurement_count"], 2)
        self.assertEqual(gold["Patient/p1"]["latest_hba1c_pct"], 5.9)
        self.assertEqual(gold["Patient/p1"]["latest_hba1c_band"], "5_7_to_6_4")
        self.assertEqual(gold["Patient/p2"]["latest_hba1c_band"], "6_5_and_above")

    def test_bands(self):
        self.assertEqual(hba1c_band(5.6), "below_5_7")
        self.assertEqual(hba1c_band(5.7), "5_7_to_6_4")
        self.assertEqual(hba1c_band(6.4), "5_7_to_6_4")
        self.assertEqual(hba1c_band(6.5), "6_5_and_above")

    def test_empty_input(self):
        silver, report = silver_transform([])
        self.assertEqual(silver, [])
        self.assertEqual(gold_patient_summary(silver), [])
        self.assertEqual(report["silver_rows"], 0)


if __name__ == "__main__":
    unittest.main()
