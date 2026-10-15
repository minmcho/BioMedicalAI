# SQA Report — SMART on FHIR app (original Phase 0) + Synthea seed (original Phase 1)

Date: 2026-10-15 · Gate: docs/SQA.md · Scope: restore the two pieces of the
original phase plan that the foundation-first build had renumbered away —
(a) `smart-app/`, a SMART on FHIR app for the Epic sandbox, and (b)
`backend/seed/`, Synthea-style synthetic demo data with an idempotent
loader. `docs/PHASES.md` is reconciled to the original numbering in the
same change. No `backend/app/*`, `web/`, `mobile/`, or `agents/` code was
touched.

## Checks and results

### 1. Unit + API tests
- Backend full suite (from `backend/`, includes the 2 new seed tests):
  **`39 passed, 1 warning in 5.77s`** (37 pre-existing + `test_seed.py` ×2;
  the warning is the upstream Starlette/httpx TestClient deprecation).
- SMART app Vitest (`smart-app/`): **Test Files 1 passed (1), Tests 7
  passed (7)** — Bundle unpacking, summary composition, lab-trend
  grouping/sorting, sparkline geometry/rendering, against hand-made
  synthetic fixtures shaped like public sandbox test-patient resources.
- SMART app build: `npm run build` (tsc --noEmit && vite build) **exit 0**
  — `dist/index.html` (1.98 kB) + `dist/launch.html` (0.58 kB) emitted.

### 2. FHIR validation
Seed resources pass through the backend's real create path (resourceType
+ required-field validation); the loader raises on the first non-201
rather than counting it. The seed test asserts the bundle loads through
that path and that pat-1's HbA1c series returns exactly
[7.8, 7.4, 7.1, 6.9] across 2025-01→10. Full US Core profile validation
remains NOT wired (unchanged, named in contracts/fhir-resources.md).

### 3. RBAC negative tests
No new backend routes were added, so no new RBAC surface exists: the
loader authenticates with an admin dev token over the unchanged
`/fhir/*` routes, whose 401/403 negatives are covered by the Phase 0/1
suites in the same green run. The SMART app holds no credentials of its
own — SMART mode uses the EHR-issued token via fhirclient; local mode
uses a user-typed dev token.

### 4. Audit assertions
Seed writes traverse `fhir.create`, which appends a hash-chained
AuditLog row per resource (existing mechanism, unmodified); the Phase
0/1 tests asserting `verify_chain` passed in the same full-suite run. No
separate seed-specific audit test was added — recorded here, not hidden.

### 5. Completeness / no-noop (live runs, actually executed)
- **Loader over real HTTP** (fresh SQLite backend, uvicorn :8123):
  run 1 → `seed: 86 created, 0 already present, 86 total`; run 2 →
  `seed: 0 created, 86 already present, 86 total` (idempotent).
  Follow-up searches: Patients total **6** (pat-1…pat-6);
  `Observation?patient=pat-1` total **11**, HbA1c points **4**;
  `Condition?patient=pat-1` **2**; `MedicationRequest?patient=pat-1` **2**.
- **SMART app local mode smoke**: seeded backend (:8124) + built app
  served by `vite preview` — `GET /` → **200** ("OpenCare — SMART on FHIR
  app"), `GET /launch.html` → **200** ("OpenCare — SMART launch"); the
  four endpoints local mode calls returned Patient **6**, Patient/pat-1
  single, Condition **2**, MedicationRequest **2**, Observation **11**.
  (A first page check failed on an IPv6-only preview bind — re-run with
  `--host 127.0.0.1` gave the 200s above; the app was not at fault.)
- Bug found by the smoke and fixed: `httpx` choked
  (`InvalidURL: Invalid port ':1]'`) on this machine's ambient
  `no_proxy` value when constructing the loader's client. The loader now
  builds its client with `trust_env=False` (a named backend must not be
  rerouted/broken by ambient proxy variables); both live runs above are
  post-fix.

### 6. Security checklist
Synthetic data only: seed patients are fabricated (`SyntheticNNN`
families, `SYN-*` identifiers, `urn:opencare:seed=synthetic` Patient
tags; fixtures invented). No secrets anywhere (dev tokens are the
backend's documented local ones). New dependencies, all open source:
`fhirclient` **3.0.0 (Apache-2.0)** — note: Apache-2.0, not MIT — plus
dev-only `vite`/`vitest`/`typescript`/`@types/node` (MIT/Apache-2.0).
SMART scope choice is documented in `src/config.ts`: SMART v2 granular
`patient/*.rs` + `launch/patient openid fhirUser`.

### 7. Data integrity
Loader ordering is dependency-first (Patients → … → Appointments);
existence is proven by GET 200 before any skip is counted; counts are
printed and asserted (86 = 6 Patient, 12 Encounter, 10 Condition,
9 MedicationRequest, 32 Observation, 6 DiagnosticReport, 5 Procedure,
6 Appointment).

### 8. Docs
`smart-app/README.md` (incl. exact fhir.epic.com registration steps),
`backend/seed/README.md`, root README (SMART app + Demo data sections,
layout), `docs/PHASES.md` reconciled with a superseded-numbering note
and an honest per-phase status table — all in this change.

## NOT RUN — with reasons
- **Live Epic-sandbox launch (fhir.epic.com): NOT RUN.** It requires a
  client_id registered under Min's own developer account — a step only
  he can do (steps in `smart-app/README.md`). Until then the authorize →
  redirect → `oauth2.ready()` path is code-complete and build-verified
  but never executed against that sandbox.
- **SMART EHR launch against a real Epic instance: NOT RUN** — same
  reason, plus a customer organisation's own app approval in production.
- **SMART Health IT launcher (launch.smarthealthit.org) live run:
  NOT RUN** from this VM (interactive third-party web flow); it is
  documented as the no-registration alternative in the app README.
- Live Keycloak run and Docker/Postgres runtime: unchanged from earlier
  phase reports — still NOT RUN anywhere.
