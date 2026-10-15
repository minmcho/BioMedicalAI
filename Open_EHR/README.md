# OpenCare EHR (Open_EHR)

Open-source, FHIR-native electronic health record. Original code,
Apache-2.0 licensed. Feature categories are inspired by large enterprise EHR
suites; no proprietary code, names, or trademarks are used.

**Current state: Phase 1 — core EHR MVP** (Phase 0 foundation included). A
working, tested FastAPI core with:

- A System-Studio-style module-pack lifecycle (adapted concept): packs install
  as **designer drafts only** and become usable only through separate human
  steps — `installed_draft → approved → attached → enabled` (plus `disabled`).
  Pack-declared API endpoints ship with `"enabled": false`.
- Role gates — `admin`, `configurator`, `clinician`, `viewer`, `patient` —
  enforced on every studio and FHIR write route, with negative tests.
- An append-only, **hash-chained audit log** written on every studio
  transition, every FHIR write, and every Appointment status transition.
- A real FHIR R4 surface — `Patient`, `Encounter`, `Observation`,
  `Condition`, `Procedure`, `MedicationRequest`, `DiagnosticReport`,
  `Appointment` (create / read / search) stored through SQLAlchemy 2.0,
  with per-resource required-field validation (exact rules in
  `contracts/fhir-resources.md`).
- Scheduling/ADT: Appointment search by practitioner and by date range, and
  a status-transition action (`booked → arrived → fulfilled`, `→ cancelled`)
  with enforced ordering and terminal states.
- A staff results inbox (`GET /fhir/inbox`) and patient-portal read scoping:
  patient users only ever see their own record (see below).

## Quick start (local, no Docker)

```bash
cd backend
python3 -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt
../.venv/bin/uvicorn app.main:app --reload   # http://127.0.0.1:8000/health
../.venv/bin/python -m pytest -q
```

Defaults to SQLite (`sqlite:///./openehr.db`) for local dev/tests — this is
the portable-repository design in action: the same code runs on PostgreSQL
in Docker Compose (see `docker-compose.yml`) by setting `DATABASE_URL`.
Note: the schema is created with `create_all` (no migrations yet), so a
database file created by an earlier phase must be deleted/recreated after
model changes.

## Quick start (Docker Compose)

```bash
cp .env.example .env
docker compose up --build   # app + PostgreSQL (pgvector) + Valkey
```

## Auth — two modes (`AUTH_MODE`)

`backend/app/security.py` is the only authentication boundary; everything
below it (packs, audit, FHIR data) is real in both modes.

- **`dev` (default):** bearer tokens map to seeded users — `admin-token`,
  `config-token`, `clinician-token`, `viewer-token`, and `patient-token`
  (a patient user linked to `Patient/pat-1`). Local development/tests only.
- **`oidc` (Keycloak-ready):** bearer tokens are JWTs (RS256) verified with
  PyJWT against a JWKS fetched from `OIDC_JWKS_URL`. Point it, plus the
  optional `OIDC_ISSUER` / `OIDC_AUDIENCE`, at a Keycloak realm in
  production (e.g. `OIDC_JWKS_URL=https://<keycloak>/realms/opencare/protocol/openid-connect/certs`).
  Claim mapping: `sub`/`preferred_username` → user; roles claim (default
  `realm_access.roles`, Keycloak shape; override the dotted path with
  `OIDC_ROLES_CLAIM`) → our role enum, unrecognised/absent → `viewer`;
  a `patient_ref` claim links a patient user to their record. Bad signature
  or expired token → 401.
  *Running a live Keycloak server and verifying against it is Phase 1 ops
  work — it is NOT verified by this repo's tests, which sign tokens with a
  throwaway RSA key and serve a fixture JWKS in-process.*

## Patient read scoping

A patient user carries `patient_ref`. They may read only resources whose
patient matches it: by-id reads of another patient's resource return 404
(non-disclosure), searches are force-scoped to their own record (an explicit
search for another patient → 403), and `GET /portal/my-results` returns
their own Observation + DiagnosticReport bundle, newest first.

## Layout

- `backend/app/` — FastAPI app (`main`, `db`, `models`, `security`, `studio`, `fhir`, `audit`)
- `backend/packs/` — versioned `*.pack.json` module-pack manifests
- `backend/tests/` — pytest suite (`test_phase0.py`, `test_phase1.py`)
- `contracts/` — API contracts: FHIR resources/profiles, GraphQL stub, gRPC `.proto` stub
- `docs/PHASES.md` — Phase 0–4 roadmap, Studio-reuse mapping, per-phase SQA gate
- `docs/SQA.md` — the standing per-feature SQA gate
- `docs/sqa/` — per-phase SQA reports

Synthetic data only — never put real PHI in this repository.
