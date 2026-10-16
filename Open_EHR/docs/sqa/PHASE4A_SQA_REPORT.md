# Phase 4A SQA report — Billing/claims + Pharmacy

Date: 2026-10-15. Gate: `docs/SQA.md`. Data: synthetic only (seed bundle +
obviously synthetic additions created through the real API in tests).
Phase 4 is split: **4A = billing/claims + pharmacy (this report)**;
4B = ED board / radiology / surgery board (next, not started).

## What shipped

- **Billing** (`backend/app/billing.py`):
  - *Charge capture* — `POST /billing/claims/generate` builds a draft
    FHIR Claim from one Encounter: every Condition on the patient's
    problem list becomes a diagnosis + a priced line; Procedures and
    Observations are charged when they reference the encounter or
    (unlinked) fall inside its period. Prices come from the **published
    Studio fee schedule** (`terminology/fee-schedule`, seeded v1,
    synthetic USD prices for the CPT/LOINC/SNOMED codes the seed + lab
    catalog use). A code missing from the schedule is priced **0 and
    flagged `priced: false`** via line extension — never dropped.
    Totals are summed server-side; claim stored through the generic
    resource layer (Claim added to `app.fhir.SUPPORTED` with a
    `patient.reference` projection, so patient read scoping applies).
  - *Lifecycle* — `workflow/claim-lifecycle` config (seeded published
    v1): draft → ready → submitted → paid | denied, denied → appealed,
    appealed → submitted; paid terminal; illegal 409, unknown state
    422, every transition audit-logged and appended to the claim's
    `supportingInfo` status history. **Appeal**: from denied only,
    requires a note (422 without), **once per claim** (counter in an
    extension; second appeal 409, history preserved).
  - *X12 837P export* — dependency-free writer producing ISA/GS/ST,
    BHT, NM1 (submitter/receiver/billing provider), HL/SBR subscriber
    loop, DMG, CLM (claim total), HI diagnoses, LX/SV1 per line, and
    SE/GE/IEA trailers. SE count and SV1 sums reconcile with CLM02 by
    construction (asserted in tests). Submitted-or-later claims only
    (draft/ready → 409). **Export only — no clearinghouse submission.**
  - *Roles* — new capabilities `billing.view` (admin/configurator/
    clinician) and `billing.manage` (admin/configurator) in
    `CAPABILITY_DEFAULTS` (Studio-overridable). Patient callers see
    only their OWN claims read-only (amounts visible — they are the
    patient's claims); another patient's claim → 404; viewer → 403 by
    default, grantable via permissions config (tested both ways).
- **Pharmacy** (`backend/app/pharmacy.py`):
  - *Formulary* — `terminology/formulary` config (seeded published v1,
    RxNorm — the exact medications the seed prescribes), concepts carry
    `stockTracked` + `initialStock`. Stock lives server-side
    (`PharmacyStock` model), seeded once from the formulary; restarts
    never reset it.
  - *Dispensing* — `POST /pharmacy/dispense` (`pharmacy.dispense`
    capability): active MedicationRequest → completed
    MedicationDispense (linked via `authorizingPrescription`), stock
    decremented in the same transaction for tracked meds, request
    status `active → dispensed`. Oversell → **409 with nothing
    written** (no dispense, request untouched, stock untouched —
    asserted). Non-active request → 409; non-formulary medication →
    422; unknown request → 404.
  - *BCMA-lite* — `POST /pharmacy/administer` (`pharmacy.administer`,
    admin/clinician): scans wristband id + medication code + dispense
    id; right-dispense / right-medication / right-patient mismatches
    are each **422 naming the failed right**; administering the same
    dispense twice → **409**; success creates a
    MedicationAdministration linked to the dispense by extension.
  - Reads: `GET /pharmacy/formulary`, `/pharmacy/stock`,
    `/pharmacy/dispense-queue`, `/pharmacy/dispenses` (staff incl.
    viewer; patient 403).
- **Web**: Billing page (generate-from-encounter for managers, status
  filter, claims table, detail with line items + unpriced flags +
  totals + diagnosis list + status history, lifecycle buttons, appeal
  with inline note, 837 download + preview; patient role is read-only)
  and Pharmacy page (dispense queue with live stock + quantity +
  oversell guard, stock table, BCMA scan form pre-filled from the last
  dispense). Client methods + types + nav (billing for
  admin/configurator/clinician/patient, pharmacy for staff; viewer has
  no billing nav, mirroring the server default).

## Gate results (exact)

1. **Unit + API tests**
   - Backend: `python -m pytest -q` → **127 passed** (102 pre-existing +
     25 new: `tests/test_phase4a_billing.py` 13,
     `tests/test_phase4a_pharmacy.py` 12).
   - Web: `npm test` → **62 passed** (44 pre-existing + 18 new:
     `src/api/billingPharmacy.test.ts` 8, `pages/BillingPage.test.tsx`
     5, `pages/PharmacyPage.test.tsx` 5; `auth/session.test.ts`
     rewritten for the new nav — still 6 tests). `npm run build` →
     **exit 0**.
2. **FHIR validation** — Claim / MedicationDispense /
   MedicationAdministration added to the generic surface with required
   fields enforced (Claim: `status` + `patient.reference`; dispense /
   administration: `status` + medication + `subject.reference`), else
   422 — negative paths exercised through the module endpoints
   (non-formulary 422, missing scan fields 422, no billable items 422).
3. **RBAC negative tests** — billing: unauthenticated 401; viewer 403
   on list/get/generate (and 200 after a permissions-config grant —
   manage still 403); clinician generate/transition 403; patient
   foreign-patient filter 403 and foreign claim 404. Pharmacy: patient
   403 on stock/queue/dispense/administer; viewer dispense 403;
   configurator administer 403; unauthenticated 401 on both write
   endpoints.
4. **Audit assertions** — billing generate/transition/export write
   `billing.claim.generate` / `billing.claim.transition` /
   `billing.claim.export837`; pharmacy writes `pharmacy.dispense` /
   `pharmacy.administer`; both suites assert the rows exist and
   `verify_chain` still passes. Config seeds write the standard
   `studio.config.create/publish` rows.
5. **Completeness / no-noop** — every route's effect is a persisted row
   observed by independent reads in tests: claim totals recomputed from
   stored lines; stock 120 → 110 observed via `GET /pharmacy/stock`
   after a dispense; request status flip observed via `GET
   /fhir/MedicationRequest/{id}`; oversell proven to change NOTHING
   (dispense count 0, request still active, stock unchanged); BCMA
   administration read back via `GET
   /fhir/MedicationAdministration/{id}`. Config-driven behaviour proven,
   not just claimed: publishing fee-schedule v2 with a changed ECG
   price reprices new claims (45 → 50, total 112 → 117); publishing a
   claim-lifecycle v2 adding `draft → submitted` makes that transition
   legal (409 → 200 without it).
6. **Security** — no secrets in code or fixtures; synthetic data only
   (synthetic payer/ETIN/NPI/member-id strings in the 837 writer are
   labelled synthetic); prices and totals are never accepted from the
   caller; patient claim access is own-claims-only with non-disclosure
   404s; no new dependencies (837 writer and all endpoints use the
   existing FastAPI/SQLAlchemy stack only).
7. **Data integrity** — dispense + stock decrement + request flip are
   one transaction (oversell check happens before any write); claim
   lifecycle is a state machine with 409 on out-of-order transitions;
   appeal counter + history live on the claim itself, so a resubmission
   cannot launder a second appeal; stock rows are seeded once and never
   reset by restarts (seed function skips existing rows).
8. **Docs** — `contracts/fhir-resources.md` gained the Phase 4A section
   (billing + pharmacy contracts, incl. the attribution rule and the
   export scope statement) and its Planned list now names 4B;
   `README.md` gained a Phase 4A section + layout updates;
   `docs/PHASES.md` records the Phase 4 split (4A DONE, 4B next); this
   report.

## Live verification beyond unit tests (actually run)

- **Live HTTP smoke** (real uvicorn on a temp SQLite DB, real seed
  loader: 86 resources created): **16/16 checks PASS** — health reports
  phase `4a`; claim generated from seeded encounter `enc-1-1` carried
  pat-1's two priced problem-list lines (38341003 $45 + 44054006 $55,
  total $100.00); patient listed the claim, viewer got 403; lifecycle
  ready → submitted over HTTP; 837 export returned ISA-led segments
  with CLM02 = $100.00 = sum of the 2 SV1 amounts; dispense queue
  listed the 9 seeded active requests; dispensing seeded `med-1`
  (Lisinopril) ×5 took stock 120 → 115; BCMA wrong-patient scan → 422
  naming "Right patient", correct scan → 201, duplicate scan → 409.
  Note, honestly: the seed's lab observations are longitudinal
  (Jan/Apr/Jul/Oct) and never fall inside a 30-minute encounter period,
  so the seeded demo claim contains diagnosis lines only — procedure /
  observation attribution (encounter-linked and in-period paths) is
  covered by the pytest suite, not by this smoke.

## NOT RUN / not claimed (honest list)

- **Clearinghouse submission NOT RUN / not built** — there is no
  clearinghouse connection, no payer enrolment, no SFTP/API transport,
  and no 835 remittance processing. The 837 output is a
  format-correct export file for review/testing only.
- **PostgreSQL runtime NOT RUN** — no Docker/Postgres on this VM (same
  standing caveat as Phases 0–3); all new code uses the portable
  SQLAlchemy layer (JSON payloads, one plain Integer counter table).
- **Browser click-through E2E NOT RUN** — web covered by component
  tests against a stub client at the network boundary + client
  request-shaping tests, per the established pattern; the live HTTP
  smoke above exercised the real server end-to-end instead.
- **Coverage resource / eligibility checks** — not built (named in
  contracts as remaining Phase 4 work); claims carry a free-text
  synthetic insurer display only.
- **BCMA right-dose / right-time** — not modelled in v1: dispensed
  quantity is recorded, but the scan does not verify dose against the
  order's dosage instructions, and there is no scheduled-window check.
  The three enforced rights are patient / medication / dispense.
- **Claim edits / voiding, partial payments, COB (secondary claims)** —
  not built; the lifecycle is the five-state machine above plus the
  one-time appeal.
