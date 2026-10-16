"""Lab demo scenario (seed/lab_demo.py): builds through the real /lab
API on top of the seed bundle, lands in the intended teaching states,
and is idempotent at the scenario level. Synthetic data only."""
import importlib.util
import json
from pathlib import Path

from tests.conftest import CLINICIAN, PATIENT

SEED_DIR = Path(__file__).resolve().parent.parent / "seed"


def _load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SEED_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup_bundle(client):
    seed = _load_module("opencare_seed", "seed.py")
    bundle = json.loads((SEED_DIR / "synthea_bundle.json").read_text())
    seed.load_bundle(bundle, seed.ClientTransport(client, CLINICIAN))


def test_lab_demo_scenario_and_idempotency(client):
    _setup_bundle(client)
    demo = _load_module("opencare_lab_demo", "lab_demo.py")
    transport = demo.ClientTransport(client)

    summary = demo.run_demo(transport, CLINICIAN)
    assert summary["skipped"] is False and summary["created"] == 2

    # pat-1's CBC is released: visible in the patient portal, all final.
    portal = client.get("/portal/my-results", headers=PATIENT).json()
    by_id = {e["resource"]["id"]: e["resource"] for e in portal["entry"]}
    assert summary["released_report"] in by_id
    lab_obs = [r for r in by_id.values()
               if r["resourceType"] == "Observation"
               and (r.get("basedOn") or [{}])[0].get("reference")
               == "ServiceRequest/lab-demo-cbc"]
    assert len(lab_obs) == 4
    assert all(r["status"] == "final" for r in lab_obs)

    # pat-2's BMP sits in the verification queue, critical + QC-blocked,
    # and the gate really does refuse verification.
    queue = client.get("/lab/verification-queue", headers=CLINICIAN).json()
    assert queue["total"] == 1
    item = queue["items"][0]
    assert item["report"]["id"] == summary["blocked_report"]
    assert item["critical"] is True
    assert [b["analyte_code"] for b in item["qc_blocked"]] == ["2823-3"]
    assert client.post(f"/lab/reports/{summary['blocked_report']}/verify",
                       headers=CLINICIAN).status_code == 409

    # second run: scenario-level idempotency — nothing new is created
    again = demo.run_demo(transport, CLINICIAN)
    assert again == {"skipped": True, "created": 0}
    assert client.get("/lab/verification-queue",
                      headers=CLINICIAN).json()["total"] == 1
    orders = client.get("/lab/orders", headers=CLINICIAN).json()
    assert orders["total"] == 2
