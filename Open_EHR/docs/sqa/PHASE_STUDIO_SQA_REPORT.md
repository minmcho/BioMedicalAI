# Studio Customization Engine — SQA Report

**Date:** 2026-10-15
**Scope built:** The Studio customization engine directed by Min (Open_EHR
customizable like Agentic_App's System Studio), per
`docs/STUDIO_CUSTOMIZATION.md`, scheduled immediately after Phase 1 closes
and before Phase 2 (Lab LIS):

1. **Versioned config store** (`backend/app/studio_config.py`,
   `app/configstore.py`, `StudioConfig` model) in the same portable
   SQLAlchemy layer as the chart: `(kind, key, version)` unique; statuses
   `draft / published / superseded / disabled`; at most one published
   version per object — that row is the effective config.
2. **Governance:** published versions are immutable (direct edit → 409;
   editing spawns a new draft version); publish supersedes the previous
   published version (kept for rollback); rollback republishes an earlier
   payload as a NEW version (rollback to a never-published draft → 422);
   disable removes the effective config; a structural diff endpoint
   (`.../diff?from=&to=`); every create/update/publish/disable/rollback
   writes a hash-chained AuditLog row.
3. **Kinds, all payload-validated server-side:** `form` (FHIR
   Questionnaire-shaped: unique linkIds, typed items, required flags,
   valueSet refs), `workflow` (states + transitions with from/to/action/
   roles, transitions must reference declared states), `terminology`
   (concepts with code/display/active + optional `referenceRange` — lab
   panels and ranges as data), `navigation` (label/module/roles/order/
   visible; **modules-only rule**: page/form entries → **400** with the
   rule stated), `branding` (orgName, portalTitle, logoUrl, #RRGGBB
   primaryColor), `permissions` (capability → role → boolean overrides;
   unknown capability or role → 422).
4. **API:** `/studio/config` CRUD + publish/disable/rollback +
   `GET /studio/config/{kind}/effective[/{key}]` (published only). Writes
   require admin/configurator; clinician/viewer read published config only
   (drafts return 404 to them — designer data); patient is excluded
   entirely. Unknown kind → 404; invalid payload → 422 with details.
5. **Config consumed by the core, for real:**
   - The Appointment ADT endpoint loads its state machine from the
     published `workflow/appointment-adt` config, seeded as published v1 at
     startup (`ensure_default_configs`) with exactly the Phase 1
     transitions; the hardcoded map remains only as a fallback if that
     config is disabled.
   - FHIR create/transition/inbox routes are gated by capabilities
     (`app.security.require_capability`): built-in defaults merged with
     the published `permissions/role-matrix` config, enforced in the API
     dependency.
   - `POST /fhir/QuestionnaireResponse` validates the referenced Studio
     form is **published** and that every required form item carries an
     answer (422 otherwise), then stores through the existing resource
     layer (read/search/patient-scoping inherited).
6. **Web:** the Studio page gained a **Studio configuration** section
   (packs UI untouched): config objects grouped by kind/key with full
   version history, payload viewer, draft editor (create + edit draft),
   and publish / disable / roll back actions — all through the real
   `/studio/config` API (`web/src/api/client.ts` config methods,
   `web/src/pages/StudioPage.tsx`).

## SQA gate results (per docs/SQA.md)

| Check | Result |
|---|---|
| Unit + API tests | **PASS** — backend full suite: `61 passed, 1 warning in 9.16s` (39 pre-existing Phase 0/1/seed tests unchanged and green + 22 new in `backend/tests/test_studio_config.py`, which alone runs `22 passed`). Web: `Test Files 5 passed (5), Tests 29 passed (29)` (22 pre-existing + 7 new in `src/api/studioConfig.test.ts`); `npm run build` exit 0 (JS 250.67 kB / gzip 77.22 kB). The 1 pytest warning is the upstream Starlette/httpx TestClient deprecation notice |
| FHIR validation | **PASS (required-field level, as documented)** — config payload 422 tests per kind (empty form items, bad item type, bad colour, transition to undeclared state, concept missing display, unknown capability); QuestionnaireResponse negative tests: missing `questionnaire`, draft-only form ("not published"), missing required answer, empty answer list — all 422. Navigation modules-only violations return **400** with the rule in the detail (tested for `type: "page"`, a `form` field, and a module-less item) |
| RBAC negative tests | **PASS** — config create: unauthenticated → 401, clinician → 403, patient → 403; publish as viewer → 403; patient on effective reads → 403; clinician reading a draft version → 404; QuestionnaireResponse create as viewer → 403. Capability enforcement: viewer write 403 by default, clinician inbox 403 by default |
| Audit assertions | **PASS** — `test_config_actions_are_audited` asserts `studio.config.create` / `publish` / `disable` rows exist and `verify_chain()` returns True; the seeded ADT workflow writes `studio.config.create` + `publish` rows as actor `system` at startup; ADT + FHIR writes keep their existing audited paths |
| Completeness / no-noop | **PASS** — every route's effect is a persisted row observed by a later read in tests: lifecycle is asserted through history + effective endpoints (draft invisible → published → superseded → rolled back v3 with v1's payload); the ADT machine change is proven by behaviour (publishing a v2 workflow that adds `booked → noshow` and drops `arrived → cancelled` flips those transitions 409 → 200 and 200 → 409; disabling the config entirely restores the fallback machine); the permissions override is proven by real API outcomes (publishing `fhir.write: {viewer: true}` flips a viewer Patient create 403 → 201; publishing `fhir.write: {clinician: false}` flips a clinician create to 403 while admin still writes; `fhir.inbox: {viewer: true}` flips the inbox 403 → 200); QuestionnaireResponses round-trip through GET by id and patient-scoped search |
| Security checklist | **PASS** — no new dependencies (backend or web). No secrets in code/fixtures; synthetic data only (branding/terminology fixtures are fabricated). Payloads validated at the boundary in `app/configstore.py`; 422/400 details name the offending field, never internals. Draft config is non-disclosed to clinician/viewer (404, not 403) |
| Data integrity | **PASS** — lifecycle transitions are transactional (row mutation + audit in one session, committed together); illegal lifecycle actions rejected: duplicate open draft → 409, edit of a published/superseded version → 409, publish of an already-published version → 409, publish of a superseded version → 409 (directed to rollback), disable of a non-published version → 409 — all tested. ADT-from-config keeps the Phase 1 guarantees: illegal transition → 409, unknown status → 422 (both re-tested under config) |
| Docs | **PASS** — README (customization engine section, web Studio section, layout), `contracts/fhir-resources.md` (QuestionnaireResponse rules, config-driven ADT note, Studio config API contract), `docs/PHASES.md` (engine recorded as a delivered workstream; Phase 2 prerequisite marked done) updated in this change |

## NOT RUN / not claimed (honest list)

- **PostgreSQL runtime NOT RUN.** The config store uses only portable
  types (JSON column, standard enums/constraints) on the same SQLAlchemy
  layer as the rest of the backend, but — as in every prior phase — the
  suite ran on SQLite; Docker is not installed on this VM, so no
  Postgres-backed run exists yet.
- **Browser click-through of the web config section NOT RUN.** The section
  is covered by client unit tests (request shaping for every config
  method, grouping/action helpers) plus the backend API tests it calls;
  no browser automation ran in this environment.
- **Spec items 2 and 8 are named gaps, not silently implied.** Custom
  fields as organisation-defined FHIR extensions on core resources
  (spec §2) and integration-endpoint config (spec §8) are NOT implemented
  in this pass; the engine covers spec items 1, 3, 4, 5, 6, 7 and 9. Per
  the spec's own rule, these stay flagged as engine gaps to extend —
  Lab LIS (Phase 2) needs only the delivered kinds (forms, panels/ranges
  terminology, workflow), which is why it can proceed.
- **Config diff is API-only.** The diff endpoint is tested; the web UI
  offers side-by-side payload viewing but no rendered diff view yet.
- **Live Keycloak / OIDC unchanged** — config endpoints sit behind the
  same auth boundary as everything else; no new live-IdP verification was
  run (still NOT RUN, as recorded in the Phase 1 report).
