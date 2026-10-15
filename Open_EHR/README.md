# OpenCare EHR (Open_EHR)

Open-source, FHIR-native electronic health record. Original code,
Apache-2.0 licensed. Feature categories are inspired by large enterprise EHR
suites; no proprietary code, names, or trademarks are used.

**Current state (original phase numbering, reconciled 2026-10-15 —
see `docs/PHASES.md`): Phase 0 (SMART on FHIR app) and Phase 1 (core EHR
MVP incl. Synthea-style seed data) are DONE, each SQA-gated; the Agentic
ADK layer is delivered as a platform workstream alongside them.** A
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

## SMART on FHIR app (Phase 0)

`smart-app/` is the SMART App Launch companion app (open-source
`fhirclient` library, Vite + TypeScript): launched from an EHR it shows
the launched patient's summary, conditions, medications, and lab trends;
with no EHR session it runs in a clearly labelled local demo mode against
this backend + the seed data below. Registration steps for the public
Epic sandbox (needs your own client_id) are in `smart-app/README.md`.
**A live sandbox launch is NOT RUN until that registration exists.**

## Demo data (Synthea-style seed)

`backend/seed/synthea_bundle.json` is a curated, **fabricated** dataset
(FHIR transaction Bundle, Synthea-style): 6 synthetic patients with
conditions, medications, encounters, procedures, appointments, diagnostic
reports, and LOINC-coded labs repeated over time. Load it into a running
backend — the loader is idempotent (existing resources are skipped):

```bash
cd backend
BASE_URL=http://127.0.0.1:8000 TOKEN=admin-token ../.venv/bin/python seed/seed.py
# -> "seed: 86 created, 0 already present, 86 total" (then 0/86 on re-run)
```

Synthetic data only — never put real PHI in this repository.

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

## Web app (`web/`)

Vite + React + TypeScript client for the same backend — no separate data
layer, no mock mode: every screen calls the real FHIR / Studio / portal
API with the signed-in user's bearer token.

- **Dev sign-in** — pick a seeded role (admin / configurator / clinician /
  viewer / patient); the token lives in memory + `sessionStorage`. This is
  the backend's `AUTH_MODE=dev` boundary, labelled as dev sign-in in the
  UI. With `VITE_AUTH_MODE=oidc` the sign-in screen instead explains that
  sign-in happens through the configured identity provider — the web app
  does not fake an SSO flow.
- **Clinician workspace** (admin/configurator/clinician; viewer read-only
  patients) — patient list with client-side filter, patient chart
  (conditions, medications, observations, diagnostic reports, procedures,
  encounters), results inbox (`/fhir/inbox`), and scheduling (appointment
  search by practitioner + date range, ADT transition buttons driven by
  the backend state machine).
- **Patient portal** (patient role) — *My results* (`/portal/my-results`)
  plus the patient's own conditions/medications; clinician navigation is
  hidden and the server force-scopes every read.
- **Studio packs** (admin/configurator) — the module-pack lifecycle in the
  web UI: install as draft, then approve → attach → enable / disable via
  the real `/studio` endpoints.
- Every screen has honest loading / empty / error states — a 403 shows as
  permission denied with the server's detail, never as an empty list.

Run it:

```bash
cd web
npm install
npm run dev        # http://localhost:5173 (backend expected on :8000)
npm test           # Vitest: API client, role nav/guards, ADT logic, chart render
npm run build      # tsc --noEmit + production build to dist/
```

Production: `docker compose up --build` includes a `web` service
(nginx-unprivileged serving the built app on http://localhost:8080;
`VITE_API_BASE` / `VITE_AUTH_MODE` are build-time args). SQA:
`docs/sqa/PHASE_WEB_SQA_REPORT.md` (includes a real smoke run against the
Synthea synthetic seed).

## Agents (platform workstream — Agentic ADK layer)

A separate service in `agents/` (port 8100) built on the real **Google ADK**:
an LlmAgent roster (orchestrator + clinical summary, results explainer,
document intake, voice note) with read-only FHIR tools, a code-enforced
guardrail callback (tool allowlist + patient scope + hash-chained tool
audit), and **draft proposals as the only write path** — a human confirms,
and the write happens as that human through the core API. Models are
LiteLLM-configured via `AGENT_MODEL` (local Ollama works with no API key;
unset model → runs return an honest 503). Full detail: `docs/AGENTS.md`.

```bash
cd agents
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
CORE_API_URL=http://127.0.0.1:8000 .venv/bin/uvicorn app.main:app --port 8100
.venv/bin/python -m pytest -q
```

## Mobile (v1 source delivered — first builds pending on Min's Macs)

Native apps over the same FHIR API, in `mobile/` (full guide:
`mobile/README.md`; SQA: `docs/sqa/PHASE_MOBILE_SQA_REPORT.md`):

- `mobile/ios/` — SwiftUI app "OpenCare", Universal (iPhone + iPad),
  hand-authored Xcode project (+ XcodeGen `project.yml` fallback),
  URLSession client, Keychain session, six screens mirroring the API
  (sign-in by dev role, patients, chart, staff inbox, patient portal,
  read-only schedule). No third-party dependencies.
- `mobile/android/` — Kotlin/Jetpack Compose app with the same screens,
  OkHttp + kotlinx-serialization client, DataStore session.
- `mobile/shared/` — Kotlin Multiplatform contract skeleton (canonical
  FHIR-lite models + `OpenCareApi` interface). The apps mirror it with
  native models in v1 and do not link it yet — stated plainly in
  `mobile/README.md`.

Both apps are read-only in v1 and were validated on the build VM by
syntax/reference checks only — **neither has been compiled yet**; first
builds are Xcode (MacBook Air) and Android Studio.

## Layout

- `backend/app/` — FastAPI app (`main`, `db`, `models`, `security`, `studio`, `fhir`, `audit`)
- `backend/packs/` — versioned `*.pack.json` module-pack manifests
- `backend/tests/` — pytest suite (`test_phase0.py`, `test_phase1.py`, `test_seed.py`)
- `backend/seed/` — synthetic (Synthea-style) demo bundle + idempotent loader
- `smart-app/` — SMART on FHIR companion app (Phase 0; `fhirclient`, Vite + TS)
- `web/` — Vite + React + TypeScript client (`src/api`, `src/auth`, `src/pages`; Vitest in `src/**/*.test.*`)
- `agents/` — Agentic ADK service (own requirements/venv; `app/`, `tests/`, `evals/`)
- `mobile/` — native iOS (SwiftUI) + Android (Compose) apps + KMP contract skeleton
- `contracts/` — API contracts: FHIR resources/profiles, GraphQL stub, gRPC `.proto` stub
- `docs/PHASES.md` — original-plan Phase 0–4 roadmap + status, Studio-reuse mapping
- `docs/SQA.md` — the standing per-feature SQA gate
- `docs/sqa/` — per-phase SQA reports

Synthetic data only — never put real PHI in this repository.
