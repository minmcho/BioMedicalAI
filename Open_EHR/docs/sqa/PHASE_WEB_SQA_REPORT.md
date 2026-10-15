# SQA report — Web app (`web/`)

Date: 2026-10-15. Scope: the Vite + React + TypeScript web client for the
OpenCare backend (Phase 1 web views, previously recorded as *not claimed*).
Gate: `docs/SQA.md`, adapted to a frontend (pytest items map to Vitest;
server-side FHIR/RBAC/audit behaviour is the backend's gate, re-verified
here over HTTP in the smoke).

## What was built

- `web/` — Vite 6 + React 19 + TypeScript (strict), Vitest 3 + jsdom +
  Testing Library. All open source (React MIT, Vite MIT, TypeScript
  Apache-2.0, Vitest MIT, jsdom MIT, @testing-library/react MIT,
  @vitejs/plugin-react MIT).
- Screens, each wired to the **real** API (no mock mode anywhere in
  shipped code):
  - Dev sign-in (seeded role/token accounts from
    `backend/app/security.py`, clearly labelled; token in memory +
    `sessionStorage`). With `VITE_AUTH_MODE=oidc` the screen shows the
    OIDC note instead — no fake SSO flow is implemented.
  - Clinician workspace: Patients (GET /fhir/Patient + client-side
    filter), Chart (GET /fhir/Patient/{id} + six section searches),
    Results inbox (GET /fhir/inbox), Scheduling (Appointment search by
    practitioner + date range; ADT buttons from the real state machine).
  - Patient portal: My results (GET /portal/my-results) + own
    conditions/medications (force-scoped searches); clinician nav hidden.
  - Studio packs (admin/configurator): GET /studio/packs +
    /studio/installations; install + approve/attach/enable/disable via
    the real POST endpoints.
  - Honest states everywhere: loading, empty, and error — a 403 renders
    as "Permission denied" with the server detail, a network failure as
    "Backend unreachable", never as an empty list.
- `web/Dockerfile` — Node 24 build stage → static serve via
  `nginxinc/nginx-unprivileged` (nginx, BSD-2-Clause; non-root, :8080),
  chosen so the runtime image carries no Node. A `web` service was added
  to `docker-compose.yml` (VITE_API_BASE / VITE_AUTH_MODE as build args).

## Gate checks

1. **Unit + component tests — PASS.** `npm test` (Vitest):
   `Test Files  4 passed (4)` / `Tests  22 passed (22)`.
   Coverage: API client request shaping (URLs, query encoding, Bearer
   header, transition POST body, inbox URL), Bundle unwrapping, ApiError
   mapping (403, network failure), role-based nav + route guarding for
   all five roles, ADT transition enable/disable logic per status
   (incl. terminal states), and a ChartPage render test against fixture
   FHIR data (+ its 403 render). Fixtures live only in `src/test/`.
2. **Production build — PASS.** `npm run build` (`tsc --noEmit && vite
   build`) exit 0; output `dist/assets/index-B4jbW6aR.js` (244.19 kB,
   gzip 75.65 kB) + CSS 3.79 kB.
3. **FHIR validation — N/A client-side (by design).** The client sends no
   resource writes except Appointment transitions; validation stays on
   the server. The smoke observed the server's 409 for an illegal
   transition and the client surfaces such errors verbatim.
4. **RBAC — PASS (client guards + server enforcement observed).**
   Nav/guard unit tests per role; in the smoke, viewer → inbox returned
   `403 "Role viewer may not perform this action"` (rendered by the web
   app as permission denied), patient → another patient's record
   returned 404, and patient searches were force-scoped.
5. **Completeness / no-noop — PASS.** Every control traces to a real
   endpoint, exercised in the smoke below; the ADT transition persisted
   (response `meta.versionId` became `"2"`), and the full Studio
   lifecycle (install → approve → attach → enable → disable) was driven
   over the same HTTP calls the Studio page buttons make.
6. **Security checklist — PASS.** No secrets in code or fixtures; the
   only tokens are the backend's documented dev-mode accounts, labelled
   dev sign-in; synthetic data only (see smoke); API errors surface the
   server's `detail` string only.
7. **Data integrity — PASS.** The client never mutates state locally as
   a substitute for the server: transitions and lifecycle actions render
   from server responses; failures trigger a reload and show the error.
8. **Docs — PASS.** Root README gained the web section (dev + prod run
   instructions); `docs/PHASES.md` marks the web clients built;
   `docker-compose.yml` updated and re-validated
   (`yaml.safe_load` → services `['db', 'valkey', 'app', 'web']`).

## Real smoke — synthetic data (Min's instruction)

Dataset: **the shared Synthea-style seed** (`backend/seed/`), loaded with
its own idempotent loader — not API-created ad-hoc data. All data is
fabricated (family names `SyntheticNNN`, identifiers `SYN-*`).

Method: core backend started locally (uvicorn, `AUTH_MODE=dev`, throwaway
SQLite at `/tmp/opencare-web-smoke.db`, deleted after), then
`BASE_URL=http://127.0.0.1:8000 TOKEN=admin-token python seed/seed.py`
→ **`seed: 86 created, 0 already present, 86 total`** (6 Patient,
12 Encounter, 10 Condition, 9 MedicationRequest, 5 Procedure,
32 Observation, 6 DiagnosticReport, 6 Appointment). Then every API call
the web pages make was issued over HTTP with the matching role token:

| Web page call | Result |
|---|---|
| GET /fhir/Patient (clinician) — Patients page | 200, total 6 (pat-1…pat-6) |
| GET /fhir/Patient/pat-1 — chart header | 200, "Alicia Synthetic402" |
| Chart sections ?patient=pat-1 | Condition 2, MedicationRequest 2, Observation 11, DiagnosticReport 1, Procedure 1, Encounter 2 (all 200) |
| GET /fhir/inbox?patient=pat-1 (clinician) | 200, total 12 (Observation + DiagnosticReport) |
| GET /fhir/inbox (viewer) | 403, "Role viewer may not perform this action" |
| GET /fhir/Appointment | 200, total 6, all `booked` |
| …?practitioner=Practitioner/prac-1 | 200, total 6 |
| …?start=2026-01-01 & end=2027-01-01 | total 6; a 2030–2031 range returned total 0 |
| POST transition appt-1 booked→arrived (ADT button) | 200, status `arrived`, `meta.versionId` `"2"` |
| POST transition arrived→booked | 409, "Illegal Appointment transition 'arrived' -> 'booked'" |
| GET /portal/my-results (patient-token) | 200, total 12 (Observation + DiagnosticReport) |
| GET /fhir/Condition (patient-token, force-scoped) | 200, total 2, subjects only `Patient/pat-1` |
| GET /fhir/Patient/pat-2 (patient-token) | 404 (non-disclosure, as documented) |
| GET /studio/packs (admin) | 200, [`clinical-core`, `lab`] |
| GET /studio/installations (admin) | 200, 0 on the fresh DB (page renders the empty state) |
| POST install clinical-core → approve → attach → enable → disable | 201 installed_draft, then approved → attached → enabled → disabled, each 200 |

The built app itself was served with `npm run preview` and answered
`GET /` with 200 (the production `dist/index.html`, title "OpenCare",
built JS asset referenced). Servers were stopped and the smoke DB
deleted afterwards.

## Limitations / NOT RUN

- **Browser click-through E2E: NOT RUN.** The smoke verified the exact
  HTTP contract every page uses (above) plus serving the built app, but
  no headless browser drove the UI. Component behaviour is covered by
  Vitest/Testing Library instead.
- **Dev sign-in only.** No real OIDC login flow exists in the web app;
  with `VITE_AUTH_MODE=oidc` it displays the provider note. A real
  Keycloak-backed sign-in remains open Phase 1 ops work (server-side
  OIDC is implemented; see PHASE1 report).
- **Docker image build: NOT RUN** (no Docker on this VM). The Dockerfile
  and compose `web` service are written and the compose YAML parses;
  `docker compose up --build` for the web image is unverified.
- Patient *chart* page is staff-facing; the patient role's own-record
  view is the portal page (results + conditions + medications), matching
  the server's scoping. Patient self-navigation to other patients'
  charts is excluded by nav guards and would 404 server-side anyway.
