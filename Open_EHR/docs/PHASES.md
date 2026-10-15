# Phases

Each phase ships end-to-end and passes the SQA gate in `docs/SQA.md`, with a
report in `docs/sqa/`, before the next phase starts.

## Phase 0 — Foundation — DONE (see docs/sqa/PHASE0_SQA_REPORT.md)
Repo scaffold; FastAPI + SQLAlchemy 2.0 core; Postgres (pgvector) + Valkey in
Compose, SQLite locally (portable repository design); Studio pack lifecycle;
hash-chained audit; FHIR Patient/Encounter/Observation; contracts stubs;
pytest suite green.
**Note on naming:** the earlier build plan used "Phase 0" for a SMART-on-FHIR
sandbox companion app. That app remains a planned fast-follow (it needs the
foundation's FHIR surface to be meaningful as our own code) — the foundation
delivered here is Phase 0 of the codebase, and the sandbox app is tracked as
Phase 1a below.

## Phase 1 — Core EHR MVP — **API scope DONE 2026-10-15** (see docs/sqa/PHASE1_SQA_REPORT.md)
Delivered: Condition/Procedure/MedicationRequest/DiagnosticReport/Appointment
resources through the Phase 0 repository/audit path, with per-resource
required-field validation (contracts/fhir-resources.md); scheduling & ADT
(Appointment search by practitioner + date range; status transitions with
order enforcement and terminal states, audited); staff results inbox
(Observation + DiagnosticReport, newest first); patient-portal read scoping
(patient role + `patient_ref`, own-record only, portal `/my-results`);
OIDC auth mode (`AUTH_MODE=oidc`, PyJWT/JWKS verification, Keycloak-shaped
claim mapping) alongside the unchanged dev mode.
**Still open in Phase 1 (not claimed):** patient portal *web* views and the
other web UI; a live Keycloak server run + verification (OIDC is tested with
in-process signed tokens + fixture JWKS); full US Core profile validation in
CI (required-field rules are implemented; the profile validator is not wired).
Practitioner/Organization/DocumentReference resources remain planned.
**Phase 1a (parallel, small):** SMART on FHIR companion app against a public
FHIR sandbox using this codebase's FHIR client shapes.

## Phase 2 — Lab LIS
ServiceRequest → Specimen → result → QC workflow, analyser file ingest
(HL7 v2 ORU / CSV) via the Go integration tier, reference ranges,
critical-value flags. Lab pack manifest graduates from draft content to real
routes — through the Studio lifecycle, never by editing the manifest flag.

## Phase 3 — Analytics warehouse
Transactional outbox → FHIR → OMOP CDM (dbt), population registry, self-service
explorer. Go bulk-export API lands here.

## Phase 4 — Revenue + specialties (one at a time, cut rather than half-ship)
Billing/claims (X12), pharmacy + barcode med-admin, ED board, radiology
(DICOMweb + open-source viewer), surgery board. Go realtime tier and Rust
compute services (DICOM, de-identification, terminology) land with the
features that need them.

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
