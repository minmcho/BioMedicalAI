# Demo data — synthetic seed

`synthea_bundle.json` is a small, curated **fabricated** dataset in FHIR R4
transaction-Bundle format, written in the style of Synthea output: 6
synthetic patients (family names are `SyntheticNNN`, identifiers are
`SYN-*`, and every Patient carries a `urn:opencare:seed = synthetic` tag)
with conditions, medication requests, encounters, procedures, appointments,
diagnostic reports, and laboratory Observations with LOINC codes repeated
over time so lab trends and the results inbox have something real to show.

**This data is invented. It is not real PHI and must never be replaced
with real patient data in this repository.**

Load it (idempotent — existing resources are skipped):

```bash
# backend running locally first (see repo README), then:
cd backend
BASE_URL=http://127.0.0.1:8000 TOKEN=admin-token ../.venv/bin/python seed/seed.py
```
