# Phase 1 — SQA Report

**Date:** 2026-10-15
**Scope built:** Core EHR MVP (API scope) on the Phase 0 foundation, reusing
the same repository / audit / security-dependency patterns throughout:
(1) Condition, Procedure, MedicationRequest, DiagnosticReport, Appointment
FHIR resources (POST create, GET by id, GET search) with per-resource
required-field validation as specified in `contracts/fhir-resources.md`;
(2) scheduling/ADT — Appointment search by practitioner and by date range,
plus a status-transition action with order enforcement and terminal states;
(3) staff results inbox (`GET /fhir/inbox`, Observation + DiagnosticReport,
newest first, optional status filter); (4) patient-portal read scoping
(new `patient` role + `User.patient_ref`: own-record reads, 404
non-disclosure by id, force-scoped search, `GET /portal/my-results`);
(5) OIDC auth mode (`AUTH_MODE=oidc`, PyJWT RS256 verification against a
JWKS from `OIDC_JWKS_URL`, Keycloak-shaped claim mapping) beside the
unchanged dev mode. Phase 0 behaviour and tests preserved.

## SQA gate results (per docs/SQA.md)

| Check | Result |
|---|---|
| Unit + API tests | **PASS** — full suite: `37 passed, 1 warning in 3.49s` (pytest, backend/tests/: 16 Phase 0 + 21 Phase 1; the 1 warning is the upstream Starlette/httpx TestClient deprecation notice). Phase 0 file alone re-run against the Phase 1 code before Phase 1 tests were added: `16 passed` |
| FHIR validation | **PASS (required-field level, exactly as documented)** — per-resource 422 tests: Condition missing code; Procedure missing subject; MedicationRequest missing intent and missing medication; DiagnosticReport missing status and missing code; Appointment participant-without-actor and missing status; wrong resourceType → 422; missing resource → 404; malformed search datetime → 422. Full US Core profile validation **NOT RUN** — profile validator still not wired; required-field rules only, as stated in contracts/fhir-resources.md |
| RBAC negative tests | **PASS** — viewer writes Condition/Appointment → 403; viewer transitions an Appointment → 403; viewer and patient call the staff inbox → 403; patient writes an Observation → 403; patient role on the OIDC viewer-fallback path writes → 403; unauthenticated/forged credentials → 401 (dev mode fake JWT, OIDC forged signature, OIDC expired token) |
| Audit assertions | **PASS** — Appointment transitions assert `fhir.appointment.transition` audit rows exist and `verify_chain()` returns True after creates + transitions; all Phase 1 creates flow through the Phase 0 `fhir.create` audit path (chain verification also re-asserted by the Phase 0 audit test) |
| Completeness / no-noop | **PASS** — every new route's effect is a persisted DB row observed by a test: resource round-trips read back stored bodies; transitions are re-read after the action (status persisted, `versionId` bumped to 3 after two transitions); inbox/portal bundles are assembled from stored rows with cross-patient isolation asserted; OIDC users are upserted into the users table by the same dependency the routes use. No stub routes added: Practitioner/Organization/DocumentReference remain absent (documented as planned), GraphQL/gRPC stubs unchanged and still labelled stubs |
| Security checklist | **PASS for Phase 1 scope** — one new dependency: `pyjwt[crypto]` (PyJWT 2.15.1, MIT; cryptography 50.0.2, Apache-2.0/BSD — both permissive open source), added to `backend/requirements.txt`. No secrets in code/fixtures: OIDC tests generate a throwaway RSA keypair per run and serve a fixture JWKS by stubbing the module's JWKS HTTP fetch — no network, no real keys. Synthetic data only. Patient non-disclosure (404, not 403, by id) prevents record-existence leaks; unknown OIDC roles map to viewer (least privilege) |
| Data integrity | **PASS** — creates/updates write resource + projections + audit in one session path; new projections (`patient_ref` for Appointment from participant actor, `practitioner_ref`, `status`, parsed `start_at`, `FhirResource.created_at`, `User.patient_ref`) are set on both create and update; ADT state machine rejects illegal transitions with 409 (booked→fulfilled, fulfilled→cancelled, cancelled→arrived — all tested), terminal states enforced |
| docker-compose.yml syntax | **PASS** — re-parsed with PyYAML after Phase 1: services `app`, `db`, `valkey` present. Compose file itself unchanged in Phase 1 |
| Docs | **PASS** — README (Phase 1 state, auth modes incl. the Keycloak note, patient scoping, create_all caveat), contracts/fhir-resources.md (Phase 1 tables with the exact enforced rules, search params, ADT machine, inbox, patient scoping), docs/PHASES.md (Phase 1 API scope marked done with the open items named) updated in this change. docs/SQA.md unchanged — the gate itself did not change |

## OIDC test method (for reproducibility)

`test_phase1.py` generates an RSA-2048 keypair in the test, builds a JWKS
from the public key, and monkeypatches `app.security._fetch_jwks` (the single
function that performs the JWKS HTTP fetch) to return that fixture. Tokens
are signed with PyJWT and verified by the real code path (PyJWK key
construction, RS256 signature, issuer + expiry validation). Covered: valid
token with `realm_access.roles=["clinician"]` accepted and role enforced
(write allowed); unmapped role → viewer (write 403); `patient` role +
`patient_ref` claim scoping enforced (own 200 / other 404); token signed by
a *different* key under the same `kid` → 401; expired token → 401; dev mode
behaviour unchanged (dev tokens work, JWT-shaped string → 401).
Implementation note: PyJWT 2.15 removed `RSAAlgorithm.from_jwk_data`; the
code uses `jwt.PyJWK(...).key` instead.

## Known limitations (stated honestly)

- **Live Keycloak NOT RUN.** No Keycloak server (or any external IdP) exists
  on this VM. OIDC verification is proven against the in-process fixture
  above; pointing `OIDC_JWKS_URL`/`OIDC_ISSUER` at a real Keycloak realm and
  verifying end-to-end remains Phase 1 ops work, as the README states.
- **Docker/Postgres runtime still unverified.** Docker is not installed on
  this VM, so `docker compose up` was again **not run**; all tests ran on
  SQLite — the designed portable path. Postgres-specific behaviour of the
  new columns/queries is unverified.
- **No migrations.** Schema comes from `create_all`; Phase 1 adds columns
  (`users.patient_ref`, four `fhir_resources` projections + `created_at`).
  A database created under Phase 0 must be recreated (a stale dev
  `openehr.db` failing with `no such column: users.patient_ref` was hit and
  removed during this build — recorded here, and the README now carries the
  caveat). Alembic wiring is still future work.
- **Full US Core profile validation NOT claimed** — only the required-field
  rules listed in `contracts/fhir-resources.md` are enforced. Appointment
  `participant.status`, terminology binding checks, and reference-existence
  checks (e.g. that a referenced Patient actually exists) are not validated.
- **Patient portal is API-only in this delivery** — the scoping and
  `/portal/my-results` endpoint are implemented and tested; the web portal
  views from the original Phase 1 text do not exist yet. No other UI either.
- Date-range search compares the parsed Appointment `start`; appointments
  stored without a parseable `start` simply never match a date filter.
- OIDC JWKS is fetched through one cache-per-URL fetch function; key
  rotation is handled by process restart / cache lifetime of the deployment,
  not by a refresh policy in code.
