# Phases — reconciled to the ORIGINAL plan (2026-10-15)

Each phase ships end-to-end and passes the SQA gate in `docs/SQA.md`, with a
report in `docs/sqa/`, before the next phase starts.

> **Superseded numbering:** this codebase was initially built foundation-first
> and the phases were renumbered twice along the way (foundation called
> "Phase 0" with the SMART app as "Phase 1a"; then the Agentic ADK layer
> inserted as "Phase 2", pushing clinical phases to 3–5). **All of that is
> superseded.** Min re-asserted the original plan from
> `Open-Source-Epic-Like-EHR-Build-Plan.md`; the table below is the single
> source of truth for phase numbers and status.

| Phase | Scope (original plan) | Status |
|---|---|---|
| **0** | SMART on FHIR companion app launching inside the Epic sandbox: patient summary, conditions, meds, lab trends | **DONE 2026-10-15** (`smart-app/`, fhirclient + Vite/TS, SMART mode + local demo mode; unit tests + build + local smoke green). **Live Epic-sandbox launch NOT RUN** — needs Min's client_id registration (see `smart-app/README.md`) |
| **1** | Core EHR MVP: auth/RBAC + audit, FHIR chart (Patient/Encounter + Condition/Procedure/MedicationRequest/DiagnosticReport/Appointment), scheduling & ADT, results inbox, patient-portal read scoping, **Synthea demo data**, Docker one-command run | **DONE 2026-10-15** — foundation + core API (docs/sqa/PHASE0_SQA_REPORT.md + PHASE1_SQA_REPORT.md under the old numbering) and the seed (`backend/seed/`, idempotent loader, live-verified) in docs/sqa/PHASE_SMART_SYNTHEA_SQA_REPORT.md. Open items, not claimed: live Keycloak run, full US Core profile validation in CI, Practitioner/Organization/DocumentReference resources |
| **2** | Lab LIS: order → specimen → result → QC workflow, analyser ingest (HL7 v2 ORU / CSV), reference ranges, critical-value flags | **DONE 2026-10-15** — `backend/app/lab.py` + web Laboratory page, built as Studio configuration + pack as directed: catalog (panels/analytes/ranges/critical thresholds) is published `terminology/lab-catalog`, the order→specimen→result→QC machine is published `workflow/lab-order-flow` (both seeded v1, config-driven behaviour proven by tests). Two-step verify → release governance with the `lab-unreleased` visibility tag (drafts invisible to inbox/portal/patient reads until release), QC gate (latest failed QC per analyte blocks verify/release with 409 until a passing run), HL7 v2 ORU^R01 + CSV ingest creating drafts only. Lab pack graduated to v2.0.0 with the real surface. SQA: `docs/sqa/PHASE2_SQA_REPORT.md` (backend 85 tests, web 37 tests, live HTTP smoke). Open items, not claimed: live analyser interface, US Core Lab profile validation, age/sex-specific ranges |
| **3** | Analytics warehouse: FHIR → OMOP CDM (dbt), population registry, risk lists, self-service explorer | **DONE 2026-10-15** — transactional outbox (`backend/app/outbox.py`, one central `before_flush` hook writing the event in the same transaction as every watched resource create/update), OMOP CDM subset in a second database (`backend/app/warehouse.py`: person, visit_occurrence, condition_occurrence, measurement, observation, drug_exposure, procedure_occurrence + curated `concept_map` — standard OMOP concepts where established, local ≥2e9 concepts elsewhere, unmapped → 0 and counted in coverage), idempotent fault-tolerant ETL (`backend/app/etl.py`, CLI + admin endpoint, dead-letter with recorded error), dbt project (`warehouse/dbt/`: staging views + `diabetes_registry` mart), aggregate-only population health (`backend/app/pophealth.py`: registries as Studio `cohort` config — seeded diabetes + hypertension — member counts, % latest lab in range, gender/age-band stratification with small-cell suppression <5, cohort explorer; admin/clinician only) + web Population health page. SQA: `docs/sqa/PHASE3_SQA_REPORT.md` (backend 102 tests, web 44 tests; ETL verified end-to-end on the seed incl. into a DuckDB warehouse via the CLI). Open items, not claimed: dbt build — **VERIFIED 2026-10-15 by parent re-run** (seed → ETL into DuckDB → `dbt build` PASS 11/11: 6 staging views, diabetes_registry mart, 4 data tests; see the addendum in `docs/sqa/PHASE3_SQA_REPORT.md`); Postgres-target dbt run still NOT RUN; risk lists (named in the original scope) are covered only in the form of registries + explorer, no separate risk-scoring list; full ATHENA vocabulary load (concept map is the documented curated subset); Postgres warehouse runtime (same no-Docker caveat as prior phases) |
| **4** | Revenue + specialties, one at a time, only if 1–3 land: billing/claims, pharmacy dispensing + barcode med-admin, ED board, radiology (DICOMweb + open viewer) | **DONE 2026-10-15** — split into 4A / 4B (below), both DONE and SQA-gated; **Phase 4 closes the original plan** |
| **4A** | Billing/claims + pharmacy: charge capture from a Studio-config fee schedule (unpriced codes flagged, never dropped), config-driven claim lifecycle with one-time appeal, X12 837P-format export (no clearinghouse), formulary as Studio config + stock-controlled dispensing, BCMA-lite med-admin (right patient/medication/dispense) | **DONE 2026-10-15** — `backend/app/billing.py` + `backend/app/pharmacy.py`, web Billing + Pharmacy pages. Fee schedule = published `terminology/fee-schedule`, lifecycle = published `workflow/claim-lifecycle`, formulary = published `terminology/formulary` (all seeded v1; config-driven pricing + lifecycle proven by tests). Claims are FHIR Claim resources (patient scoping via the `patient.reference` projection); dispensing decrements the server-side `pharmacy_stock` counter (oversell = 409, nothing written) and flips the request `active → dispensed`. SQA: `docs/sqa/PHASE4A_SQA_REPORT.md` (backend 127 tests, web 62 tests, live HTTP smoke 16/16). Open items, not claimed: clearinghouse submission / 835 remittance, Coverage resource, right-dose/right-time BCMA checks |
| **4B** | Specialties, one at a time: ED board, radiology (DICOMweb + open viewer), surgery board | **DONE 2026-10-15** — `backend/app/boards.py` + web ED board / Radiology / Surgery board pages, all on Studio configuration: ED tracking machine = published `workflow/ed-flow` (ESI 1–5 triage validation, wait times computed server-side from the recorded arrival, admit creates a `partOf`-linked inpatient Encounter, depart/admit are governed exits); radiology = ImagingStudy on the generic surface created from a ServiceRequest with the modality validated against published `terminology/imaging-modalities`, DICOMweb (QIDO-RS-shaped) study-level DICOM JSON with UID round-trip, report flow preliminary → final gated by the `radiology.verify` capability, and an external OHIF-style viewer link built from the new `integration/dicom-viewer` config kind (`viewer.baseUrl`) — **metadata-only studies: no pixel data, no embedded viewer, stated in UI + docs**; surgery = Procedure + linked Appointment cases with rooms as published `terminology/or-rooms`, case machine = published `workflow/surgery-flow`, day board grouped by room, and room double-booking refused server-side (409) on create AND reschedule against the real stored times. SQA: `docs/sqa/PHASE4B_SQA_REPORT.md` (backend 139 tests, web 81 tests, live HTTP smoke 28/28). Open items, not claimed: real DICOM pixel-data ingest / series + instances / WADO-RS retrieval, an embedded viewer, ED age/sex-specific triage protocols, Postgres runtime (same no-Docker caveat as prior phases) |

## Workstreams delivered alongside the phases (not clinical phases)

- **Studio customization engine (DONE 2026-10-15, after Phase 1, before
  Phase 2):** versioned Studio configuration in `backend/app/studio_config.py`
  + `app/configstore.py` (spec: `docs/STUDIO_CUSTOMIZATION.md`) — kinds
  `form` (FHIR Questionnaire-shaped), `workflow`, `terminology` (panels +
  reference ranges as data), `navigation` (modules-only menu rule, 400 on
  page/form entries), `branding`, `permissions`; draft → publish lifecycle
  with immutable published versions, supersede, disable, rollback, structural
  diff, and hash-chained audit on every transition. Consumed by the core for
  real: the Appointment ADT machine runs from the published
  `workflow/appointment-adt` config (seeded v1 at startup), FHIR write/inbox
  gates are capabilities merged from the published `permissions/role-matrix`
  config and enforced in the API dependency, and `QuestionnaireResponse`
  writes validate against the published form. The web Studio page is a
  full tabbed System Studio module (visual editors per kind, pack
  lifecycle, audit view — built 2026-10-16, SQA:
  `docs/sqa/STUDIO_UI_SQA_REPORT.md`). SQA: `docs/sqa/PHASE_STUDIO_SQA_REPORT.md`
  (backend 61 tests, web 29 tests, both suites green).
- **Realignment (2026-10-16):** the audit in `docs/sqa/REALIGNMENT_AUDIT.md`
  found the backend clinical core genuinely config-driven but the web
  shell consuming zero config at runtime, the permissions matrix
  governing only some writes, and pack enablement gating nothing. The
  realignment closed the consumption gaps: the shell renders navigation,
  branding and capability gating from the published configs (seeded
  defaults preserve the previous behaviour exactly); every backend gate
  is a capability check, including Studio's own APIs; all five workflow
  machines honour per-transition `roles`; governed/active state sets,
  claim exportable states + appeal limit, the ESI range, BCMA rights and
  QC policy parameters moved into their config payloads; and released
  critical lab results gained a tracked notification lifecycle
  (pending → notified → acknowledged). SQA:
  `docs/sqa/REALIGNMENT_SQA_REPORT.md` (backend 165 tests, web 135
  tests, both suites green).
- **Client deliverables (alongside Phase 1):** the web app (`web/` —
  clinician workspace, patient portal, System Studio) and the native
  mobile sources (`mobile/` — SwiftUI iOS Universal + Kotlin Compose
  Android + KMP shared models). Each carries its own SQA report in
  `docs/sqa/`. Mobile completion pass (2026-10-15): Android — Gradle
  wrapper committed, JDK/SDK toolchain installed on the build VM, but
  the Gradle build is BLOCKED there by the sandbox's `other_tcp`=Deny
  permission (all JVM TCP intercepted; see the mobile SQA report for
  evidence and the user-side fix), so its first real compile is the
  CI Android job or Android Studio; iOS — CI-defined (shared scheme
  committed; macOS-runner xcodebuild in `.github/workflows/mobile.yml`,
  staged for the repo root), with Swift parse (12/12) and core-layer
  typecheck passed on the VM — first real compile still pending.
- **Agentic ADK — lives in Agentic_App, NOT in Open_EHR (Min's decision,
  2026-10-15):** the multimodal agentic ADK was built into Agentic_App's
  System Studio (see that project's `docs/ADK_SQA_REPORT.md`). The
  `agents/` folder in this repo is an earlier PARKED draft (built before
  that decision): it is not part of any phase, is not published, and is
  kept only as reference. Its design (Google ADK roster, read-only FHIR
  tools, guardrail callback, draft-proposal human-confirm write path) is
  documented in `docs/AGENTS.md`.

## Studio-reuse mapping (concepts adapted from Agentic_App's System Studio)

| System Studio concept | OpenCare adaptation (original code) |
|---|---|
| Versioned module packs | `backend/packs/*.pack.json` manifests, `(name, version)` unique in DB |
| Instantiation = designer draft only | Install creates `installed_draft` and nothing else (governance bookkeeping: module routes themselves are live from first boot — install state does not gate them; see the known gap in `docs/sqa/REALIGNMENT_SQA_REPORT.md`) |
| Approve, then attach, as separate human steps | `approve` requires `installed_draft`; `attach` requires `approved`; out-of-order = 409 |
| Backend surface ships disabled until enabled | **Not implemented as gating (corrected 2026-10-16):** manifests declare endpoints `"enabled": false`, but no code path reads that flag; `enable` (requires `attached`) is a separate audited governance action only. Enablement gating is a documented known gap with a design note in the realignment SQA report |
| Role gates | Studio actions require `admin`/`configurator` (403 for clinician/viewer, tested) |
| Audit logging on studio actions | Every transition + install writes a hash-chained AuditLog row (tested) |
| No mocks, real DB | SQLAlchemy 2.0 against SQLite locally / PostgreSQL in Compose |
