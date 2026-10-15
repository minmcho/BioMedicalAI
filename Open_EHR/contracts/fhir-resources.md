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
(roles admin/configurator/clinician; audited as
`fhir.appointment.transition`; bumps `meta.versionId`).
Allowed: `proposed|pending -> booked|cancelled`,
`booked -> arrived|cancelled`, `arrived|checked-in -> fulfilled|cancelled`.
`fulfilled`, `cancelled`, `noshow` are terminal. Illegal transition -> 409;
unknown target status or missing `to_status` -> 422; unknown id -> 404.

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

## Planned
- Phase 1 (remaining): Practitioner, Organization, DocumentReference
  resources; full US Core profile validation in CI (validator wiring).
- Phase 2 (Lab): ServiceRequest, Specimen, DiagnosticReport + Observation
  profiles for lab results/QC.
- Phase 3: Bulk FHIR NDJSON export shapes for FHIR → OMOP (dbt).
- Phase 4: Coverage, Claim, MedicationDispense, ImagingStudy (DICOMweb refs).
