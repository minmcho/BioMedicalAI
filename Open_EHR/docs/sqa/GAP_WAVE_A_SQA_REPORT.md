# Gap Programme Wave A — Safety Gates · SQA Report (2026-10-20)

Coordinator-run gate; staging only. Release HELD — nothing pushed,
nothing synced. Synthetic data only. Plan:
`~/workspace/gapprogramme_work/PROGRAMME_PLAN.md` §3; register:
`~/workspace/qcaudit_work/MODULE_GAP_REGISTER.md`. Wave A closes the
paths where a patient-safety control existed somewhere in the tree
but the dangerous act could still complete end-to-end without it:
five register BLOCKERs, three pulled MAJORs (the second halves of the
same safety chains), and one user-reported defect folded in as item 9.

## Gate results (coordinator-run, 2026-10-20)

- **Backend: 1,511 passed / 0 failed / 0 errors** (129 files,
  22 batches of ≤6 files, private basetemps, /tmp pruned between
  batches, proxy vars stripped, live-stack env sourced so the live
  legs — PostgreSQL/pgvector, MariaDB, Keycloak OIDC, MinIO S3 —
  ran in sequence). Baseline 1,448 + 63 new Wave A tests (LAB 22,
  PHARM 14, CHART 13, WF 14) = 1,511 — reconciles exactly.
- **Web: 573 passed / 82 files** (baseline 558; +12 WaveAChart,
  +2 item-9 indicator-focus, +1 net pharmacy). **Production build
  exit 0.** **Case-collision audit over web/src: ZERO.**
- The run survived two runtime restarts by the standing discipline:
  batches 1–3 banked from the on-disk log (229 passed), the live
  stack restarted and verified (ports 5432/3306/8080/9000), batches
  4–22 resumed to completion. Nothing was carried forward unverified.
- Two coordinator integration fixes were needed, both diagnosed to
  root cause and registered below: the verification-queue contract
  pin (additive response keys) and the Phase 17 interactions fixture
  (apixaban became high-alert under PHARM-M1; its verify now carries
  the governed override). No production code was changed at the gate.
- NOT RUN: browser/rendered check — blocked on this VM by standing
  environment limits; Min's Air click-through after the next sync is
  the acting browser gate.

## 1. LAB-B1 (LAB-1) — Blood-bank issue gate · BLOCKER CLOSED

`issue_blood_unit` previously checked unit status, expiry, and the
reaction hold only — an ABO-incompatible unit could be issued
end-to-end. Now a pure-read gate evaluator
(`backend/app/lab_benches.py`, `_evaluate_issue_gate`) runs inside
the issue handler **before any mutation** (including before the
legacy expiry flip), and a refusal is a 409 whose detail names
**every** failed check, with zero state change:

- the patient must have a **resulted (released) 883-9 ABO group** —
  no group on file refuses ("type and screen first");
- **compatibility matrices in code** (a clinical standard, not site
  configuration): RBC — O→any, A→A/AB, B→B/AB, AB→AB, Rh-positive
  donor never to an Rh-negative recipient; plasma — the inverse
  matrix (AB universal donor), Rh not applicable;
  platelets/cryoprecipitate — the RBC matrix as the documented
  default;
- the latest resulted **890-4 antibody screen must be Negative**
  within the governed validity window; a Positive screen refuses at
  any age;
- a **unit-specific resulted crossmatch** — new endpoint
  `POST /lab/blood-bank/orders/{order_id}/crossmatch-result` results
  BB-XM through the standard result-entry core and stamps
  `urn:opencare:extension/crossmatch-unit` on the Observation and
  report; the gate consumes exactly those stamped, released
  observations (a generic patient-level crossmatch never satisfies
  it; an "Incompatible" result for the unit is an absolute refusal) —
  **or** governed **electronic-crossmatch eligibility** (resulted
  group + negative screen + eXM enabled for the product);
- **emergency release** is the only override: it bypasses *missing*
  evidence only (nothing resulted yet, expired negative screen,
  eXM-ineligible), requires a reason code from the governed list, and
  requires a universally compatible unit when the patient group is
  unknown (O NEG red cells, AB plasma). It **never** bypasses
  contradictory evidence — ABO/Rh incompatibility against a resulted
  group, a positive screen, an incompatible crossmatch, or a bogus
  reason code all still refuse. The release is audited in the issue
  transaction under the distinct action
  `lab.bloodbank.unit.emergency_issue` with reason code, note, and
  the bypassed checks recorded.

A dry-run posture endpoint (`GET /lab/blood-bank/units/{unit_id}/
gate-status?order_id=`) exposes the same evaluator without writes.
The Lab page blood-bank section shows the governed gate parameters
and per-(unit, order) posture, renders refusal reasons verbatim, and
replaces the old "no linkage" concession text with the real
type-&-screen/crossmatch linkage.

**Studio surface:** the lab catalog settings payload
(`terminology` lab catalog) gained a governed `bloodBankGate`
section — `antibodyScreenValidityHours: 72`,
`electronicCrossmatch: {enabled: true, products: ["rbc"]}`,
`emergencyReleaseReasonCodes` (life-threatening-haemorrhage,
massive-transfusion-protocol, urgent-clinical-need) — seeded by
default and merged into pre-existing published catalogs through the
tree's governed-republication upgrade (new published version, system
actor, audited; site customization never overwritten). The ABO/Rh
matrix itself is deliberately NOT configuration.

**Evidence:** `backend/tests/test_wave_a_lab.py` (22 tests) proves
refusal + zero state change for ABO-incompatible RBC, Rh+→Rh−,
missing patient group, positive screen, expired screen, incompatible
unit crossmatch, missing crossmatch with eXM-ineligible product, and
plasma inverse-matrix incompatibility; and allows for compatible +
crossmatched, plasma inverse-compatible, the eXM path (incl. the
dry-run), and emergency release with missing evidence + universal
unit — while emergency release is refused for a non-universal unit,
a bogus reason code, a positive screen, and an incompatible
crossmatch.

## 2. LAB-M1 — Bedside transfusion verification · MAJOR CLOSED

`record_transfusion` took a bare `unit_id`. It now runs the
BCMA-pattern bedside check: wristband patient id (must resolve to
the issued unit's patient), the patient's date of birth (must match
the Patient resource), the unit id, and pre-transfusion vitals
(temperature 30–45 °C, pulse 20–250, systolic 40–300, diastolic
20–200 and < systolic, respiratory rate 4–60), plus optional second
checker and start/stop times. Any failure is a 422 naming every
failed check, writes nothing, and commits an audit row
(`lab.bloodbank.transfusion.verification_refused`) before refusing.
Success records the verification block (identifiers checked,
verified_by, second checker, verified_at) and the vitals as
extensions on the Procedure, with `performedPeriod` start/stop; the
unit moves to transfused as before.

**Evidence:** test_wave_a_lab.py — DOB mismatch refused + audited,
wristband mismatch refused, missing vitals 422, out-of-range vital
named, happy path asserts the verification + vitals extensions and
performedPeriod on the Procedure.

## 3. PHARM-B1 (PHARM-1) — Dispense requires a current verification · BLOCKER CLOSED

Dispensing never consulted the pharmacist verification record. Now:

- `PharmacyVerification` carries `status` (active/revoked),
  `revoked_by/at/reason`, `order_version`, and override fields
  (additive columns via the inspector-guarded
  `pharmacy_ops.ensure_pharmacy_verification_columns`, wired in
  `app.main` beside the org-placement precedent). **Current** is a
  single source of truth (`pharmacy_ops.verification_state` /
  `current_verification`): a row exists, is active, its
  `order_version` equals the order's current `FhirResource.version`,
  and `verified_at` is inside the governed validity window. States:
  current | none | revoked | stale | expired — each with a human
  reason quoted verbatim in refusals.
- `POST /pharmacy/dispense` requires a current verification after
  the existing read-only checks and **before** stock/FEFO writes; a
  refusal commits an audit row
  (`pharmacy.dispense.refused_unverified`) and raises 409 naming the
  state — zero state change. One deliberate subtlety, proven by
  test: dispensing itself bumps the order version (its status
  transition), so the dispense **re-binds** the verification across
  its own transition; any *other* edit still stales it (and then
  both dispense and administration refuse).
- `POST /pharmacy/verification/{order_id}/revoke` (pharmacy.verify,
  reason required, audited) returns the order to the verification
  queue; `_verified_order_ids` now means *current* verifications, so
  revoked/stale/expired orders reappear in the queue automatically.
- The dispense queue annotates every item with its verification
  state; the Pharmacy page shows it, disables Dispense **with the
  reason shown** when not current, and surfaces revocation inline.

**Studio surface:** `terminology/pharmacy-ops` gained
`verificationValidityHours: null` (no time expiry unless a site
publishes one).

**Evidence:** `backend/tests/test_wave_a_pharmacy.py` proves
unverified dispense → 409 + audited + zero writes (no dispense row,
stock unchanged, request still active); verified → 201; revoked →
409 naming the revocation; order edited after verification → 409
stale.

## 4. PHARM-M1 — Verification hard stops + DUR dispositions · MAJOR CLOSED

`verify_order` refused only on PGx / non-formulary / unacknowledged
restriction — a recorded-allergy conflict or a monitoring blocker
waved through, and the DUR queue was display-only. Now verification
computes **hard-stop signals** — allergy conflict
(`orders._allergy_conflict`, the chart's single AllergyIntolerance
source), medication-monitoring **blockers** (warnings still do not
block), and the formulary **high-alert** flag — and refuses 422
naming each signal unless it is covered by a recorded DUR
**override disposition** or a payload override whose reason code is
in the governed `durOverrideReasonCodes` with a non-empty note
(bogus code → 422). Overrides are recorded on the verification row
(reason code, note, the overridden signal list with its route) and
audited. New `PharmacyDurDisposition` table +
`POST /pharmacy/dur/{order_id}/disposition` (pharmacy.verify); DUR
queue items carry their signals, keys, and dispositions, and the
Verification tab renders per-signal disposition actions (disabled
**with the reason shown** when no reason codes are governed) and the
override fields. The PGx refusal still runs first, unchanged;
interaction checking remains inert by evaluated decision (BD-70) —
findings flag, they do not refuse, exactly as before.

**Studio surface:** `durOverrideReasonCodes` in
`terminology/pharmacy-ops` (benefit-outweighs-risk,
allergy-cross-reactivity-assessed, monitoring-plan-in-place,
specialist-direction, patient-previously-tolerated); `highAlert:
true` on the seeded formulary's apixaban and oxycodone entries (the
high-alert classes present in the seed).

**Evidence:** test_wave_a_pharmacy.py — allergy conflict 422 naming
the allergen; monitoring blocker 422; governed override → 201 with
the override recorded on the row and in audit; bogus code 422;
high-alert without override 422; recorded disposition satisfies its
signal at verify.

## 5. PHARM-M2 — One administration path · MAJOR CLOSED

The Pharmacy page's med-admin was a weak 3-rights typed-ID path
(`POST /pharmacy/administer`) beside the real five-rights BCMA. The
weak path is **removed** (endpoint and its config machinery deleted;
the endpoint now 404s, pinned by test). The only engine is
`POST /pharmacy/bcma/administer`, which additionally gates on a
**current** verification of the authorizing order at administration
time (refused scans audited `pharmacy.bcma.scan_refused`, 409
naming the verification state) — so a verification revoked after
dispensing still stops the dose at the bedside. The Pharmacy page
med-admin is the five-rights scan form (wristband + medication-label
payloads, auto-filled after a dispense).

**Evidence:** test_wave_a_pharmacy.py (BCMA with revoked
verification refused + audited; retired path 404) plus the migrated
BCMA/MAR suites (test_phase11_bcma, test_phase5_mar_profiles,
test_phase15_barcodes) running verify → dispense → BCMA-administer
end-to-end.

## 6. CHART-B2 — Allergies visible + editable in the chart · BLOCKER CLOSED

The chart never fetched AllergyIntolerance. Now a new
**Allergies tab** (`web/src/pages/chart/AllergiesTab.tsx`) lists the
patient's AllergyIntolerance resources (substance, criticality,
reactions, clinical + verification status, recorded date/by) with
add and edit (status transitions, verification status) through the
generic FHIR paths, client validation with inline errors, gated by
the chart's write capability. **NKDA is a recorded posture**: an
AllergyIntolerance coded `no-known-allergy`, distinct from an empty
list, which reads "allergies not recorded" — the two states are
visually different in the tab and the new **chart-wide allergy
banner** (active substances, or NKDA, or not-recorded) in the chart
header. The Orders tab shows the same active-allergy posture above
the order form, before placement. This is the single source the
diet tray-check, ordering hard stop, DUR, and verification all read.

**Evidence:** `backend/tests/test_wave_a_chart.py` — an allergy
created through the chart's path is seen by `_allergy_conflict` and
stops a conflicting order placement (422) while a non-conflicting
order passes; NKDA representation test. Web contract tests in
`WaveAChart.test.tsx`.

## 7. CHART-B3 — Structured order sig builder · BLOCKER CLOSED

The order form sent type + free-text code only, although the backend
accepted `dosageInstruction`. Medication orders now get a structured
sig builder (dose + governed unit, route, frequency, duration,
PRN + required PRN reason, indication) that composes a FHIR
`dosageInstruction` in exactly the shape the pharmacy dose readers
already parse — `doseAndRate[0].doseQuantity {value, unit}`, route
coding, `timing.repeat {frequency, period, periodUnit}` (PRN carries
a timing code only) — plus a human-readable sig text shown live and
stored as `dosageInstruction[0].text`. The server validates the
structure at placement (`orders.py`): dose > 0, unit and route in
the governed sets, timing present, text present — violations are a
422 **naming the field**. Text-only dosage remains accepted for
backwards compatibility. The sig displays in the order list and as a
Sig column on the MAR.

**Studio surface:** new governed terminology key
**`medication-sig`** (routes, frequencies with times-per-day, dose
units), seeded published-by-default through the orders module's
`ensure_orders_defaults`; `/orders/types` returns it additively so
the web form renders the *published* vocabularies (the web contract
test runs against the real response shape).

**Evidence:** test_wave_a_chart.py — valid sig persisted verbatim;
zero dose / unknown unit / unknown route / missing frequency each
422 naming the field; text-only dosage still accepted.
WaveAChart.test.tsx — builder renders the governed options, inline
validation errors, sig composition.

## 8. WF-B1 — Credential primary-source verification · BLOCKER CLOSED

`StaffCredential` was a self-asserted row and `check_privileges`
treated "row exists + unexpired" as verified — the system asserted
verification it never performed. Now verification is a recorded,
governed, audited act:

- Two new tables (no existing table altered):
  `CredentialVerification` (source, method, verifier, verified_on,
  outcome verified/unable-to-verify/discrepancy, evidence, note) and
  `CredentialExclusionCheck` (kind, outcome clear/hit/not-run,
  checker, date, evidence). Endpoints
  `POST/GET /people/credentials/{id}/verifications` and
  `POST/GET /people/staff/{id}/exclusion-checks`
  (workforce.work writes, workforce.read reads; audited
  `workforce.credential.verify` / `workforce.exclusion.check`).
  Validation is against the **published** config: unknown source
  code → 422 naming it; future verified_on → 422; outcome
  "verified" without evidence → 422.
- Credential state is **computed at read time** (the tree's posture):
  `exclusion-hit` > `unverified` > `verification-expired` (verified
  but older than the governed `reverificationDays`) > `verified`,
  and rides `_credential_out` and the Workforce credentials tab
  (badge with source + date; record-verification and exclusion-check
  forms with selects from the published config; a select whose list
  failed to load is disabled with the reason shown).
- `check_privileges` now requires the satisfying credential to be in
  state **verified**, and demands the published
  `requireExclusionKinds` whenever privileges require credentials.
  New problem strings name the gap exactly: `no verified <type> on
  file (credential unverified)`, `… (PSV expired — re-verify)`,
  `… (exclusion hit: <kind>)`, `missing exclusion check: <kind>`.
  All pre-existing problem strings and the credential-expiry
  judgement are preserved verbatim (pinned by test);
  `StaffPrivilege`'s docstring claim finally matches the code.

**Studio surface:** the workforce terminology payload gained a
governed `psv` section — `sources` (state-licence-board,
certifying-board, medical-school, npdb-query, dea-registration,
employer-reference), `exclusionKinds` (licence-board, npdb, oig,
sam), `reverificationDays: 730`, `requireExclusionKinds:
[licence-board, oig, sam]` — with the governed-append upgrade
republishing pre-existing payloads (old version superseded, audit
rows, everything else preserved).

**Evidence:** `backend/tests/test_wave_a_workforce.py` (14 tests)
proves the wave gate addition end-to-end: a self-asserted credential
does **not** satisfy `check_privileges`; after a recorded PSV it
does; stale PSV names re-verification; a discrepancy outcome does
not satisfy; an exclusion hit on a required kind names the kind; a
missing required kind is a problem; the POST 422s fire as specified.

## 9. Item 9 — Analytics "View indicator" appeared dead (user-reported)

Root cause: the seeded system publishes 24 indicator definitions;
selecting one loaded its value correctly (endpoints and shapes
verified consistent), but the detail card rendered **after** the
full 24-row list — on a real screen, viewports below the click —
with no scroll, no focus move, and no visible change at the click
site beyond `aria-pressed`. Indistinguishable from a dead button,
and a violation of the visible-workflow-state standard. Fix
(`AnalyticsPage.tsx` + `styles.css`): the detail card renders
**above** the published-indicators list once a selection exists;
selection scrolls it into view (`scrollIntoView`, smooth) and moves
keyboard focus to its heading; the clicked row/button carry the
selected treatment (accent pressed-button + highlighted row, the
master-detail tokens). Regression tests
(`AnalyticsPage.indicatorFocus.test.tsx`, 2) pin the document
position, the scroll call, the focus landing, and the selected
state moving with the selection; the existing drill tests stay
green.

## Configuration-coverage list (Studio surfaces added/extended)

| Surface | Kind / key | What Wave A added |
|---|---|---|
| Lab catalog settings | terminology / lab catalog | `bloodBankGate` (screen validity window, eXM enablement + products, emergency-release reason codes) + governed-republication upgrade |
| Pharmacy operations | terminology / `pharmacy-ops` | `verificationValidityHours`, `durOverrideReasonCodes` |
| Formulary | terminology / `formulary` | `highAlert` flag on seeded apixaban + oxycodone entries |
| Medication sig vocabularies | terminology / `medication-sig` (NEW key) | routes, frequencies (with times/day), dose units |
| Workforce | terminology / workforce payload | `psv` section (sources, exclusion kinds, reverification window, required exclusion kinds) + governed-append upgrade |

No new config *kinds* were needed; all Wave A governance rides the
existing draft → publish machinery, seeded published-by-default
following the established pattern. The ABO/Rh compatibility matrix
is a clinical standard and is deliberately code, not configuration.

## Compliance-coverage list

- **Transfusion safety** — issue-time ABO/Rh + crossmatch +
  antibody-screen gate and bedside two-identifier verification
  evidence the register's audit-controls family (HIPAA 45 CFR
  164.312(b)): every refusal, emergency release, verification, and
  bedside-verification refusal appends a hash-chained audit entry
  (`lab.bloodbank.unit.issue`, `…emergency_issue`,
  `lab.bloodbank.transfusion`, `…verification_refused`) reviewable
  in the Studio audit view; capability gates (lab.work/lab.verify)
  evidence minimum-necessary access (164.502(b)).
- **Medication verification** — pharmacist verification as a
  precondition of dispensing and administration, verification
  revocation, and DUR override capture evidence the same audit
  family (`pharmacy.verify`, `pharmacy.dispense`,
  `pharmacy.dispense.refused_unverified`,
  `pharmacy.bcma.scan_refused`, disposition + revoke actions) with
  role gates (pharmacy.verify/dispense/administer) as the access
  control.
- **Credentialing / primary-source verification** — PSV records +
  exclusion checks evidence workforce governance: verification is an
  attributed, dated, evidence-carrying act
  (`workforce.credential.verify`, `workforce.exclusion.check` audit
  actions), privilege decisions consume only the verified state, and
  the demanded sources/kinds/windows are published Studio
  configuration — the posture UCSF Credentialing Policy 5.01
  describes (enumerated file contents, kept current).

## Adjusted existing tests (registered; no assertion weakened)

- LAB: `test_phase6b_lab.py`, `test_phase15_lab_depth.py` — fixtures
  now result a released type-&-screen before issue and send the
  LAB-M1 bedside payload (the governed behaviour, not a bypass).
- PHARM: `test_phase4a_pharmacy.py`, `test_phase11_pharmacy_ops.py`,
  `test_phase11_bcma.py`, `test_phase5_mar_profiles.py`,
  `test_phase15_barcodes.py`, `test_phase15_packs.py`,
  `test_phase15_oncology.py`, `test_phase17_mgkg.py`,
  `test_phase9_specialty_deep.py`, `test_realignment.py` — fixtures
  verify before dispensing/administering; BCMA-lite tests rewritten
  onto the surviving scan path; the retired `bcmaRights`-subset
  premise in test_realignment.py replaced by a scan-is-the-only-
  engine assertion (the weak path it configured no longer exists).
  `test_phase11_pharmacy_ops.py`'s restricted-acknowledgement case
  now also exercises the apixaban high-alert override it newly
  triggers. Two more were fixed by the coordinator at the gate:
  `test_page_contracts.py` — the verification-queue exact-key pin
  moved to the evolved contract (the queue additively carries
  `dur_override_reason_codes` + `verification_validity_hours`, which
  VerificationTab consumes); `test_phase17_interactions.py` — the
  apixaban verify now supplies the governed high-alert override
  (interaction findings still flag-only, BD-70 unchanged).
- CHART: none.
- WF: `test_workforce_teams.py`, `test_frontdoor.py`,
  `test_phase15_coordination.py`, `test_phase15_inbox.py` — fixtures
  record a real PSV + clear exclusion checks where they demand
  privileges; the gate was not loosened.
- Item 9: none (new test file only).

## Boundaries held

Drug-interaction checking untouched (inert by evaluated decision,
BD-70); the DUR reviewer agent untouched (honest-off); compounding
competency records untouched (Wave F); Inventory untouched; the
parked `agents/` folder and `docs/AGENTS.md` untouched.
`web/src/api/client.ts` retains an `administer` method with no page
callers (its endpoint is retired) — noted for a later cleanup wave,
not silently removed here.

---

## Parent addendum (2026-10-21, parent re-verification)

The coordinator's gate was re-run IN FULL by the parent (129 backend
files, 22 batches, live legs in sequence; web suite; build; collision
audit). First parent run: backend 1,510 passed / 1 failed, web
573/573, build exit 0, collisions ZERO. The single failure was
investigated to root cause before any sync:

- FAILED (batch 2, 23:11 PDT): tests/test_inventory.py::
  test_expiry_worklist_and_governed_window. NOT a Wave A regression
  and NOT production code: the server computes day counts from the
  UTC date (app/inventory.py _today; the codebase-wide convention —
  access, lab, lab_benches, infection_prevention, support_services
  all do the same), while the test's _future/_past helpers used the
  LOCAL date. At 23:11 PDT the UTC date is already the next day, so
  every exact day-count assertion was off by one. Morning runs and
  the coordinator's run (different process timezone) agree and pass —
  a deterministic evening failure, reproduced on demand under
  TZ=Pacific/Kiritimati (local date a full day ahead of UTC).
- A sweep of every test file using local date.today() under full
  date divergence found ONE more exposure of the same class:
  tests/test_wave_a_workforce.py pinned TODAY = date.today() and
  posted 'verified_on' dates the server then rejected as future —
  all 14 of its tests failed under divergence. The other six files
  (depth journeys, depth lab, pharmacy ops, phase-15 lab depth,
  ux checkin, workforce onboarding) are timezone-clean.
- FIX (test files only; no production code changed): both files now
  derive fixture dates from the UTC date, with the reason recorded
  in comments at the fix sites. Proofs: test_inventory.py 32/32 and
  test_wave_a_workforce.py 14/14 under TZ=Pacific/Kiritimati AND
  under TZ=America/Los_Angeles; batches 2 and 22 re-run green on the
  fixed tree with the full live env (96 and 52 passed).
- Reconciled parent total: backend 1,511 passed / 0 failed /
  0 errors — the coordinator's number, matched. Baseline was 1,448;
  Wave A added 63.

NAMED OPEN QUESTION (product, for Min — not resolved by this fix):
the codebase counts clinical days in UTC. For a single-site
local-first deployment, expiry windows, credential validity, and
TAT clocks arguably belong to a governed FACILITY timezone. That is
a Studio-configuration decision (one facility-timezone setting the
date helpers read), recorded here and in the programme ledger; the
test fix above deliberately follows the existing convention rather
than smuggling the change in.

NOT RUN (unchanged): browser/rendered check on this VM — Min's Air
click-through after Air Sync Run 13 is the acting browser gate.
Release remains HELD; nothing pushed to GitHub.
