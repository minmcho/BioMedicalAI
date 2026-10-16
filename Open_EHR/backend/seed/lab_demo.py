"""Synthetic LAB demo scenario, driven through the real /lab API.

Builds a small, deliberately instructive Phase 2 dataset on top of the
seed bundle's patients (run seed/seed.py first):

1. A CBC order for pat-1 taken all the way through the workflow —
   collected, received, resulted (all values in range), verified and
   RELEASED, so pat-1's portal shows a finished lab report.
2. A BMP order for pat-2 with a critical potassium (6.4 mmol/L), followed
   by a FAILING potassium QC run. Verification of that report is
   therefore blocked by the QC gate — the demo intentionally leaves it
   in that state. Recording a passing potassium QC run (POST /lab/qc)
   unblocks it.

Idempotent at the scenario level: if the demo orders already exist, the
script reports them as skipped and changes nothing.

Usage:
    BASE_URL=http://127.0.0.1:8000 TOKEN=clinician-token python seed/lab_demo.py

The core (`run_demo`) takes an injectable transport — any object with
`get(path, headers)` / `post(path, payload, headers)` returning
(status_code, body_dict) — so tests drive it in-process against a
FastAPI TestClient while the CLI drives it over HTTP with httpx.
"""
import argparse
import os
import sys

CBC_ORDER_ID = "lab-demo-cbc"
BMP_ORDER_ID = "lab-demo-bmp"

CBC_RESULTS = [
    {"code": "718-7", "value": 13.9},
    {"code": "4544-3", "value": 42.0},
    {"code": "6690-2", "value": 6.5},
    {"code": "777-3", "value": 240},
]

BMP_RESULTS = [
    {"code": "2345-7", "value": 110},     # glucose — high
    {"code": "3094-0", "value": 14},      # BUN — normal
    {"code": "2160-0", "value": 0.9},     # creatinine — normal
    {"code": "2951-2", "value": 139},     # sodium — normal
    {"code": "2823-3", "value": 6.4},     # potassium — CRITICAL high
]

FAILING_QC = {"analyte_code": "2823-3", "control_material": "SYN-QC-DEMO-K",
              "expected_low": 3.8, "expected_high": 4.2, "measured_value": 5.9}


class ClientTransport:
    """Adapter for any test-client-like object (e.g. FastAPI TestClient)."""

    def __init__(self, client):
        self._client = client

    def get(self, path, headers):
        r = self._client.get(path, headers=headers)
        return r.status_code, _safe_json(r)

    def post(self, path, payload, headers):
        r = self._client.post(path, json=payload, headers=headers)
        return r.status_code, _safe_json(r)


class HttpTransport:
    """Real HTTP transport (httpx)."""

    def __init__(self, base_url: str):
        import httpx  # deferred so tests can import this module without it

        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0,
                                    trust_env=False)

    def get(self, path, headers):
        r = self._client.get(path, headers=headers)
        return r.status_code, _safe_json(r)

    def post(self, path, payload, headers):
        r = self._client.post(path, json=payload, headers=headers)
        return r.status_code, _safe_json(r)


def _safe_json(response):
    try:
        body = response.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _expect(step: str, status: int, wanted: int, body: dict) -> dict:
    if status != wanted:
        raise RuntimeError(f"lab demo step '{step}' failed: HTTP {status} "
                           f"{body.get('detail', body)}")
    return body


def run_demo(transport, headers: dict) -> dict:
    """Build the lab demo scenario. `headers` must carry a clinician (or
    admin) bearer token. Returns a summary dict."""
    status, _ = transport.get(f"/lab/orders/{CBC_ORDER_ID}", headers)
    if status == 200:
        return {"skipped": True, "created": 0}
    for patient in ("pat-1", "pat-2"):
        status, _ = transport.get(f"/fhir/Patient/{patient}", headers)
        if status != 200:
            raise RuntimeError(
                f"lab demo needs Patient/{patient} — load the seed bundle "
                "first (seed/seed.py)")

    def order(order_id, code, patient):
        status, body = transport.post(
            "/lab/orders",
            {"id": order_id, "code": code,
             "subject": {"reference": f"Patient/{patient}"}}, headers)
        _expect(f"order {order_id}", status, 201, body)

    def transition(order_id, to_status):
        status, body = transport.post(
            f"/lab/orders/{order_id}/transition",
            {"to_status": to_status}, headers)
        _expect(f"{order_id} -> {to_status}", status, 200, body)

    def results(order_id, values):
        status, body = transport.post(
            f"/lab/orders/{order_id}/results", {"results": values}, headers)
        return _expect(f"results {order_id}", status, 201, body)

    # 1) CBC for pat-1: the full happy path, released.
    order(CBC_ORDER_ID, "58410-2", "pat-1")
    transition(CBC_ORDER_ID, "collected")
    transition(CBC_ORDER_ID, "received")
    cbc = results(CBC_ORDER_ID, CBC_RESULTS)
    cbc_report = cbc["report"]["id"]
    status, body = transport.post(f"/lab/reports/{cbc_report}/verify", {}, headers)
    _expect("verify CBC report", status, 200, body)
    status, body = transport.post(f"/lab/reports/{cbc_report}/release", {}, headers)
    _expect("release CBC report", status, 200, body)

    # 2) BMP for pat-2: critical potassium + a failing potassium QC run,
    #    leaving verification blocked by the QC gate (the teaching state).
    order(BMP_ORDER_ID, "51990-0", "pat-2")
    transition(BMP_ORDER_ID, "collected")
    transition(BMP_ORDER_ID, "received")
    transition(BMP_ORDER_ID, "in-process")
    bmp = results(BMP_ORDER_ID, BMP_RESULTS)
    bmp_report = bmp["report"]["id"]
    status, body = transport.post("/lab/qc", FAILING_QC, headers)
    qc = _expect("failing potassium QC", status, 201, body)
    assert qc["passed"] is False
    status, body = transport.post(f"/lab/reports/{bmp_report}/verify", {}, headers)
    if status != 409:
        raise RuntimeError(f"expected the QC gate to block verification of "
                           f"{bmp_report}, got HTTP {status}")

    return {"skipped": False, "created": 2,
            "released_report": cbc_report, "blocked_report": bmp_report,
            "qc_blocking_analyte": "2823-3"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Build the synthetic lab demo scenario (idempotent).")
    parser.add_argument("--base-url",
                        default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token",
                        default=os.environ.get("TOKEN", "clinician-token"))
    args = parser.parse_args(argv)

    transport = HttpTransport(args.base_url)
    headers = {"Authorization": f"Bearer {args.token}"}
    summary = run_demo(transport, headers)
    if summary["skipped"]:
        print("lab demo: already present, skipped")
    else:
        print(f"lab demo: created {summary['created']} orders; "
              f"released report {summary['released_report']}; "
              f"report {summary['blocked_report']} left QC-blocked on "
              f"analyte {summary['qc_blocking_analyte']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
