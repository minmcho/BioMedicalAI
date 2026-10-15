"""Synthetic seed bundle: loads through the real API, idempotently, and the
seeded lab series are queryable as trends. Synthetic data only."""
import importlib.util
import json
from pathlib import Path

from tests.conftest import ADMIN, CLINICIAN

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("opencare_seed", SEED_DIR / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle():
    return json.loads((SEED_DIR / "synthea_bundle.json").read_text())


def test_seed_loads_and_is_idempotent(client):
    seed = _load_seed_module()
    transport = seed.ClientTransport(client, ADMIN)
    bundle = _bundle()

    first = seed.load_bundle(bundle, transport)
    assert first["created"] == first["total"] == 86
    assert first["skipped"] == 0

    second = seed.load_bundle(bundle, transport)
    assert second["created"] == 0
    assert second["skipped"] == second["total"] == 86


def test_seeded_patients_and_lab_trends_are_queryable(client):
    seed = _load_seed_module()
    seed.load_bundle(_bundle(), seed.ClientTransport(client, ADMIN))

    r = client.get("/fhir/Patient", headers=CLINICIAN)
    assert r.status_code == 200
    patients = [e["resource"] for e in r.json()["entry"]]
    assert len(patients) == 6
    assert all(p["name"][0]["family"].startswith("Synthetic") for p in patients)

    # pat-1 has a 4-point HbA1c series (LOINC 4548-4) — the trend the SMART
    # app charts.
    r = client.get("/fhir/Observation", params={"patient": "pat-1"}, headers=CLINICIAN)
    obs = [e["resource"] for e in r.json()["entry"]]
    a1c = [o for o in obs
           if o["code"]["coding"][0]["code"] == "4548-4"]
    assert len(a1c) == 4
    dates = sorted(o["effectiveDateTime"] for o in a1c)
    values = [o["valueQuantity"]["value"]
              for o in sorted(a1c, key=lambda o: o["effectiveDateTime"])]
    assert dates == sorted(dates) and values == [7.8, 7.4, 7.1, 6.9]

    # Inbox for pat-1 returns the seeded results (Observations + report).
    r = client.get("/fhir/inbox", params={"patient": "pat-1"}, headers=CLINICIAN)
    assert r.status_code == 200
    types = {e["resource"]["resourceType"] for e in r.json()["entry"]}
    assert types == {"Observation", "DiagnosticReport"}
