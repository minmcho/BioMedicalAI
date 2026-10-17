# Phase 7 SQA Report — Access & Full Revenue Cycle

**Date:** 2026-10-17 · **Scope:** `docs/FULL_SYSTEM_ROADMAP.md` §3 Phase 7
+ §2 inventory rows for access/revenue · **Rule:** §6 — configure, don't
fork. Synthetic data only.

Phase 7 closes the access and revenue-cycle gaps on the existing
machinery: referrals/authorisations and eligibility in a new
`backend/app/access.py` (router `/access`), HIM coding + 835 remittance
+ denials + statements in a new `backend/app/revenue.py` (router
`/billing`, alongside the Phase 4A claims module — no fork of the
lifecycle, fee schedule, or appeal machinery). The order-signing path in
`app/orders.py` gained the authorisation gate; `app/billing.py` gained
the coding feed and the optional coding-required gate. Backend version
0.7.0, health phase "7".

## Gate results (all re-run at delivery)

| Gate | Result |
| --- | --- |
| Backend pytest (full suite) | **273 passed** (249 baseline + 24 new Phase 7 tests) |
| Web vitest (full suite) | **184 passed / 28 files** (175 baseline + 9 new) |
| Web production build (`npm run build`, tsc + vite) | **clean** |

Per-file new tests: `tests/test_phase7_access.py` (13),
`tests/test_phase7_revenue.py` (11), `web/src/pages/BillingPage7.test.tsx`
(4), `web/src/pages/SchedulingAccess.test.tsx` (5).

## What landed, in task order

### 1. Referrals & authorisations (`app/access.py`)

- **Referral = FHIR ServiceRequest + triage Task.** Intake accepts an
  external-style payload (`POST /access/referrals`) and stores a real
  ServiceRequest (category `urn:opencare:referral`, deliberately **no**
  order-type extension, so referrals never appear on CPOE surfaces)
  plus a linked Task whose status mirrors the referral's place in the
  published `workflow/referral-flow` machine:
  received → triaged → authorised / denied → scheduled → completed
  (+ cancelled), driven by the SHARED workflow loader — per-transition
  roles from config are enforced (authorise/deny restricted to
  admin + clinician in the seeded machine; test-proven: a viewer attempt
  is 403). Triage queue API (`GET /access/referrals/queue`) + a
  Scheduling-page section with intake form and transition buttons.
- **Prior authorisations** are records, not FHIR (no published auth
  resource profile in scope): auth number, payer, service, approved
  units, validity window, used units — `POST /access/authorisations`,
  approve/deny via the published `workflow/prior-auth-flow`.
- **The signing gate.** A service listed in ANY active payer's
  `authRequiredServices` (published `terminology/payers`) cannot be
  signed without an approved authorisation covering it (validity window
  + units remaining): signing returns **422 naming the missing
  authorisation**. With a covering auth, the order links it
  (`urn:opencare:extension/prior-auth`) and consumes one unit. The
  override path follows the Phase 5 allergy pattern exactly:
  `auth_override_reason` proceeds and is recorded on the order
  (`urn:opencare:extension/auth-override`) + audited. The gate checks
  the UNION over active payers — a deliberate fail-safe (no Coverage
  resource exists yet, so the system cannot know which payer insures a
  patient; recorded as a design note below). The seeded payer config
  ships `authRequiredServices: []`, so the gate is **inert by default**
  and existing behaviour is byte-preserved; a config-flip test publishes
  a payer requiring auth for `imaging` and proves block → auth →
  link/consume → override.
- **Payers are published terminology**: code, display, eligibility
  figures (copay, coinsurance %, covered services) and the
  auth-required list all live in the concept payload. Two synthetic
  payers seeded.

### 2. Eligibility (`app/access.py`)

- `POST /access/eligibility/check` evaluates patient + payer + optional
  service/date **deterministically against the published payer config**
  and returns covered/not-covered + copay/coinsurance FROM that config.
  Unknown payer → 422. Services outside a payer's `coveredServices`
  (when published) return covered=false.
- Every check is **recorded** (timestamp, requester, result) in
  `eligibility_checks` and audited; the response carries
  `basis: "local-config"` and an explicit disclaimer. API and UI both
  label it a local configuration check — **never presented as a
  real-time payer response**; the Scheduling-page section states "no
  live payer connection (no EDI 270/271)" in its copy and renders the
  API disclaimer verbatim.

### 3. HIM / coding (`app/revenue.py` + `app/billing.py`)

- **Coding workqueue**: encounters that are finished OR have ≥1 signed
  (final) note and have no CodingRecord — `GET /billing/coding/queue`.
  Coders assign ICD-10-CM diagnoses + CPT procedures validated against
  the published `terminology/coding-diagnoses` /
  `terminology/coding-procedures` value sets (unknown code → 422 naming
  the code; display/system resolved server-side from config). One
  record per encounter (re-assign → 409; amend via PUT). Assignment and
  amendment are audited.
- **Coded encounters feed claim generation**: `_generate_claim`
  consumes the CodingRecord — coded diagnoses become the claim's
  diagnosis section (replacing the problem list), coded procedures
  become fee-schedule-priced lines tagged
  `charge-source: CodingRecord/{encounter_id}`; chart Procedures with
  the same code are de-duplicated (never double-charged, never
  silently dropped — problem-evaluation lines are still charged).
  Exact-math test: coded visit claim totals **USD 292.00**
  (E11.9 problem-evaluation 55.00 + I10 problem-evaluation 60.00 +
  CPT 99214 110.00 + CPT 93005 45.00 + CPT 85025 22.00).
- **Coding-required gate**: `requireCoding` is an optional boolean in
  the published `workflow/claim-lifecycle` payload, **default off** —
  Phase 4A behaviour byte-preserved. With it published on, generating a
  claim for an uncoded encounter returns **409**; config-flip test
  proves block → code → generate.

### 4. 835 remittance (`app/revenue.py`)

- `POST /billing/remittance/835` accepts the raw X12 text. A real
  parser handles ST/SE (with the SE segment count **validated**), BPR,
  TRN, N1/NM1 payer identification, and per-claim CLP/SVC/CAS.
  **Malformed file → 422 with segment context and NOTHING posts**
  (parse fully first; posting is one transaction).
- Posting matches CLP01 to our claim ids and writes a remittance
  record onto the claim (supportingInfo, category `remittance`):
  paid amount, contractual + other adjustments (CAS CO/OA), patient
  responsibility (CAS PR), check number, outcome. Claim status is set
  **directly** from the CLP02 mapping in published
  `terminology/remittance-status` (1/2/3/19 → paid; 4 → denied) — this
  is a payer system event, deliberately bypassing the human lifecycle
  machine (documented design decision; the outcome is also recorded in
  the remittance record). Paid with remaining balance →
  `partially-paid`. Balance = total − paid − non-PR adjustments −
  patient responsibility, derived by one `claim_financials()` helper
  used by every money view.
- **Unmatched lines are never dropped**: unknown claim ids, unmapped
  CLP02 codes, claims in non-postable states (draft/ready) and
  duplicate checks (same TRN02) land in `remittance_exceptions` with
  the verbatim CLP01 + reason. Exact-math fixture test: a 3-CLP file
  posts paid 292→ balance 0.00, partial 240 − 180 − 30 (CO) − 20 (PR)
  → balance **10.00** → partially-paid, denied 52 → denied; a second
  file with an unknown claim + duplicate check yields 2 exceptions and
  posts nothing twice.
- Billing page: paste-and-post section, batch table, exceptions table.
  Copy states plainly that posting is local — **no clearinghouse
  connection exists**.

### 5. Denials workqueue (`app/revenue.py`)

- The queue is **derived** from claim state (status denied, last
  remittance outcome denied, or partially-paid with adjustments) — no
  second store of truth. Denial reasons are the verbatim CAS group +
  reason codes from the remittance, categorised through published
  `terminology/denial-categories` (CARC → category map seeded;
  unmapped reasons surface as `uncategorised`, never hidden).
- Work actions: assign, note (append-only), resolve with an outcome
  from the published outcomes list. **`appealed` requires a real appeal
  through the existing Phase 4A flow** (appeal count respected —
  appeal-once semantics unchanged); **`rebilled` requires naming the
  corrected claim**. Rebill (`POST /billing/claims/{id}/rebill`) is
  restricted to denied/partially-paid claims and creates a **new draft
  claim** linked via `urn:opencare:extension/rebill-of`; the original
  is never written. Billing page section shows reasons + financials,
  assignment, notes, rebill button and outcome resolution.

### 6. Access/scheduling gaps (§2)

- **Waitlist** (§2 marks patient self-scheduling partial; the waitlist
  was missing): config-driven queue — patient (or staff on their
  behalf) requests a slot type from published
  `terminology/slot-types` (`POST /access/waitlist`), entries ordered
  priority → age. On a cancellation, staff offer the freed slot
  (`POST /access/waitlist/offer/{appointment_id}`): only a **cancelled**
  Appointment qualifies (409 otherwise), matched by appointmentType /
  serviceType code against the head entry's slot type. Accepting an
  offer books a **real Appointment** (`POST .../accept`); declining
  returns the entry to waiting. Lifecycle = published
  `workflow/waitlist-flow`. Patients act only on their own entries
  (mirrors the Phase 4A billing carve-out). Scheduling-page section:
  join form, queue table, offer form, accept & book.
- **Patient statements** (revenue-side access): `GET
  /billing/statements/{patient}` — per-claim billed/paid/adjusted/
  patient-responsibility/patient-paid/due/balance from posted
  remittances; patients read only their own. **Patient payments** are
  recorded as ledger entries (`POST /billing/claims/{id}/patient-payment`,
  method validated against published `terminology/payment-methods`,
  overpayment of the patient share → 422). API + UI both state there
  is **no payment gateway** — this records payments taken, it does not
  process cards.
- Self-scheduling beyond the waitlist (patient-picked open slots) is
  NOT built: §2 lists patient self-scheduling as partial via the
  existing request flag; Phase 7 scope named the waitlist, which is
  what landed. Stated here rather than implied.

## §6 configuration-coverage list

Everything a site would change to make access + revenue cycle *theirs*
is Studio configuration data (published, versioned, governed):

| Behaviour | Configuration |
| --- | --- |
| Payer list, eligibility figures (copay / coinsurance / covered services) | `terminology/payers` concept payload `eligibility` |
| Which services require prior authorisation | `terminology/payers` concept payload `authRequiredServices` (seeded empty = gate inert) |
| Referral service catalogue | `terminology/referral-services` |
| Waitlist slot types | `terminology/slot-types` |
| Referral lifecycle + per-transition roles | `workflow/referral-flow` |
| Prior-auth lifecycle + roles | `workflow/prior-auth-flow` |
| Waitlist lifecycle + roles | `workflow/waitlist-flow` |
| ICD-10-CM coding value set | `terminology/coding-diagnoses` |
| CPT coding value set | `terminology/coding-procedures` |
| Denial categories (CARC map) + resolution outcomes | `terminology/denial-categories` (`categories`, `outcomes`) |
| 835 CLP02 → outcome mapping | `terminology/remittance-status` |
| Patient payment methods | `terminology/payment-methods` |
| Coding-required-before-claim gate | `workflow/claim-lifecycle` payload `requireCoding` (default off) |
| Who may read/work access + coding queues | `permissions/role-matrix` capabilities `access.read`, `access.work`, `coding.read`, `coding.work` |

New config kinds: none. New capabilities: 4, added via the realignment
pattern (backend defaults + web `CAPABILITY_DEFAULTS` mirror — the
models test count moved 26 → 30 with the four named, the documented
mechanism-change pattern). Proxies: `/access` added to the Vite dev
proxy and nginx (new top-level prefix only).

## Mechanism changes to existing behaviour (with justification)

1. **`validate_workflow` accepts an optional boolean `requireCoding`**
   in the claim-lifecycle payload. Additive and default-off; without
   it the coding-required gate could not be config (the §6 rule).
2. **Claim generation consumes a CodingRecord when one exists**
   (diagnoses replace the problem-list section; coded procedures priced
   and chart duplicates de-duplicated). Encounters without a
   CodingRecord generate byte-identically to Phase 4A (pinned by the
   unchanged 249-test baseline, incl. the $83 solo-practice superbill).
3. **Remittance posting sets claim status directly**, bypassing the
   human lifecycle machine — a payer adjudication is a system event,
   not a staff transition; the outcome + full money record are stored
   on the claim and audited.
4. **Existing web test doubles** (`BillingPage.test.tsx` stub) gained
   the Phase 7 client methods returning empty states, so the Phase 4A
   assertions keep testing the claims UI they were written for while
   the new sections mount beside it.
5. `BillingPage` statement section now renders the API-returned
   disclaimer verbatim (found missing during SQA of this phase).

## Design notes (recorded, not hidden)

- The auth gate checks the union of active payers' required services
  because no Coverage resource exists yet; when Coverage lands, the
  gate should narrow to the patient's own payer. Until then it fails
  safe (more likely to ask for an auth than to miss one), and it is
  inert until a site publishes a non-empty list.
- Local codes where no standard set is in scope: denial categories map
  standard CARC codes (published config, real X12 semantics); coding
  sets are curated ICD-10-CM / CPT subsets mirroring the fee schedule,
  extensible by publishing new terminology versions.
- Eligibility responses are synthetic by construction: figures come
  from the site's own published payer config, and the stored record +
  `basis: "local-config"` make that auditable.

## NOT RUN / not claimed

- **Live payer EDI 270/271 eligibility** — NOT RUN, by design: no
  payer network integration exists; eligibility is a local-config
  evaluation, labelled as such in API + UI.
- **Clearinghouse submission / live 835 retrieval** — NOT RUN, by
  design: the 837 export (Phase 4A) and 835 ingest (this phase) are
  format-level, local only; nothing is transmitted anywhere.
- **Browser click-through E2E** — NOT RUN (same standing gap as prior
  phases; web behaviour is covered by component tests with stub
  clients at the network boundary).
- **Postgres runtime** — NOT RUN (no Docker on this VM; SQLite test
  path only, same caveat as every prior phase).
- **Payment processing** — none exists by design (ledger recording
  only), stated in API responses and UI copy.

NOT published to GitHub / NOT synced to any Mac — staging only, per
instruction. `mobile/`, `smart-app/`, `warehouse/`, `agents/` untouched.
