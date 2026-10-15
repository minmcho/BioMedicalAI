# Phase 0 — SQA Report

**Date:** 2026-10-15
**Scope built:** Repo scaffold; FastAPI + SQLAlchemy 2.0 foundation; Studio
module-pack lifecycle (install as draft → approve → attach → enable/disable)
adapted from Agentic_App System Studio concepts; RBAC (admin / configurator /
clinician / viewer); append-only hash-chained audit log; FHIR R4
Patient/Encounter/Observation create/read/search-by-patient; pack manifests
(clinical-core, lab) with all declared endpoints `enabled: false`;
contracts (FHIR resource list, GraphQL stub, gRPC `.proto` stub);
Docker Compose (PostgreSQL pgvector + Valkey + app).

## SQA gate results (per docs/SQA.md)

| Check | Result |
|---|---|
| Unit + API tests | **PASS** — `16 passed, 1 warning in 0.63s` (pytest, backend/tests/test_phase0.py; warning is an upstream Starlette/httpx deprecation notice, not a failure) |
| FHIR validation | **PASS (minimal-field level)** — resourceType mismatch → 422, Patient without name/identifier → 422, Encounter without subject.reference → 422, Observation without code → 422; all covered by tests. Full US Core profile validation NOT RUN — validator not yet wired; named as Phase 1 work in contracts/fhir-resources.md |
| RBAC negative tests | **PASS** — viewer install → 403, clinician install → 403, viewer FHIR write → 403 |
| Audit assertions | **PASS** — test asserts audit rows exist for `studio.install` and `fhir.create`, count grows, and `verify_chain()` returns True |
| Completeness / no-noop | **PASS** — every route's effect is a persisted DB row observed by a test (installations, FHIR resources, audit rows). Lifecycle violations return 409 (attach before approve, enable before attach — both tested). Install idempotency tested (no duplicate installation) |
| Security checklist | **PASS for Phase 0 scope** — no secrets in code/fixtures (dev tokens are documented local-only defaults, overridable via env); synthetic data only in tests; no new non-open-source dependencies (FastAPI/SQLAlchemy/Pydantic/pytest/httpx/PyYAML, all permissive open source) |
| Data integrity | **PASS** — resource + projection + audit written in one session/transaction path; hash chain verifies |
| docker-compose.yml syntax | **PASS** — parsed with PyYAML: services `app`, `db`, `valkey` present |
| Docs | **PASS** — README, docs/PHASES.md (incl. Studio-reuse mapping), docs/SQA.md, contracts/ updated in this change |

## Known limitations (stated honestly)

- **Docker not available on this VM** (`docker` not installed), so
  `docker compose up` was **not run**. Compose was validated as YAML only;
  runtime verification on PostgreSQL must happen on Min's machine or a
  Docker-capable host. All tests ran against SQLite — which is the designed
  Tier-2/portable path, but Postgres-specific behaviour is unverified.
- **Dev auth is a stand-in at the auth boundary only**: bearer-token → seeded
  user mapping in `app/security.py`. Production replaces it with Keycloak
  OIDC (planned Phase 1). Data, packs, and audit below that layer are real.
- **Go API tier and Rust services are not in Phase 0** — only their contract
  stubs (`contracts/internal.proto`). They land with the features that need
  them (Phases 2–4, per docs/PHASES.md).
- **No UI yet** — no web, SwiftUI, or Kotlin clients in Phase 0.
- **GraphQL is a schema stub only** — no GraphQL server; the stub is labelled
  as such in `contracts/schema.graphql`, not presented as implemented.
- FHIR surface is intentionally minimal (3 resource types, minimal-field
  validation, no US Core profile validation, no update/delete, search only
  by patient reference).
- `app/main.py` instantiates a default app on import, which creates a local
  SQLite file in dev; harmless and git-ignored, noted here for transparency.
