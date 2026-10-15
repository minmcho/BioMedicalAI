"""Idempotent loader for the synthetic (Synthea-style) demo bundle.

Loads backend/seed/synthea_bundle.json into a running OpenCare backend,
in dependency order (Patients first), skipping resources that already
exist. Safe to run any number of times against the same backend.

Usage:
    BASE_URL=http://127.0.0.1:8000 TOKEN=admin-token python seed/seed.py
    python seed/seed.py --bundle seed/synthea_bundle.json

Env: BASE_URL (default http://127.0.0.1:8000), TOKEN (default admin-token —
a dev-mode admin token; in OIDC deployments pass a real admin JWT).

The loader core (`load_bundle`) takes an injectable transport — any object
with `get(path)` / `post(path, json)` returning (status_code, body_dict) —
so tests drive it in-process against a FastAPI TestClient while the CLI
drives it over HTTP with httpx. No third path, no fake success: a resource
counts as created only when the backend answers 201, and skipped only
when a prior GET answered 200.
"""
import argparse
import json
import os
import sys
from pathlib import Path

# Dependency order: referenced resources before the resources that
# reference them (the backend stores references as strings, but inbox /
# trend queries read much better when parents land first).
TYPE_ORDER = ("Patient", "Encounter", "Condition", "MedicationRequest",
              "Procedure", "Observation", "DiagnosticReport", "Appointment")


class HttpTransport:
    """Real HTTP transport (httpx) with a bearer token."""

    def __init__(self, base_url: str, token: str):
        import httpx  # deferred so tests can import this module without it

        # trust_env=False: the target is a specific (usually local) backend;
        # ambient HTTP(S)_PROXY variables must not reroute or break it.
        self._client = httpx.Client(base_url=base_url.rstrip("/"), timeout=10.0,
                                    trust_env=False)
        self._headers = {"Authorization": f"Bearer {token}"}

    def get(self, path: str):
        r = self._client.get(path, headers=self._headers)
        return r.status_code, _safe_json(r)

    def post(self, path: str, payload: dict):
        r = self._client.post(path, json=payload, headers=self._headers)
        return r.status_code, _safe_json(r)


class ClientTransport:
    """Adapter for any test-client-like object with .get/.post returning
    responses with .status_code and .json() (e.g. FastAPI TestClient)."""

    def __init__(self, client, headers: dict):
        self._client = client
        self._headers = headers

    def get(self, path: str):
        r = self._client.get(path, headers=self._headers)
        return r.status_code, _safe_json(r)

    def post(self, path: str, payload: dict):
        r = self._client.post(path, json=payload, headers=self._headers)
        return r.status_code, _safe_json(r)


def _safe_json(response):
    try:
        body = response.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def load_bundle(bundle: dict, transport) -> dict:
    """Load every resource in a FHIR transaction Bundle. Idempotent.

    Returns {"created": n, "skipped": n, "total": n}. Raises RuntimeError
    on the first resource the backend neither has (GET 200) nor accepts
    (POST 201) — partial loads are reported, never hidden.
    """
    if bundle.get("resourceType") != "Bundle":
        raise ValueError("seed file must be a FHIR Bundle")
    resources = [e["resource"] for e in bundle.get("entry", [])
                 if isinstance(e, dict) and isinstance(e.get("resource"), dict)]
    resources.sort(key=lambda r: TYPE_ORDER.index(r["resourceType"])
                   if r.get("resourceType") in TYPE_ORDER else len(TYPE_ORDER))
    created = skipped = 0
    for resource in resources:
        rtype, rid = resource.get("resourceType"), resource.get("id")
        if not rtype or not rid:
            raise ValueError(f"seed resource missing resourceType/id: {resource!r}")
        status, _ = transport.get(f"/fhir/{rtype}/{rid}")
        if status == 200:
            skipped += 1
            continue
        status, body = transport.post(f"/fhir/{rtype}", resource)
        if status != 201:
            raise RuntimeError(
                f"loading {rtype}/{rid} failed: HTTP {status} {body.get('detail', body)}")
        created += 1
    return {"created": created, "skipped": skipped, "total": len(resources)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Load the synthetic demo bundle (idempotent).")
    parser.add_argument("--bundle", default=str(Path(__file__).with_name("synthea_bundle.json")))
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token", default=os.environ.get("TOKEN", "admin-token"))
    args = parser.parse_args(argv)

    bundle = json.loads(Path(args.bundle).read_text())
    transport = HttpTransport(args.base_url, args.token)
    result = load_bundle(bundle, transport)
    print(f"seed: {result['created']} created, {result['skipped']} already present, "
          f"{result['total']} total -> {args.base_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
