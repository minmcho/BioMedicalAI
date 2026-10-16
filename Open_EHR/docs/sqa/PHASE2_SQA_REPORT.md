# Phase 2 — Lab LIS — SQA Report

**Date:** 2026-10-15
**Scope built:** The Laboratory Information System from the original
plan (Phase 2: order → specimen → result → QC, analyser ingest,
reference ranges, critical-value flags), built ON the Studio
customization engine as Min directed — site-specific behaviour is
published Studio configuration, not hardcoded:

1. **Catalog as Studio config** (`terminology/lab-catalog`, seeded
   published v1 by `app.lab.ensure_lab_defaults`): LOINC panels (CBC
   `58410-2`, BMP `51990-0`) and analytes, each with unit and a
   `referenceRange` of `{low, high, criticalLow, criticalHigh, unit}`.
   The terminology validator was extended to type-check the critical
   bounds. Order placement validates against the published catalog:
   unknown code → 422, inactive code → 422.
2. **Workflow as Studio config** (`workflow/lab-order-flow`, seeded
   published v1): `ordered → collected → received → in-process →
   resulted → verified → released`, plus rejection from collected /
   received / in-process and `ordered → cancelled`. The published config
   is the live machine (a code constant is fallback-only, mirroring the
   ADT pattern). Collection creates the linked **Specimen** with a
   server-generated accession id; the specimen mirrors order states;
   rejection requires a reason (422 without) and records it on both
   order (`statusReason`) and specimen (`note`); illegal transitions →
   409; `resulted`/`verified`/`released` are governed states — a bare
   transition into them → 422 directing to the proper endpoints.
3. **Results** — manual entry (`POST /lab/orders/{id}/results`) and
   analyser ingest (`POST /lab/ingest`: HL7 v2 ORU^R01 via a small
   dependency-free parser reading PID-3/OBR-2/3/OBX-3/5/6, plus a CSV
   fallback) share one creation path. Panel orders accept only their
   component analytes; non-numeric values, unknown/inactive/panel codes
   → 422; results before specimen receipt → 409. Interpretation is
   computed from the configured ranges (critical LL/HH first, then
   L/H, else N; HL7 v3 ObservationInterpretation) and the applied range
   is stored on the Observation; reports with a critical value carry
   the `urn:opencare:extension/critical-result` extension.
4. **Two-step governance** — results are `preliminary` and carry the
   `lab-unreleased` meta tag (`urn:opencare:visibility`): excluded from
   `GET /fhir/inbox` and `GET /portal/my-results`, excluded from
   patient searches, and patient by-id reads → 404. **Verify**
   (`lab.verify`: admin/clinician — configurator excluded by default)
   moves the report to `final` and the order to `verified`; **release**
   makes observations `final`, strips the tag (the moment results
   appear in inbox + portal), stamps `issued`, and completes the
   order/specimen. Nothing is ever auto-released — ingest creates
   drafts in exactly the same way (tested).
5. **QC** — `LabQcRun` records (own table; QC is control-material data,
   not a patient resource) with `passed` computed server-side.
   Verify/release enforce the gate: the LATEST run per analyte decides;
   a failed latest run blocks with 409 naming the analyte until a
   passing run exists after it. The verification queue and
   `GET /lab/qc/status` expose the block live. An unrelated analyte's
   failure does not block (tested).
6. **Generic FHIR surface** — `ServiceRequest`, `Specimen`, `Task`
   added to `app.fhir.SUPPORTED` with required-field validation and
   projections (`Task.for` projects the patient), inheriting create /
   read / search / patient scoping.
7. **Web** — a Laboratory page (`web/src/pages/LabPage.tsx`): new-order
   form against the catalog, order queue with lifecycle actions
   (incl. rejection-reason input), specimen tracking, result entry with
   configured ranges shown, analyser ingest (paste + file), the
   verification queue with CRITICAL and QC-blocked badges and
   verify/release buttons (disabled while QC-blocked), and a QC page
   (record runs, gate banner). New `ApiClient` lab methods
   (`web/src/api/client.ts`). Viewer sees the page read-only; patient
   never sees it (nav + server both deny).
8. **Pack graduated** — `backend/packs/lab.pack.json` → v2.0.0
   describing the real modules/forms/endpoints. Entries keep
   `"enabled": false` per the platform convention: pack manifests
   declare the surface, and the human install → approve → attach →
   enable lifecycle flips it (the designed lifecycle per docs/SQA.md,
   identical to clinical-core).
9. **Seed** — `backend/seed/lab_demo.py`: scenario through the real
   API (released CBC for pat-1; critical-potassium BMP for pat-2 left
   QC-blocked), scenario-level idempotent, tested.

## SQA gate results (per docs/SQA.md)

| Check | Result |
|---|---|
| Unit + API tests | **PASS** — backend full suite: `85 passed, 1 warning in 16.46s` = 61 pre-existing (Phases 0/1/seed/Studio) unchanged and green + 23 new in `backend/tests/test_lab.py` + 1 in `backend/tests/test_lab_demo.py`. Web: `Test Files 7 passed (7), Tests 37 passed (37)` = 29 pre-existing (3 `session.test.ts` expectations updated for the new Laboratory nav item — behaviour change, recorded here) + 5 new in `src/api/labClient.test.ts` + 3 new in `src/pages/LabPage.test.tsx`. `npm run build` exit 0 (JS 266.52 kB / gzip 81.14 kB). The 1 pytest warning is the upstream Starlette/httpx TestClient deprecation notice |
| FHIR validation | **PASS (required-field level, as documented)** — ServiceRequest missing intent/code/subject → 422, Specimen missing subject → 422, Task missing intent → 422 (generic surface); lab order unknown/inactive code → 422; result-entry negatives (code outside panel, panel code as result, unknown analyte, non-numeric value, empty list) → 422; ingest negatives (non-HL7 content, non-numeric OBX, patient mismatch, unknown order, bad CSV header/value) → 422; QC negatives (unknown analyte, low > high, missing control material) → 422 |
| RBAC negative tests | **PASS** — order placement: unauthenticated → 401, viewer → 403, patient → 403 (configurator → 201 per the `lab.order` default); transitions as viewer → 403; verify as configurator → 403 (the `lab.verify` default deliberately excludes the systems role from the clinical sign-off), viewer → 403, unauthenticated → 401; ingest as viewer → 403; QC create as viewer → 403 / unauthenticated → 401; `/lab/orders`, `/lab/verification-queue`, `/lab/qc`, `/lab/catalog` as patient → 403; generic-surface writes as patient → 403 |
| Audit assertions | **PASS** — `test_lab_actions_are_audited_and_chain_verifies` asserts `lab.order.transition`, `lab.results.create`, `lab.qc.run`, `lab.report.verify` and `lab.report.release` rows exist and `verify_chain()` returns True; results/specimens/orders also write the standard `fhir.create` rows through the shared resource layer; the seeded catalog + workflow write `studio.config.create`/`publish` rows as actor `system` |
| Completeness / no-noop | **PASS** — every route's effect is a persisted row observed by later reads in tests, including a **live HTTP smoke** (real uvicorn, temp SQLite): seed loaded 86 resources; `lab_demo.py` over HTTP created the scenario ("released report …; report … left QC-blocked on analyte 2823-3"); `GET /lab/verification-queue` showed the BMP report preliminary with the critical potassium; `GET /portal/my-results` (patient pat-1) contained exactly the released CBC (1 final report + 4 final observations) among its 17 entries; `GET /lab/qc/status?analyte_code=2823-3` returned `blocked: true` with the failing run. QC gate proven behaviourally in both directions (fail blocks verify AND release; a later pass unblocks; unrelated analyte does not block). Config-driven behaviour proven: publishing catalog v2 with potassium `criticalHigh` 5.5 reinterprets the same 5.8 value H → HH; publishing workflow v2 without `collected → received` makes that transition 409. Ingest non-release proven: after HL7 ingest the report is preliminary, queue holds it, portal does not |
| Security checklist | **PASS** — no new dependencies anywhere (HL7 parser is hand-written stdlib; CSV via stdlib `csv`). No secrets in code or fixtures; synthetic data only. Inputs validated at the boundary in `app.lab` + the catalog validators; error details name fields/states, never internals. Patient non-disclosure extended, not weakened: unreleased results are 404 (by-id) / excluded (search, inbox, portal) for patients, and staff draft access exists only via the role-gated `/lab` surface |
| Data integrity | **PASS** — lifecycle/state machines reject out-of-order work with 409 and tests: transitions skipping states, transitions out of terminal states, results before receipt, second result entry, release before verify, double verify/release, verify of a non-lab report → 422. Writes go through the shared transactional resource layer (resource + projections + audit commit together); verify/release mutate report + observations + order + specimen and their audit row in one commit |
| Docs | **PASS** — README (current-state line, new "Lab LIS (Phase 2)" section, Laboratory bullet in the web section, lab demo note in Demo data, Layout), `contracts/fhir-resources.md` (Phase 2 section: resource table, full `/lab` contract incl. visibility tag, interpretation and QC-gate rules; Planned list updated), `docs/PHASES.md` (Phase 2 → DONE with evidence + open items; Phase 3 → NEXT), lab pack v2.0.0 — all updated in this change |

## NOT RUN / not claimed (honest list)

- **PostgreSQL runtime NOT RUN.** The lab module uses the same portable
  SQLAlchemy layer (JSON bodies, one new table with portable
  Float/Boolean/String columns); as in every prior phase the suite ran
  on SQLite — Docker is not installed on this VM.
- **Live analyser / MLLP NOT RUN.** Ingest is exercised with synthetic
  HL7 v2 ORU^R01 messages and CSV payloads over the HTTP API in tests
  (plus the live-server smoke used the demo script, not an analyser).
  There is no MLLP socket listener and no real analyser was connected.
- **Browser click-through of the Laboratory page NOT RUN.** The page is
  covered by component tests (render of queue/verification/specimen/QC
  states, verify click-through to the client method with the QC-block
  disabling the button, viewer read-only rendering) and client
  request-shaping tests; no browser automation exists in this
  environment. The backend behaviour the page drives is fully
  API-tested.
- **Live Keycloak / OIDC unchanged** — `/lab` sits behind the same auth
  boundary; no new live-IdP verification was run (still NOT RUN, as
  recorded in earlier reports).
- **Reference ranges are age/sex-agnostic in v1** — one range per
  analyte from config, as the phase spec allowed; paediatric /
  sex-specific range selection is a named future extension (the config
  shape can carry additional range entries when a consumer exists).
- **US Core Laboratory profile validation NOT RUN** — validation is at
  the documented required-field level; Observation/DiagnosticReport
  carry LOINC coding, category and interpretation in US Core shape, but
  no profile validator is wired in CI (same standing item as Phases
  0/1).
- **Specimen state vocabulary** — Specimen `status` carries the LIS
  workflow states (`collected/received/in-process/resulted/…`) rather
  than the base FHIR Specimen status enum; this is this system's
  documented profile choice for the LIS (contracts updated), made so
  the specimen mirrors the order machine losslessly.
