# OpenCare EHR (Open_EHR)

Open-source, FHIR-native electronic health record. Original code,
Apache-2.0 licensed. Feature categories are inspired by large enterprise EHR
suites; no proprietary code, names, or trademarks are used.

**Current state (original phase numbering, reconciled 2026-10-15 —
see `docs/PHASES.md`): Phase 0 (SMART on FHIR app), Phase 1 (core EHR
MVP incl. Synthea-style seed data) and Phase 2 (Lab LIS) are DONE, each
SQA-gated; the Agentic ADK layer is delivered as a platform workstream
alongside them.** A working, tested FastAPI core with:

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
- A **Studio customization engine** (see below): forms, workflows,
  terminology, navigation, branding and permissions are versioned Studio
  configuration with a draft → publish lifecycle — not hardcoded per site.

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

For the lab, `backend/seed/lab_demo.py` adds a Phase 2 scenario on top
of the bundle (same env vars; use a clinician token): a CBC for pat-1
taken through verification and release, and a BMP for pat-2 with a
critical potassium left blocked by a failing QC run — recording a
passing potassium QC run unblocks it. Scenario-level idempotent.

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

## Studio customization engine

Everything a deploying organisation changes is **Studio configuration data**
(spec: `docs/STUDIO_CUSTOMIZATION.md`) — versioned objects under
`/studio/config`, in the same portable SQLAlchemy layer as the chart:

- **Kinds:** `form` (FHIR Questionnaire-shaped definitions), `workflow`
  (state machines as data), `terminology` (code lists / value sets, lab
  panels and reference ranges), `navigation` (menu items — **modules only**;
  page/form entries are rejected with 400, the System Studio rule),
  `branding` (org name, logo, colour, portal title), `permissions`
  (per-role capability overrides).
- **Governance:** create = draft; drafts are invisible to clinician/viewer
  and to effective reads. Publish supersedes the previously published
  version (kept for rollback); rollback republishes an old payload as a new
  version. Published versions are immutable — editing one spawns a new
  draft. Every transition writes a hash-chained audit row. Writes require
  admin/configurator.
- **Consumed by the core, for real:** the Appointment ADT state machine is
  loaded from the published `workflow/appointment-adt` config (seeded as v1
  at startup with the standard transitions; the hardcoded map is only a
  fallback if the config is disabled). FHIR write/inbox gates are
  capabilities — built-in defaults merged with the published
  `permissions/role-matrix` config and enforced in the API dependency, so
  publishing an override changes API outcomes (tested). Form responses are
  accepted only against a **published** form:
  `POST /fhir/QuestionnaireResponse` validates the referenced form and its
  required items (422 otherwise).
- **Effective reads:** `GET /studio/config/{kind}/effective[/{key}]` returns
  published config only, for any staff role. A structural diff between two
  versions is at `GET /studio/config/{kind}/{key}/diff?from=&to=`.
- The web Studio page has a **Studio configuration** section (list by kind,
  view payloads, create/edit drafts, publish, disable, roll back).

## Lab LIS (Phase 2)

A laboratory information system built **on** the Studio customization
engine — the lab's catalog and workflow are published Studio
configuration, not code (seeded as v1 at startup, editable through the
normal draft → publish flow):

- **Catalog as config** — `terminology/lab-catalog`: LOINC panels (CBC,
  BMP) and analytes with units, reference ranges and **critical
  thresholds**. Orders validate against it: unknown/inactive code → 422.
- **Workflow as config** — `workflow/lab-order-flow`:
  `ordered → collected → received → in-process → resulted → verified →
  released` (+ rejection with a mandatory reason). Collection creates a
  linked **Specimen** with a server-generated accession id; illegal
  transitions → 409; every step is audit-logged.
- **Results** — manual entry and **analyser ingest** (HL7 v2 ORU^R01
  with a dependency-free parser, plus a CSV fallback). Results are
  created as preliminary Observations + a DiagnosticReport with
  **interpretation computed from the configured ranges** (N/L/H and
  critical LL/HH; critical reports carry a critical-result extension).
- **Two-step governance** — drafts carry a `lab-unreleased` visibility
  tag: invisible in the clinician inbox, the patient portal and every
  patient read path until a clinician **verifies** (report → final) and
  then **releases** (observations → final, tag removed). Nothing is
  ever auto-released; ingest produces drafts in exactly the same way.
- **QC gate** — QC runs are first-class records with server-computed
  pass/fail. If the latest QC run for an analyte failed, verify/release
  of any report containing it → **409** until a passing run exists
  after it.
- Resources `ServiceRequest` / `Specimen` / `Task` also joined the
  generic FHIR surface. Exact contract: `contracts/fhir-resources.md`.
  A synthetic demo scenario (released CBC + a critical, QC-blocked BMP)
  is in `backend/seed/lab_demo.py`; SQA: `docs/sqa/PHASE2_SQA_REPORT.md`.

## Analytics warehouse + population health (Phase 3)

Clinical writes feed an OMOP analytics warehouse through a
**transactional outbox**: one central SQLAlchemy flush hook writes an
`outbox_events` row in the *same transaction* as every clinical resource
create/update (Patient, Encounter, Condition, Observation,
MedicationRequest, Procedure, DiagnosticReport, ServiceRequest) — no
endpoint writes outbox rows by hand.

- **ETL** (`backend/app/etl.py`): drains the outbox in order into a
  second database (`WAREHOUSE_DATABASE_URL`, default
  `sqlite:///./warehouse.db`; PostgreSQL/DuckDB URLs work) with OMOP CDM
  tables — person, visit_occurrence, condition_occurrence, measurement,
  observation, drug_exposure, procedure_occurrence — upserting by FHIR
  source id. Idempotent (a re-run with no new events changes nothing);
  a failed transform dead-letters its event with the error recorded and
  the drain continues. Run it with `python -m app.etl run` or
  `POST /warehouse/etl/run` (admin).
- **Concept mapping** is a small documented subset (`concept_map`):
  standard OMOP concepts where established (T2DM → 201826, HbA1c →
  3000963, Female → 8532, …), local custom concepts (≥ 2,000,000,000,
  the OMOP convention) elsewhere; unmapped codes land as concept 0 and
  are counted in the coverage statistic — never dropped.
- **dbt project** (`warehouse/dbt/`): staging views over the OMOP tables
  + a `diabetes_registry` mart; DuckDB/Postgres profiles in
  `profiles.yml.example`.
- **Population health** (`/pophealth`, admin/clinician): registries are
  Studio configuration (kind `cohort` — seeded: diabetes, hypertension),
  computed from the warehouse, **aggregate-only**: member counts, % with
  latest lab in range, gender/age-band stratification with small-cell
  suppression (strata < 5 return `"<5"`, never the raw number), plus a
  cohort explorer (`POST /pophealth/explore`). The web app has a
  Population health page with registry cards, the explorer, and an
  outbox/ETL/coverage status panel. Contract: `contracts/warehouse.md`;
  SQA: `docs/sqa/PHASE3_SQA_REPORT.md`.

## Billing + pharmacy (Phase 4A)

- **Billing** (`backend/app/billing.py`, web Billing page): charge
  capture generates a draft FHIR Claim from an encounter — the problem
  list becomes the diagnoses (priced problem-evaluation lines) and the
  encounter's procedures/observations are priced from the **Studio fee
  schedule** (`terminology/fee-schedule`, seeded synthetic USD prices).
  A code missing from the schedule is charged at 0 and flagged
  **unpriced**, never dropped; totals are computed server-side. The
  claim lifecycle (`draft → ready → submitted → paid/denied → appealed`)
  is **Studio workflow config** (`workflow/claim-lifecycle`); a denied
  claim can be appealed **once, with a note**, and its full status
  history is preserved on the claim. Submitted claims export as an
  **X12 837P-style EDI file** (ISA/GS/ST/CLM/SV1/SE/GE/IEA, amounts
  reconciling with the claim) — a format-correct export only; there is
  no clearinghouse submission. Roles: admin/configurator manage,
  clinician views, patients see only their own claims, viewer denied
  by default (grantable via Studio permissions config).
- **Pharmacy** (`backend/app/pharmacy.py`, web Pharmacy page): the
  formulary is **Studio terminology config** (`terminology/formulary`,
  RxNorm) with per-medication `stockTracked` flags. Dispensing an
  active MedicationRequest creates a MedicationDispense, decrements
  the server-side stock counter, and flips the request to `dispensed`;
  overselling is a 409 with nothing written. **BCMA-lite** med-admin
  scans wristband + medication + dispense id and enforces right
  patient / right medication / right dispense (each mismatch a 422
  naming the right; a second administration of one dispense is a 409).
  Contract: `contracts/fhir-resources.md` (Phase 4A section);
  SQA: `docs/sqa/PHASE4A_SQA_REPORT.md`.

## Specialty boards (Phase 4B)

- **ED board** (`backend/app/boards.py`, web ED board page): ED visits
  are Encounters with class EMER whose status carries the tracking
  state (`waiting → triage → in-treatment → disposition`) from the
  published **Studio workflow config** (`workflow/ed-flow`). Triage
  records the **ESI acuity (1–5)**; the board groups the live census by
  state with **wait times computed on the server** from the recorded
  arrival. Admit creates a linked inpatient Encounter (`partOf`);
  depart closes the visit — both are governed exits, not bare status
  flips.
- **Radiology** (same module, web Radiology page): an **ImagingStudy**
  is created from a ServiceRequest with the modality validated against
  the published **Studio terminology config**
  (`terminology/imaging-modalities`). **DICOMweb** reads
  (`GET /dicomweb/studies[/{uid}]`) serve study-level DICOM JSON (PS3.18
  tags) derived from the stored studies. Reports flow
  `preliminary → final` on radiologist verification
  (`radiology.verify`). **Studies are metadata-only in v1 — no pixel
  data is stored**, and there is no embedded viewer: the page links out
  to an external OHIF-style viewer whose base URL is **Studio
  integration config** (`integration/dicom-viewer` → `viewer.baseUrl`).
- **Surgery board** (same module, web Surgery board page): a case is a
  Procedure + linked scheduling Appointment; rooms are **Studio
  terminology config** (`terminology/or-rooms`) and the case workflow
  (`scheduled → pre-op → in-op → pacu → done`) is **Studio workflow
  config** (`workflow/surgery-flow`). The day board groups cases by
  room, and **double-booking a room with overlapping times is refused
  server-side (409)** on both scheduling and rescheduling, comparing
  the real stored times.
  Contract: `contracts/fhir-resources.md` (Phase 4B section);
  SQA: `docs/sqa/PHASE4B_SQA_REPORT.md`. **Phase 4 is complete**
  (4A + 4B).

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
- **Studio packs + configuration** (admin/configurator) — the module-pack
  lifecycle in the web UI (install as draft, then approve → attach →
  enable / disable via the real `/studio` endpoints), plus the Studio
  configuration section: versioned config objects by kind, payload viewer,
  draft editor, publish / disable / roll back via `/studio/config`.
- **Laboratory** (staff; viewer read-only) — order queue with lifecycle
  actions, specimen tracking, result entry against the configured
  catalog, analyser ingest (paste or file, HL7/CSV), the verification
  queue with CRITICAL and QC-blocked badges, and the QC page (record
  runs, see the gate state). All calls go to the real `/lab` API.
- **Specialty boards** (staff; viewer read-only) — the ED board (census
  grouped by tracking state, ESI acuity badges, server wait times,
  triage/admit/depart actions), Radiology (studies with report status,
  draft + verify flow, and an “Open in viewer” link to the configured
  external OHIF viewer — studies are metadata-only, no embedded
  viewer), and the Surgery board (day view grouped by room, case state
  actions, reschedule, with server 409 conflicts surfaced inline). All
  calls go to the real `/ed`, `/radiology` and `/surgery` APIs.
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

- `backend/app/` — FastAPI app (`main`, `db`, `models`, `security`, `studio`, `studio_config`, `configstore`, `fhir`, `lab`, `audit`, plus Phase 3: `outbox`, `warehouse`, `etl`, `pophealth`; plus Phase 4A: `billing`, `pharmacy`; plus Phase 4B: `boards`)
- `backend/packs/` — versioned `*.pack.json` module-pack manifests
- `backend/tests/` — pytest suite (`test_phase0.py`, `test_phase1.py`, `test_seed.py`, `test_studio_config.py`, `test_lab.py`, `test_lab_demo.py`, `test_phase3_warehouse.py`, `test_phase3_pophealth.py`, `test_phase4a_billing.py`, `test_phase4a_pharmacy.py`, `test_phase4b_boards.py`)
- `backend/seed/` — synthetic (Synthea-style) demo bundle + idempotent loader, plus the Phase 2 lab demo scenario (`lab_demo.py`)
- `warehouse/dbt/` — dbt project over the OMOP warehouse (staging views + `diabetes_registry` mart)
- `smart-app/` — SMART on FHIR companion app (Phase 0; `fhirclient`, Vite + TS)
- `web/` — Vite + React + TypeScript client (`src/api`, `src/auth`, `src/pages`; Vitest in `src/**/*.test.*`)
- `agents/` — Agentic ADK service (own requirements/venv; `app/`, `tests/`, `evals/`)
- `mobile/` — native iOS (SwiftUI) + Android (Compose) apps + KMP contract skeleton
- `contracts/` — API contracts: FHIR resources/profiles, warehouse + population-health contract (`warehouse.md`), GraphQL stub, gRPC `.proto` stub
- `docs/PHASES.md` — original-plan Phase 0–4 roadmap + status, Studio-reuse mapping
- `docs/SQA.md` — the standing per-feature SQA gate
- `docs/sqa/` — per-phase SQA reports

Synthetic data only — never put real PHI in this repository.
