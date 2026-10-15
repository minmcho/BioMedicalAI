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
| **2** | Lab LIS: order → specimen → result → QC workflow, analyser ingest (HL7 v2 ORU / CSV), reference ranges, critical-value flags | **NEXT** — not started. (Min has also directed a Studio customization engine to be built immediately after Phase 1 closes and before Lab LIS, so Lab lands as Studio configuration + packs — spec: `docs/STUDIO_CUSTOMIZATION.md`) |
| **3** | Analytics warehouse: FHIR → OMOP CDM (dbt), population registry, risk lists, self-service explorer | Not started |
| **4** | Revenue + specialties, one at a time, only if 1–3 land: billing/claims, pharmacy dispensing + barcode med-admin, ED board, radiology (DICOMweb + open viewer) | Not started |

## Workstreams delivered alongside the phases (not clinical phases)

- **Client deliverables (alongside Phase 1):** the web app (`web/` —
  clinician workspace, patient portal, Studio packs UI) and the native
  mobile sources (`mobile/` — SwiftUI iOS Universal + Kotlin Compose
  Android + KMP shared models). Each carries its own SQA report in
  `docs/sqa/`; mobile first builds happen in Xcode / Android Studio on
  Min's Macs.
- **Agentic ADK layer (`agents/`, DONE 2026-10-15):** a separate service on
  the real Google ADK — LlmAgent roster, read-only FHIR tools, guardrail
  callback, draft-proposal human-confirm write path, multimodal intake.
  Full detail: `docs/AGENTS.md`; SQA: `docs/sqa/PHASE2_AGENTS_SQA_REPORT.md`
  (report filename kept from the superseded numbering). Agent reasoning
  quality / evals are NOT RUN (no model on the build VM).

## Studio-reuse mapping (concepts adapted from Agentic_App's System Studio)

| System Studio concept | OpenCare adaptation (original code) |
|---|---|
| Versioned module packs | `backend/packs/*.pack.json` manifests, `(name, version)` unique in DB |
| Instantiation = designer draft only | Install creates `installed_draft` and nothing else; no routes go live |
| Approve, then attach, as separate human steps | `approve` requires `installed_draft`; `attach` requires `approved`; out-of-order = 409 |
| Backend surface ships disabled until enabled | Pack manifests declare endpoints `"enabled": false`; `enable` requires `attached` and is a separate audited action |
| Role gates | Studio actions require `admin`/`configurator` (403 for clinician/viewer, tested) |
| Audit logging on studio actions | Every transition + install writes a hash-chained AuditLog row (tested) |
| No mocks, real DB | SQLAlchemy 2.0 against SQLite locally / PostgreSQL in Compose |
