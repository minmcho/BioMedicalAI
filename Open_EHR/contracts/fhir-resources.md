# FHIR R4 resources and profiles used

Version: FHIR R4, aligned to US Core profiles where a profile exists
(profile URLs are referenced in resource `meta.profile` by producers; the
server stores resources as validated JSON and does **not** yet run full US
Core profile validation — see docs/SQA.md and the phase SQA reports).
Validation is at the level of: `resourceType` match + the exact
required-field rules in the tables below, which follow the spirit of US Core
must-support elements. Anything beyond these rules is not claimed.

## Phase 0 (implemented)
| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| Patient | Demographics / identity | `name` or `identifier` |
| Encounter | Visit / contact with care | `subject.reference` |
| Observation | Labs, vitals, measurements | `code`, `subject.reference` |

## Phase 1 (implemented)
| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| Condition | Problem list | `code`, `subject.reference` |
| Procedure | Procedures performed | `code`, `subject.reference` |
| MedicationRequest | Prescriptions / orders | (`medicationCodeableConcept` or `medicationReference`), `subject.reference`, `intent` |
| DiagnosticReport | Lab / imaging reports | `code`, `status` (`subject.reference` optional; a report without one has no patient projection and never appears in an inbox/portal result set) |
| Appointment | Scheduling / ADT | `status`, at least one `participant` with `actor.reference` |

All resources: `POST /fhir/{Type}` (201, writes audited, roles
admin/configurator/clinician only), `GET /fhir/{Type}/{id}` (404 if absent),
`GET /fhir/{Type}` search returning a `searchset` Bundle. Responses carry
`meta.versionId`.

### Search parameters (all resource types)
- `patient=<id or Patient/<id>>` — via the `patient_ref` projection
  (for Appointment, the `Patient/*` participant actor).
- `practitioner=<id or Practitioner/<id>>` — via the `practitioner_ref`
  projection (first `Practitioner/*` participant actor; primarily
  Appointment).
- `start=<ISO datetime>`, `end=<ISO datetime>` — date-range filter on the
  parsed `start_at` projection (Appointment `start`); malformed datetime
  -> 422.

### Appointment ADT transitions
`POST /fhir/Appointment/{id}/transition` with `{"to_status": "..."}`
(gated by the `fhir.write` capability — default roles
admin/configurator/clinician; audited as `fhir.appointment.transition`;
bumps `meta.versionId`).
**The state machine is Studio configuration**: the published
`workflow/appointment-adt` config (seeded as v1 at startup with exactly the
transitions below) is what the endpoint enforces; the hardcoded map is only
a fallback used when that config is disabled. Publishing a different
workflow config changes the legal transitions.
Default machine: `proposed|pending -> booked|cancelled`,
`booked -> arrived|cancelled`, `arrived|checked-in -> fulfilled|cancelled`.
`fulfilled`, `cancelled`, `noshow` are terminal. Illegal transition -> 409;
unknown target status or missing `to_status` -> 422; unknown id -> 404.

### QuestionnaireResponse (Studio forms)
| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| QuestionnaireResponse | Answers to a Studio form | `questionnaire` naming a **published** Studio form config (kind `form`; bare key or `Questionnaire/<key>`); one `item` answer per form item marked `required` |

`POST /fhir/QuestionnaireResponse` (201; `fhir.write` capability) validates
against the published form definition before storing through the normal
resource layer, so read/search and patient read scoping behave exactly like
the other resource types. A response referencing a draft or nonexistent
form -> 422.

### Studio configuration API (`/studio/config`)
Versioned configuration objects — kinds `form`, `workflow`, `terminology`,
`navigation`, `branding`, `permissions` — with payload validation per kind
(structural problems -> 422 with details; navigation items naming a page or
form instead of a module -> 400, the modules-only menu rule).
`POST /studio/config/{kind}/{key}` creates the next version as a draft
(409 if a draft is already open); `PUT .../versions/{n}` replaces a draft
payload (409 for any other status — published versions are immutable);
`POST .../versions/{n}/publish` publishes and supersedes the previous
published version; `POST .../versions/{n}/disable` disables a published
version; `POST /studio/config/{kind}/{key}/rollback` with `{"version": n}`
republishes an earlier payload as a new version. Writes require
admin/configurator; every transition is audit-logged. Reads:
`GET /studio/config[/{kind}/{key}[/versions/{n}]]` (clinician/viewer see
published versions only), `GET /studio/config/{kind}/effective[/{key}]`
(published only), `GET /studio/config/{kind}/{key}/diff?from=&to=`.
Unknown kind -> 404.
The `permissions` object (key `role-matrix`) overrides capability cells:
`{"capabilities": {"fhir.write": {"viewer": true}}}` — merged over the
built-in defaults (`fhir.write`, `fhir.inbox`: admin/configurator/clinician;
`studio.manage`: admin/configurator) and enforced in the API dependencies.

### Results inbox
`GET /fhir/inbox?patient=<id or Patient/<id>>&status=<optional>` — staff
roles only (admin/configurator/clinician; viewer and patient -> 403).
Returns one Bundle combining the patient's Observation + DiagnosticReport
resources, newest first by stored creation time.

### Patient read scoping (role = patient)
A patient user carries `patient_ref` (e.g. `Patient/pat-1`). Rules:
- By-id reads: resources whose `patient_ref` differs from the caller's
  return **404** (non-disclosure — identical to a missing resource).
- Search: results are force-scoped to the caller's own `patient_ref`; an
  explicit `patient=` parameter naming a different patient -> **403**.
- `GET /portal/my-results` (patient role only): the caller's own
  Observation + DiagnosticReport Bundle, newest first.
- Patients cannot write (403 on all POST routes).

## Phase 2 (implemented) — Lab LIS

New resource types on the generic surface (same create/read/search +
patient read scoping as all other types):

| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| ServiceRequest | Lab orders (and generic requests) | `status`, `intent`, `code`, `subject.reference` |
| Specimen | Collected samples | `status`, `subject.reference` |
| Task | Workflow tasks (`for` projects the patient) | `status`, `intent` |

### Lab API (`/lab`, app.lab) — everything site-specific is Studio config

- **Catalog**: terminology config `lab-catalog` (seeded published v1):
  concepts are LOINC-coded analytes (`unit`, `referenceRange` with
  `low`/`high`/`criticalLow`/`criticalHigh`) or panels (`panel: true`,
  `components: [analyte codes]`). `GET /lab/catalog` serves the effective
  catalog + workflow to staff (patient -> 403).
- **Workflow**: workflow config `lab-order-flow` (seeded published v1):
  `ordered -> collected -> received -> in-process -> resulted ->
  verified -> released`, plus `collected/received/in-process ->
  rejected` and `ordered -> cancelled`. The published config is the live
  machine; the code constant is only a fallback if it is disabled.
- `POST /lab/orders` (`lab.order` capability; default
  admin/configurator/clinician): body `{code}` (catalog code or a
  CodeableConcept) + `subject.reference` (or `patient`). Unknown or
  inactive code -> **422**; unknown patient -> **404**. The order is a
  ServiceRequest with status `ordered`, intent `order`.
- `POST /lab/orders/{id}/transition` (`lab.work`) with `{to_status}`:
  illegal transition -> **409**, unknown state -> **422**. Moving to
  `collected` creates the linked Specimen with a server-generated
  accession id (`accessionIdentifier`, system `urn:opencare:accession`)
  and mirrors later states onto it. `rejected` requires `reason`
  (else 422). `resulted`/`verified`/`released` are governed states —
  a bare transition into them -> **422** (use the endpoints below).
- `POST /lab/orders/{id}/results` (`lab.work`) with
  `{results: [{code, value, unit?}]}`: only for orders in
  `received`/`in-process` (else **409**). Panel orders accept only their
  component analytes, single-analyte orders only their own code;
  unknown/inactive/panel codes or non-numeric values -> **422**.
- `POST /lab/ingest` (`lab.work`) with `{format: "hl7"|"csv", content}`:
  HL7 v2 ORU^R01 (PID-3 patient, OBR-2/3 order id, OBX-3/5/6 results) or
  CSV (`order_id,code,value` + optional `patient_id,unit`). Ingest
  references an existing order, verifies the patient matches, and
  creates drafts through the same path as manual entry — malformed
  messages -> **422**, orders not yet received -> **409**.
- **Draft visibility**: result Observations + DiagnosticReport are
  created `preliminary` with `meta.tag`
  `{system: "urn:opencare:visibility", code: "lab-unreleased"}`. Tagged
  resources are excluded from `GET /fhir/inbox` and
  `GET /portal/my-results`, from patient search results, and patient
  by-id reads of them -> **404**. Staff manage drafts only via `/lab`.
- **Interpretation**: computed at result creation from the catalog
  range — critical thresholds first (`LL`/`HH`), then the reference
  range (`L`/`H`), else `N` (HL7 v3 ObservationInterpretation system);
  the Observation also carries the applied `referenceRange`. A report
  containing a critical value gets extension
  `urn:opencare:extension/critical-result = true`. Ranges are v1
  age/sex-agnostic (one range per analyte, from config).
- `GET /lab/verification-queue` (staff incl. viewer): reports whose
  order is `resulted`/`verified`, each with results, `critical` flag,
  `stage` (`awaiting-verification`/`awaiting-release`) and live
  `qc_blocked` list.
- `POST /lab/reports/{id}/verify` (`lab.verify` capability; default
  admin/clinician — configurator excluded): report `preliminary ->
  final`, order `resulted -> verified`. Wrong state -> **409**;
  non-lab report -> **422**.
- `POST /lab/reports/{id}/release` (`lab.verify`): requires the order
  `verified` (else **409**); Observations become `final`, the
  unreleased tag is removed from report + observations (the moment
  results appear in the inbox and portal), `issued` is stamped, and
  order/specimen move to `released`. Nothing is ever auto-released.
- **QC**: `POST /lab/qc` (`lab.work`) with `{analyte_code,
  control_material, expected_low, expected_high, measured_value, unit?}`
  — `passed` is computed server-side (`expected_low <= measured <=
  expected_high`); unknown analyte or `expected_low > expected_high`
  -> **422**. `GET /lab/qc[?analyte_code=]`, `GET
  /lab/qc/status?analyte_code=` (staff). **QC gate**: if the LATEST QC
  run for an analyte failed, verify and release of any report
  containing that analyte -> **409** naming the analyte, until a
  passing run exists after it. No QC history does not block.
- Capabilities `lab.order` / `lab.work` / `lab.verify` are ordinary
  Studio permissions-config capabilities (defaults in
  `app.configstore.CAPABILITY_DEFAULTS`, overridable via the published
  `permissions/role-matrix` config like `fhir.write`).

## Phase 4A (implemented) — Billing / claims + Pharmacy

New resource types on the generic surface (same create/read/search +
patient read scoping as all other types; Claim projects its patient from
`patient.reference`, the others from `subject.reference`):

| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| Claim | Billing claim (charge capture + lifecycle) | `status`, `patient.reference` |
| MedicationDispense | Pharmacy dispensing record | `status`, (`medicationCodeableConcept` or `medicationReference`), `subject.reference` |
| MedicationAdministration | Bedside administration record (BCMA) | `status`, (`medicationCodeableConcept` or `medicationReference`), `subject.reference` |

### Billing API (`/billing`, app.billing) — pricing + lifecycle are Studio config

- **Fee schedule**: terminology config `fee-schedule` (seeded published
  v1, synthetic USD prices): concepts carry `system` + `unitPrice`.
  Pricing looks a code up by (system, code), falling back to code alone.
- **Charge capture**: `POST /billing/claims/generate` (`billing.manage`
  capability; default admin/configurator) with `{encounter_id}`. Builds
  a draft Claim for the encounter's patient:
  - every Condition on the patient's problem list becomes a `diagnosis`
    entry AND a priced line (problem evaluation);
  - Procedures and Observations are charged when they reference the
    encounter (`encounter.reference`) or, carrying no encounter
    reference, when their clinical date (`performedDateTime` /
    `effectiveDateTime`) falls inside the encounter period.
  - A code absent from the fee schedule is priced **0 and flagged
    unpriced** (`item.extension` `urn:opencare:extension/priced` =
    false) — a charge is never silently dropped. Line sources are kept
    in `urn:opencare:extension/charge-source`. Totals are computed
    server-side. No billable items -> **422**; unknown encounter/patient
    -> **404**.
- **Lifecycle**: workflow config `claim-lifecycle` (seeded published v1):
  `draft -> ready -> submitted -> paid | denied`, `denied -> appealed`,
  `appealed -> submitted`; `paid` is terminal. `POST
  /billing/claims/{id}/transition` (`billing.manage`) with `{to_status,
  note?}`: illegal -> **409**, unknown state -> **422**. Every
  transition is appended to the claim's `supportingInfo` status history
  and audit-logged. **Appeal**: `to_status = "appealed"` is legal only
  from `denied`, **requires a note** (else 422), and may happen **once**
  per claim (counter in extension `urn:opencare:extension/appeal-count`;
  a second appeal -> **409**, history preserved). API responses expose
  the parsed `appealCount` and `statusHistory` at top level.
- **Reads**: `GET /billing/claims[?status=][?patient=]`, `GET
  /billing/claims/{id}` — capability `billing.view` (defaults
  admin/configurator/clinician; **viewer denied by default**, grantable
  via the published permissions config). A **patient** caller sees only
  their own claims (amounts included — they are the patient's own
  claims), read-only; another patient's claim -> **404**
  (non-disclosure), an explicit foreign `patient=` filter -> **403**.
- **X12 837P export**: `GET /billing/claims/{id}/export/837`
  (`billing.view`) returns `text/plain`: ISA/GS/ST envelope, BHT, NM1
  submitter/receiver/billing-provider, HL/SBR subscriber loop, DMG, CLM
  with the claim total, HI diagnoses, one LX/SV1 per claim line, and
  SE/GE/IEA trailers whose counts and amounts reconcile with the claim
  (SE count = segments ST..SE; sum of SV1 amounts = CLM02 = claim
  total). Only claims in `submitted`/`paid`/`denied`/`appealed` may be
  exported (draft/ready -> **409**). **This is a format-correct export
  file only — there is NO clearinghouse connection, payer submission,
  or 835 remittance processing.**

### Pharmacy API (`/pharmacy`, app.pharmacy) — formulary is Studio config

- **Formulary**: terminology config `formulary` (seeded published v1,
  RxNorm): concepts carry `stockTracked` and `initialStock`. Stock is
  server-side state (`pharmacy_stock` table), seeded once from
  `initialStock` for stock-tracked meds; a restart never resets it.
  `GET /pharmacy/formulary`, `GET /pharmacy/stock` (staff incl. viewer;
  patient -> 403).
- `GET /pharmacy/dispense-queue` (staff): ACTIVE MedicationRequests
  with their formulary entry and live stock level.
- `POST /pharmacy/dispense` (`pharmacy.dispense` capability; default
  admin/configurator/clinician) with `{medication_request_id, quantity?}`
  (default quantity 1): the request must be `active` (else **409**) and
  its medication an active formulary entry (else **422**). Creates a
  `completed` MedicationDispense (`authorizingPrescription` -> the
  request), decrements stock for stock-tracked meds in the same
  transaction, and moves the request to status `dispensed`. Dispensing
  more than the on-hand stock -> **409** with nothing written.
  Response: `{dispense, medication_request, stock_after}` (`stock_after`
  is null for non-tracked meds).
- `GET /pharmacy/dispenses[?patient=]` (staff): dispense Bundle.
- **BCMA-lite med-admin**: `POST /pharmacy/administer`
  (`pharmacy.administer` capability; default admin/clinician) with
  `{wristband_id, medication_code, dispense_id}`. Enforced rights, each
  failing as **422 naming the right**: *right dispense* (the dispense
  exists), *right medication* (the dispense's code equals the scanned
  code), *right patient* (the dispense's patient equals the wristband id
  — bare id or `Patient/<id>`). Administering an already-administered
  dispense -> **409**. A clean scan creates a `completed`
  MedicationAdministration linked to the dispense via extension
  `urn:opencare:extension/dispense`. (Right dose / right time are not
  modelled in v1 — stated, not implied.)

## Phase 4B (implemented) — Specialty boards (ED, radiology, surgery)

New resource type on the generic surface (same create/read/search +
patient read scoping as all other types):

| Resource | Use | Required fields enforced (else 422) |
|---|---|---|
| ImagingStudy | Imaging study metadata (DICOMweb source) | `status`, `subject.reference`, an `identifier` with system `urn:dicom:uid` (the Study Instance UID) |

All three boards live in `app.boards` and are staff-only: board reads
require a staff role (admin/configurator/clinician/viewer; **patient ->
403**, unauthenticated -> 401), writes are capability-gated
(`ed.work`, `radiology.work`, `surgery.work`: admin/configurator/
clinician; `radiology.verify`: admin/clinician — all ordinary Studio
permissions-config capabilities). Everything site-specific is Studio
configuration, seeded published v1 at startup: `workflow/ed-flow`,
`workflow/surgery-flow`, `terminology/imaging-modalities`,
`terminology/or-rooms`, and `integration/dicom-viewer` (a new config
kind, `integration`, whose payload carries external-system connection
data; the viewer link target is the payload path `viewer.baseUrl`).

### ED board (`/ed`)

- The ED visit is an Encounter with `class` code **EMER** whose
  `status` carries the ED tracking state (the lab convention):
  `waiting -> triage -> in-treatment -> disposition -> departed |
  admitted`, from the published `workflow/ed-flow` config (the code
  constant is only a fallback if the config is disabled).
- `POST /ed/arrivals` (`ed.work`) with `{patient}` (or
  `subject.reference`), optional `chief_complaint` and `arrived_at`
  (defaults to now; the recorded arrival drives wait times). Unknown
  patient -> **404**.
- `GET /ed/board` (staff): the current census — EMER encounters in an
  active state, **grouped by state**, longest-waiting first, each entry
  carrying `wait_minutes` **computed server-side** from
  `period.start`, the recorded `acuity` (or null), and the chief
  complaint; plus per-state `counts` and a `total`.
- `POST /ed/encounters/{id}/transition` (`ed.work`) with
  `{to_status}`: illegal -> **409**, unknown state -> **422**. Moving
  to `triage` **requires `acuity`: an ESI integer 1–5** (else 422),
  stored as extension `urn:opencare:extension/ed-acuity`. A non-EMER
  encounter -> **422**.
- `departed` / `admitted` are **governed states** — a bare transition
  into them -> **422**; use:
  - `POST /ed/encounters/{id}/admit` (`ed.work`): legal only when the
    workflow allows `<current> -> admitted` (else **409**). Creates an
    inpatient Encounter (`class` IMP, `status` in-progress,
    `partOf` -> the ED encounter, optional `ward` recorded as a
    location), moves the ED encounter to `admitted`, closes its
    period, and records the link on the ED encounter as extension
    `urn:opencare:extension/admission-encounter`.
  - `POST /ed/encounters/{id}/depart` (`ed.work`): same legality rule;
    moves to `departed` and closes the period.

### Radiology (`/radiology`) + DICOMweb (`/dicomweb`)

- `GET /radiology/modalities` (staff): the published modality list.
- `POST /radiology/studies` (`radiology.work`) with
  `{service_request_id, modality, description?}`: the order must exist
  (**404**) and carry a subject patient (**422**); the modality must
  be an **active** concept in the published `imaging-modalities`
  config (unknown/inactive -> **422**). Creates an ImagingStudy:
  `status` available, a generated Study Instance UID
  (`2.25.<uuid-integer>`, identifier system `urn:dicom:uid`),
  `basedOn` -> the ServiceRequest, `started` now,
  `numberOfSeries`/`numberOfInstances` 0. The response item carries
  `study_uid`, a `viewer_url` (below), the latest `report` summary (or
  null) and **`has_images: false`**.
- **Metadata-only, stated plainly**: v1 studies carry **no pixel data**
  — there are no series/instances, no WADO retrieval, and no embedded
  viewer anywhere in the system. The study body itself carries a note
  saying so.
- **Viewer link**: `viewer_url` is constructed server-side from the
  published `integration/dicom-viewer` config as
  `{viewer.baseUrl}/viewer?StudyInstanceUIDs={uid}` (OHIF-style; the
  seeded default base URL is the public OHIF demo viewer — a deployment
  points it at its own viewer in Studio). When no base URL is
  configured the field is null and the UI says “viewer not configured”
  instead of inventing a link. The viewer is external software; only
  the link is ours.
- `GET /radiology/studies`, `GET /radiology/studies/{id}` (staff):
  list items as above; detail adds the study's `reports`.
- `POST /radiology/studies/{id}/report` (`radiology.work`) with
  `{conclusion}` (or `findings`): drafts a DiagnosticReport in
  `preliminary` status, category RAD, `imagingStudy` -> the study,
  `basedOn` -> the order, code inherited from the order. Missing text
  -> **422**; a study with an open preliminary report -> **409**
  (verify it first).
- `POST /radiology/reports/{id}/verify` (`radiology.verify`):
  `preliminary -> final`, `issued` stamped. A report with no
  `imagingStudy` link -> **422**; not preliminary -> **409**.
- **DICOMweb** (QIDO-RS shaped, staff-only): `GET /dicomweb/studies`
  returns a JSON array of study-level **DICOM JSON** objects (PS3.18:
  hex tag -> `{vr, Value}`) derived from the stored ImagingStudies —
  `0020000D` StudyInstanceUID (UI), `00100010` PatientName (PN,
  `Family^Given`), `00100020` PatientID (LO), `00080020`/`00080030`
  StudyDate/Time (DA/TM, from `started`), `00080061`
  ModalitiesInStudy (CS), `00200010` StudyID (SH), `00201206` /
  `00201208` study-related series/instance counts (IS, 0 in v1), and
  `00081030` StudyDescription when present. `GET
  /dicomweb/studies/{study_uid}` returns the single study object for
  that UID (unknown UID -> **404**). Metadata only — no series,
  instance, or pixel endpoints exist.

### Surgery board (`/surgery`)

- A case is a **Procedure** whose `status` carries the case workflow
  state — `scheduled -> pre-op -> in-op -> pacu -> done`, cancellable
  from `scheduled`/`pre-op` — from the published
  `workflow/surgery-flow` config, linked to a scheduling
  **Appointment** (`start`/`end`, patient + room participants) by
  extensions: `urn:opencare:extension/or-room` (room code) and
  `urn:opencare:extension/scheduled-appointment` on the case,
  `urn:opencare:extension/surgery-case` on the appointment.
- `GET /surgery/rooms` (staff): the published `or-rooms` list.
- `POST /surgery/cases` (`surgery.work`) with `{patient_id}` (or
  `patient` / `subject.reference`), `{code, display?, system?, room,
  start, end, surgeon?}`: unknown patient -> **404**; room not an
  active `or-rooms` concept -> **422**; missing/invalid times or
  `end <= start` -> **422**; **room double-booking -> 409** (below).
- **Overlap rule**: creating or rescheduling refuses (409, naming the
  conflicting appointment) when another non-cancelled appointment in
  the same room has a stored interval overlapping the requested one
  (`start < other.end && other.start < end`, comparing the real stored
  times). Adjacent intervals (one ends exactly when the other starts)
  are not overlaps; different rooms never conflict; cancelling a case
  cancels its appointment and frees the room.
- `GET /surgery/board?date=YYYY-MM-DD` (staff; default today, UTC;
  malformed date -> **422**): the day's cases **grouped by room** —
  every configured room appears, even when empty — each case with its
  appointment times, patient and current workflow state, in start
  order.
- `POST /surgery/cases/{id}/transition` (`surgery.work`) with
  `{to_status}`: illegal -> **409**, unknown state -> **422**;
  `done`/`cancelled` also settle the linked Appointment
  (fulfilled/cancelled). A Procedure that is not a surgery case ->
  **422**.
- `POST /surgery/cases/{id}/reschedule` (`surgery.work`) with
  `{start, end}`: moves the appointment; the overlap rule is enforced
  again excluding the case's own appointment; a `done`/`cancelled`
  case -> **409**.

## Planned
- Phase 1 (remaining): Practitioner, Organization, DocumentReference
  resources; full US Core profile validation in CI (validator wiring).
- Phase 3: Bulk FHIR NDJSON export shapes for FHIR → OMOP (dbt).
- Phase 4 remaining: Coverage resource; 835 remittance import; DICOM
  pixel-data ingest (real series/instances + WADO-RS retrieval); ED
  age/sex-specific triage protocols.
