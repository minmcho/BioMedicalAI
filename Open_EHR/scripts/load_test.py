#!/usr/bin/env python3
"""OpenCare Phase 17 (WS-LOADDR, Scope D) — live-stack load evidence.

Boots the real application (uvicorn, single worker, real HTTP — NOT
TestClient) against the LIVE workspace PostgreSQL stack, seeds the
synthetic demo bundle through the app's own HTTP seed loader path,
then exercises the hot paths at a stated concurrency and reports
MEASURED per-path latency (p50/p95/p99/max), throughput and errors.

Database selection (no secrets are stored in this file):
  * DATABASE_URL / WAREHOUSE_DATABASE_URL env vars win when set.
  * Otherwise OPENCARE_TEST_PG_URL (from ~/workspace/live-stack/
    .test-urls, sourced by the operator) supplies the main URL and the
    warehouse URL is derived by swapping the trailing database name
    ``opencare_test`` -> ``opencare_test_wh``.
  * --main-db / --warehouse-db rename the databases on the same
    server/credentials (used by WS-LOADDR to avoid the shared test
    databases, which live-suite fixtures drop/recreate).

The target databases are created when missing (plus the pgvector
extension), matching live-stack/start.sh provisioning.

Workload (fixed, stated): concurrency = 20 client threads throughout.
  fhir_patient_read   GET  /fhir/Patient/pat-1          200 requests
  chart_graphql       POST /graphql patientChart        100 requests
  order_create_cpoe   POST /orders (nursing order)      100 requests
  results_inbox_list  GET  /inbox/results               100 requests
  census              GET  /inpatient/census            100 requests
  bed_board           GET  /inpatient/beds              100 requests
Paths run one at a time (each saturated at the stated concurrency), so
per-path throughput is that path's own. Auth: dev tokens
(clinician-token for workload, admin-token for seeding).

Output: a JSON document (raw per-request latencies included) written
to --out, plus a human summary on stdout. Exit 0 only when the app
booted, seeding succeeded, and every path's error count is 0.
"""
import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, urlunparse

CHART_QUERY = """
query Chart($patientId: ID!) {
  patientChart(patientId: $patientId) {
    patient { id displayName birthDate gender }
    conditions { id codeDisplay clinicalStatus }
    medications { id medicationDisplay status intent }
    recentResults { id resourceType codeDisplay value status }
  }
}
"""


def _swap_db(url: str, new_db: str) -> str:
    parts = urlparse(url)
    return urlunparse(parts._replace(path=f"/{new_db}"))


def _find_backend() -> Path:
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents, Path.cwd()]:
        for cand in (parent / "backend", parent / "Open_EHR" / "backend"):
            if (cand / "app" / "main.py").exists():
                return cand
    raise SystemExit("cannot locate backend/ (app/main.py) from script location or cwd")


def _ensure_databases(main_url: str, wh_url: str) -> None:
    """Create the target databases (+ vector ext) when missing."""
    from sqlalchemy import create_engine, text

    def ensure(url: str) -> None:
        db = urlparse(url).path.lstrip("/")
        admin_url = _swap_db(url, "postgres")
        admin = create_engine(admin_url, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :d"),
                {"d": db}).first()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{db}"'))
        admin.dispose()
        eng = create_engine(url)
        with eng.connect() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.commit()
        eng.dispose()

    ensure(main_url)
    if wh_url != main_url:
        ensure(wh_url)


def _pg_version(url: str) -> str:
    from sqlalchemy import create_engine, text

    eng = create_engine(url)
    try:
        with eng.connect() as conn:
            return conn.execute(text("SELECT version()")).scalar()
    finally:
        eng.dispose()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="JSON results path")
    ap.add_argument("--concurrency", type=int, default=20)
    ap.add_argument("--main-db", default=None,
                    help="rename main database on the test-URL server")
    ap.add_argument("--warehouse-db", default=None,
                    help="rename warehouse database on the test-URL server")
    ap.add_argument("--keep-server-log", default=None)
    args = ap.parse_args()

    main_url = os.environ.get("DATABASE_URL") or os.environ.get("OPENCARE_TEST_PG_URL")
    if not main_url:
        print("FAIL: set DATABASE_URL or OPENCARE_TEST_PG_URL (source the "
              "live-stack test URLs)", file=sys.stderr)
        return 2
    wh_url = os.environ.get("WAREHOUSE_DATABASE_URL")
    if not wh_url:
        wh_url = _swap_db(main_url, "opencare_test_wh")
    if args.main_db:
        main_url = _swap_db(main_url, args.main_db)
    if args.warehouse_db:
        wh_url = _swap_db(wh_url, args.warehouse_db)

    backend = _find_backend()
    port = _free_port()
    base = f"http://127.0.0.1:{port}"

    _ensure_databases(main_url, wh_url)
    pg_version = _pg_version(main_url)

    env = dict(os.environ)
    env["DATABASE_URL"] = main_url
    env["WAREHOUSE_DATABASE_URL"] = wh_url
    env["AUTH_MODE"] = "dev"
    log_path = args.keep_server_log or f"/tmp/loaddr-uvicorn-{port}.log"
    log_fh = open(log_path, "wb")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(port), "--workers", "1"],
        cwd=str(backend), env=env, stdout=log_fh, stderr=subprocess.STDOUT,
        start_new_session=True)

    import httpx

    result: dict = {
        "tool": "phase17_work/loaddr/load_test.py",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "cpu_count": os.cpu_count(),
            "postgres": pg_version,
            "server": "uvicorn, 1 worker, real HTTP on 127.0.0.1 scratch port",
            "main_database": urlparse(main_url).path.lstrip("/"),
            "warehouse_database": urlparse(wh_url).path.lstrip("/"),
            "credentials": "live-stack test URLs (values not recorded)",
        },
        "concurrency": args.concurrency,
        "seed": None,
        "paths": [],
    }
    try:
        # --- boot wait -------------------------------------------------
        deadline = time.time() + 180
        booted = False
        with httpx.Client(trust_env=False, timeout=5.0) as c:
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    r = c.get(f"{base}/health")
                    if r.status_code == 200:
                        booted = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
        if not booted:
            result["boot"] = "FAILED — see server log"
            result["server_log"] = log_path
            Path(args.out).write_text(json.dumps(result, indent=2))
            print(f"BOOT FAILED; server log: {log_path}")
            return 1
        result["boot"] = "ok (/health 200)"

        # --- seed via the app's own bundle over HTTP -------------------
        bundle_path = backend / "seed" / "synthea_bundle.json"
        bundle = json.loads(bundle_path.read_text())
        resources = [e["resource"] for e in bundle.get("entry", [])
                     if isinstance(e, dict) and isinstance(e.get("resource"), dict)]
        order = ("Patient", "Encounter", "Condition", "MedicationRequest",
                 "Procedure", "Observation", "DiagnosticReport", "Appointment")
        resources.sort(key=lambda r: order.index(r["resourceType"])
                       if r.get("resourceType") in order else len(order))
        admin_h = {"Authorization": "Bearer admin-token"}
        created = skipped = 0
        with httpx.Client(base_url=base, trust_env=False, timeout=30.0) as c:
            for res in resources:
                rt, rid = res["resourceType"], res["id"]
                g = c.get(f"/fhir/{rt}/{rid}", headers=admin_h)
                if g.status_code == 200:
                    skipped += 1
                    continue
                p = c.post(f"/fhir/{rt}", json=res, headers=admin_h)
                if p.status_code != 201:
                    raise RuntimeError(f"seed {rt}/{rid}: HTTP {p.status_code} {p.text[:300]}")
                created += 1
        result["seed"] = {"created": created, "skipped": skipped,
                          "total": len(resources),
                          "bundle": "backend/seed/synthea_bundle.json"}

        # --- workload ---------------------------------------------------
        # One keep-alive httpx client per worker thread (thread-local),
        # so measured latency is server time, not per-request TCP setup.
        import threading

        _tls = threading.local()

        def _client() -> "httpx.Client":
            c = getattr(_tls, "client", None)
            if c is None:
                c = httpx.Client(base_url=base, trust_env=False, timeout=30.0)
                _tls.client = c
            return c

        clinician_h = {"Authorization": "Bearer clinician-token"}

        def run_path(name: str, count: int, fn) -> dict:
            lat: list[float] = []
            statuses: dict[str, int] = {}
            errors: list[str] = []

            def one(i: int):
                t0 = time.perf_counter()
                try:
                    status = fn(i)
                except Exception as exc:  # measured, not hidden
                    status = f"EXC:{type(exc).__name__}"
                    errors.append(f"{status}: {exc}"[:200])
                dt = (time.perf_counter() - t0) * 1000.0
                return dt, status

            wall0 = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                for dt, status in pool.map(one, range(count)):
                    lat.append(dt)
                    key = str(status)
                    statuses[key] = statuses.get(key, 0) + 1
            wall = time.perf_counter() - wall0
            ok = sum(v for k, v in statuses.items()
                     if k.isdigit() and 200 <= int(k) < 300)
            lat_sorted = sorted(lat)
            entry = {
                "path": name, "requests": count, "ok": ok,
                "errors": count - ok, "status_counts": statuses,
                "wall_seconds": round(wall, 3),
                "throughput_rps": round(count / wall, 2) if wall else None,
                "latency_ms": {
                    "mean": round(statistics.mean(lat), 2) if lat else None,
                    "p50": round(_percentile(lat_sorted, 50), 2),
                    "p95": round(_percentile(lat_sorted, 95), 2),
                    "p99": round(_percentile(lat_sorted, 99), 2),
                    "max": round(max(lat), 2) if lat else None,
                },
                "raw_latency_ms": [round(v, 2) for v in lat],
                "error_samples": errors[:5],
            }
            result["paths"].append(entry)
            print(f"{name:22s} n={count} ok={ok} err={count-ok} "
                  f"p50={entry['latency_ms']['p50']}ms "
                  f"p95={entry['latency_ms']['p95']}ms "
                  f"max={entry['latency_ms']['max']}ms "
                  f"rps={entry['throughput_rps']}")
            return entry

        def fhir_read(_i: int) -> int:
            return _client().get("/fhir/Patient/pat-1", headers=clinician_h).status_code

        def chart(_i: int) -> int:
            r = _client().post("/graphql", headers=clinician_h,
                               json={"query": CHART_QUERY,
                                     "variables": {"patientId": "pat-1"}})
            if r.status_code == 200 and "errors" in r.json():
                return 500
            return r.status_code

        def order_create(i: int) -> int:
            payload = {"type": "nursing", "code": "vital-signs",
                       "display": "Vital signs every 4 hours",
                       "subject": {"reference": "Patient/pat-1"},
                       "note": f"loaddr {uuid.uuid4().hex[:8]} {i}"}
            return _client().post("/orders", json=payload,
                                  headers=clinician_h).status_code

        def inbox(_i: int) -> int:
            return _client().get("/inbox/results", headers=clinician_h).status_code

        def census(_i: int) -> int:
            return _client().get("/inpatient/census", headers=clinician_h).status_code

        def beds(_i: int) -> int:
            return _client().get("/inpatient/beds", headers=clinician_h).status_code

        run_path("fhir_patient_read", 200, fhir_read)
        run_path("chart_graphql", 100, chart)
        run_path("order_create_cpoe", 100, order_create)
        run_path("results_inbox_list", 100, inbox)
        run_path("census", 100, census)
        run_path("bed_board", 100, beds)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
        result["server_log_tail"] = Path(log_path).read_text(errors="replace")[-2000:]

    result["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    Path(args.out).write_text(json.dumps(result, indent=2))
    total_err = sum(p["errors"] for p in result["paths"])
    print(f"wrote {args.out}; total errors across paths: {total_err}")
    return 0 if total_err == 0 and result.get("boot", "").startswith("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
