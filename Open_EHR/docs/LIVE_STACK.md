# OpenCare — Live Evidence Stack

What this is: the **local evidence stack** that Phase 17 stood up so the
production-readiness claims rest on real servers, not on SQLite-only
test doubles. It runs entirely from a workspace-persisted rootfs
(`~/workspace/live-stack/`) — no system packages, no Docker — so it
survives VM image replacement: the rootfs, data directories and
scripts are all under `~/workspace/`, and `start.sh` re-provisions
idempotently. It is a **local drill environment**, not a production
deployment; credentials live only in `~/workspace/live-stack/`
(mode 0600 files: `.secrets`, `.test-urls`, `credentials.env`,
`minio/credentials.env`) and never enter this repository.

## Operate it

```bash
~/workspace/live-stack/start.sh     # PostgreSQL + MariaDB (idempotent)
~/workspace/live-stack/status.sh    # engines used, readiness, versions, ports
~/workspace/live-stack/stop.sh      # clean shutdown, data kept
~/workspace/live-stack/minio/start.sh        # MinIO object store (:9000)
~/workspace/live-stack/keycloak-start.sh     # Keycloak 26 (:8080)
~/workspace/live-stack/keycloak-stop.sh
# Observability binaries (BD-61) have their own start scripts under
# ~/workspace/live-stack/{prometheus,grafana,otelcol}/
```

Test harnesses source `~/workspace/live-stack/.test-urls` for
`OPENCARE_TEST_PG_URL` / `OPENCARE_TEST_MARIADB_URL`; the OIDC live
tests take `OPENCARE_TEST_OIDC_*` mapped from `credentials.env`
(`KC_*`); the S3 drill takes `OPENCARE_TEST_S3_ENDPOINT` mapped from
`minio/credentials.env` (`OPENCARE_S3_ENDPOINT`).

## What's in it (versions measured live)

| Component | Version | Endpoint | Evidence role |
|---|---|---|---|
| PostgreSQL | 16.15 (Ubuntu rootfs) | 127.0.0.1:5432 | Primary-engine live legs; DR drill source/target |
| pgvector | 0.6.0 | (extension) | RAG store live test — real `vector` column + `<->` query |
| MariaDB | 10.11.14 | 127.0.0.1:3306 | Second-engine conformance + hot-swap drill target |
| MinIO | RELEASE.2025-04-22 | 127.0.0.1:9000 | Real S3 cold tier (BD-63) — bucket `opencare-cold` |
| Keycloak | 26.7.0 | 127.0.0.1:8080 | Live OIDC: discovery, issued-token verification, BD-54 session flow |
| Local CA + certs | openssl-issued | TLS proof on :8443 | Live TLS serving proof (uvicorn SSL; nginx template route remains documented) |
| Prometheus / Grafana / otelcol | 2.53.1 / 11.6.0 / 0.114.0 | :9090 / :3000 / :4318 | BD-61 observability run: 7/7 targets UP, journey traces in the collector |

Databases provisioned per engine: `opencare_test`, `opencare_test_wh`
(app + OMOP warehouse), plus drill databases the workstreams created
(`opencare_loaddr*`, `opencare_probe*`) which are evidence artefacts,
not part of the standing stack.

## What was proven on it (Phase 17)

- Engine conformance suites on both live engines (4/4, zero skips) and
  the pgvector RAG live test (1/1).
- DB-platform hot-swap drill (BD-64): test-connection records, governed
  swap SQLite → live engine with health-proof + automatic rollback,
  manual rollback — `tests/test_phase17_dbplat_live.py`.
- S3 tiering drill (BD-63): aged raw entry moved to MinIO, retrieved
  byte-exact with hash verification.
- Load evidence: the app serves correctly to the connection-pool
  ceiling; the original SQLAlchemy default pool (5 + 10) wedged at 20
  concurrent clients (measured, reproduced twice). Pool sizing is now
  environment-configurable (`OPENCARE_DB_POOL_*`, defaults 20 + 20);
  post-fix probe: 30/30 requests succeed at concurrency 20 and 30.
  Full measured tables: `docs/sqa/PHASE17_SQA_REPORT.md`.
- DR drill: `pg_dump` of the exercised live database (1,324 rows,
  185 tables) → restore into a fresh database → zero row-count
  mismatches → audit hash chain verifies on the restored database.

## Honest limits

- **Docker runtime: NOT RUN** — no Docker exists on this VM; the Helm
  chart (BD-62) is validated by lint + rendering only, and no
  Kubernetes cluster install has been executed anywhere.
- The servers run inside a user namespace from workspace binaries
  (the workspace filesystem cannot chown to the `postgres`/`mysql`
  uids). This is a local-evidence arrangement, not a deployment shape.
- Absolute load latencies were measured on a shared 2-CPU VM under
  multi-agent contention and are pessimistic; the pool-ceiling cliff
  and its fix are structural and reproduce cleanly.
- Object-store backup (`mc mirror` / bucket replication) was NOT
  drilled — the DR drill covers the database; a combined DB + bucket
  drill remains outstanding.
- AWS S3 proper was not used (no account); the cold tier is
  endpoint-generic boto3, drilled against MinIO only.
