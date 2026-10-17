# Phase 9 SQA Report — Specialty Operations + Engagement + Quality/Research

Date: 2026-10-17. Scope: roadmap §4 (the 11 specialty operations) +
§3 Phase 9 + §6 config rule. Staging only — NOT published to GitHub,
NOT synced to any Mac, per instruction. Synthetic data only.

## Gate results (all re-run at delivery)

- Backend: **324 passed** (297 Phase 8 baseline + 27 new Phase 9
  tests; zero regressions, no existing test weakened — one existing
  web test's capability-count expectation and two text assertions
  were updated for content this phase legitimately added, see
  "Mechanism changes").
- Web: **202 passed / 32 files** (187 baseline + 15 new).
- `npm run build`: **clean** (tsc --noEmit + vite build).

New backend suites: `tests/test_phase9_packs.py` (6),
`tests/test_phase9_specialty_deep.py` (6),
`tests/test_phase9_engagement.py` (7), `tests/test_phase9_quality.py`
(8). New web suites: `src/api/phase9.test.ts` (5),
`src/pages/PortalPage.test.tsx` (4), `src/pages/InboxPage.test.tsx`
(3), plus 3 new tests in `PopHealthPage.test.tsx`.

## What landed, in task order

### Part A — the 11 specialty packs (`backend/packs/specialty-*.pack.json`)

Eleven governed Studio packs, one per service line in roadmap §4:
oncology, cardiology, diabetes-endo, gastroenterology, geriatrics,
pulmonology, obgyn, ophthalmology, orthopedics, psychiatry,
rehabilitation. Every pack carries the §4 shape: order set(s),
specialty intake/assessment form(s), flowsheet(s), a terminology
subset, a registry cohort, and its modules — all as Studio
configuration that installs as DRAFT and enables only through the
existing approve → attach → enable governance (test-proven: enable
from draft is a 409; a pack never self-enables). Enabling applies
every declared configuration atomically (validate-all first, the
profile-pack pattern). Tests prove for ALL eleven: install +
validate + enable, every order set places real orders, every form
validates a complete submission through the ordinary form machinery,
every registry is listed. DEEP end-to-end proof for three (below);
the other eight carry the structural proof just described — stated
plainly, per the brief.

Per-specialty §4 fidelity notes:

- **Oncology** — regimen-style order sets: cycle/day structure rides
  each order item's note (e.g. "Cycle 1 Day 1 — chemotherapy");
  supportive-care set; intake (site/histology/stage/TNM/ECOG),
  toxicity-grading and tumour-board forms; cancer registry cohort.
- **Cardiology** — ACS pathway + heart-failure clinic order sets
  (device/echo work rides the existing imaging/procedure order
  types); HEART-score risk form (5 items, 0–2 each); echo form;
  HF flowsheet (daily weights).
- **Diabetes & endocrinology** — does NOT duplicate the Phase 5
  seeded `diabetes-admission` order set (test-asserted); adds
  insulin-titration + screening-bundle sets, diabetes intake +
  glucose flowsheet, and its own registry cohort key
  (`diabetes-endo-registry`).
- **Gastroenterology** — endoscopy-prep + IBD-flare sets; endoscopy
  report form with prep-quality; IBD flowsheet.
- **Geriatrics** — CGA form (cognition/ADL/IADL/frailty/
  deprescribing/advance-care-plan), delirium (CAM) form, falls +
  delirium admission set.
- **Pulmonology** — COPD exacerbation + sleep-study sets; intake
  with mMRC + inhaler-technique check; PFT form; action-plan form.
- **OB/GYN** — prenatal-visit + postpartum sets; intake
  (gravida/para/LMP/EDD/risk), prenatal flowsheet (fundal height,
  fetal heart rate, glucose screening), delivery record (Apgar
  1/5, blood loss).
- **Orthopedics** — joint-replacement pathway + fracture-clinic
  sets; intake with bone-health/fracture-risk screen; PROM
  flowsheet.
- **Ophthalmology (DEEP, Bascom Palmer pattern)** — the pack
  publishes the order-type catalogue with an `eye-procedure` type
  whose `requiredFields` make `bodySite` mandatory: placing an eye
  procedure without laterality is a **422**. The cataract pathway
  set lands bodySite (eye structure + OD laterality codings) on
  every eye order; the injection-clinic set is bilateral-aware (one
  order per eye, OD + OS). The imaging-centric exam form (visual
  acuity OD/OS, IOP OD/OS, OCT/fundus flags, REQUIRED laterality
  item) validates through the ordinary form machinery — the same
  validator the web FormRenderer targets — and a submission missing
  laterality is a 422. Laterality is enforced by configuration
  (catalogue requiredFields), not by an ophthalmology code path.
- **Rehabilitation (DEEP, Shirley Ryan pattern)** — FIM-shaped
  functional assessment (15 items scored 1–7) and therapy-minutes
  (PT/OT/speech delivered minutes per therapy day) as flowsheets:
  entries submit and read back as time series, functional gain
  visible between admission and follow-up entries (test-proven,
  incl. a 135-minute therapy day total).
- **Psychiatry (DEEP)** — the pack publishes a formulary extension
  (clozapine, lithium) and `terminology/medication-monitoring`
  rules evaluated by a small GENERIC hook in `pharmacy.dispense`
  (the Phase 2 QC-gate pattern: a published rule, a hard stop, no
  silent pass). Clozapine dispensing BLOCKS (409) with no ANC on
  file, with ANC below 1.5, with an ANC older than 28 days, and
  when the only ANC carries the lab-unreleased tag (unreleased
  results never count as monitoring evidence); a current in-range
  ANC lets the dispense proceed (stock 30 → 29 test-proven).
  Lithium's rule action is `flag`: the dispense proceeds and
  carries `monitoring_warnings`. With no rules published (no pack),
  dispensing behaviour is byte-for-byte the Phase 4A behaviour —
  the gate is inert, test-proven.

### Part B — messaging + telehealth + self-scheduling (`backend/app/engagement.py`, new)

- **Secure messaging**: patient ↔ care-team messages as FHIR
  Communication resources in per-patient threads (thread, sender
  role and read-by carried as extensions). Staff inbox per patient
  (`GET /messages/inbox?patient=`), thread detail, reply, read
  receipts (explicit — replying does not mark read), unread counts;
  the web Inbox page shows the selected patient's threads next to
  their results. Portal: patients send/read/reply ONLY their own
  threads — another patient's thread is a **404** (non-disclosure),
  test-proven on read, reply and read-receipt. Staff access is
  capability-gated (`messages.use`); portal endpoints are
  patient-role only.
- **Telehealth**: `telehealth` joins the published slot types as
  governed config (a governed append to the published payload, the
  Phase 6 note-types pattern), with a `telehealth` note type +
  `note-telehealth` template form likewise appended. A telehealth
  appointment gets a visit workspace (portal + staff variants):
  provider name + join URL rendered from the PUBLISHED provider
  template in `integration/telehealth`, the disclaimer rendered
  verbatim — **OpenCare does not provide video**; the join link is
  the organisation's configured provider, stated in the API, the
  UI and this report. The pre-visit intake is the published
  `telehealth-intake` form, validated by the ordinary form
  validator; the workspace payload carries the published form
  definition so the portal renders it through FormRenderer without
  Studio read access. Non-telehealth appointment → 422; another
  patient's appointment → 404.
- **Self-scheduling**: `POST /portal/appointments/request` creates
  a real Appointment from the PUBLISHED slot types (Phase 7's
  catalogue — scheduling is not forked): `proposed` by default
  (staff confirm through the ordinary appointment flow), or
  `booked` when the published `integration/self-scheduling`
  config's `autoConfirm` is true — the flip is a config publish,
  test-proven with no code change. Unknown slot type → 422 naming
  the configured set. Patients list only their own appointments.

### Part C — quality measure engine (`backend/app/quality.py`, new)

Measures are published Studio config (new kind `measure`):
{id, title, denominator, numerator} where each side is a criteria
object in the Phase 3 cohort-criteria style — `conditionCodes` /
`procedureCodes` as {system, code} objects, `lab` / `labs` rules
(LOINC + comparator + threshold + optional windowDays), `ageRange`.
The engine REUSES the pophealth evaluator (`_members`,
`_latest_measurements`, `_compare`, `_cell`, `_reference_date`) —
no second evaluator was written. The numerator is evaluated inside
the denominator. Small-cell suppression uses pophealth's `_cell`
with the measure's own `smallCellThreshold` (default 5), and the
rate is reported ONLY when both counts are visible — a rate over a
known denominator would leak a suppressed numerator. The three
seeded measures (published v1 at startup through governance):
`hba1c-control`, `bp-control` (two-lab AND rule),
`colorectal-screening` (age-band denominator). Exact-math tests on
a purpose-built synthetic warehouse population: HbA1c 6/10 = 60.0
(includes the sharp edges: exactly 8.0 does not count, a 19-month
stale controlled value does not count), BP 5/6 = 83.3, colorectal
5/8 = 62.5 (out-of-band persons with colonoscopies leak into
neither side). Measure config validation rejects empty criteria,
lab rules without a code, and unknown comparators at draft time.
Web: a Quality section on the PopHealth page — measure cards with
the rate, or an explicit small-cell-suppressed state (no rate,
with the reason stated).

### Part D — research cohort workbench (extends `backend/app/pophealth.py`)

Verified first: saved cohort definitions ALREADY exist — they are
the published Studio `cohort` configs behind `/pophealth/registries`.
Added on top: **snapshots** — `POST /pophealth/registries/{key}/
snapshots` (pophealth.run) records the current member count +
definition version (never membership) in a new `cohort_snapshots`
table; `GET …/snapshots` returns the time series (test: 10 → 11
across an ETL'd population change). **De-identified aggregate
export** — `GET /pophealth/registries/{key}/export` returns the
definition + the (already suppressed) aggregate summary + a
no-patient-level-rows notice, behind the new `pophealth.export`
capability (admin/configurator, the interop.export pattern) and
audited on every call (test asserts the audit entry and that no
patient id, `Patient/` reference or `person_id` appears anywhere
in the payload). Row-level export does not exist anywhere in this
surface, by design. Web: snapshot history + Take snapshot +
Export aggregates (with JSON download) on each registry card,
buttons capability-shaped from the published permission matrix.

## §6 configuration-coverage list

Everything a site/specialty changes WITHOUT code, per surface:

1. **The 11 specialty packs themselves** — order sets, forms,
   flowsheets, terminology subsets, registry cohorts, module
   lists: governed pack configuration (draft → approve → attach →
   enable). A site enables the specialties it runs.
2. **Ophthalmology order-type catalogue** (`terminology/order-types`
   superset adding `eye-procedure` with requiredFields
   [code, bodySite]) — laterality enforcement is catalogue data.
3. **Psychiatry formulary + monitoring rules**
   (`terminology/formulary` superset; `terminology/medication-
   monitoring`: per-medication block/flag action, required labs
   with min/max value and max age in days) — thresholds are data;
   a site can gate any formulary medication the same way.
4. **Order sets** (`orderset` kind) — 22 published sets after all
   packs enable (2 seeded + 20 pack); cycle/day structure is
   order-set item data.
5. **Forms** (`form` kind) — every specialty intake/assessment/
   flowsheet + telehealth intake + telehealth note template.
6. **Quality measures** (`measure` kind, NEW) — denominator/
   numerator criteria, smallCellThreshold per measure.
7. **Telehealth** (`integration/telehealth`) — provider name,
   join URL template, intake form key, note type, disclaimer text.
8. **Self-scheduling** (`integration/self-scheduling`) —
   autoConfirm flag.
9. **Slot types + note types** (`terminology/slot-types`,
   `terminology/note-types`) — telehealth entries appended as new
   governed versions of the published payloads.
10. **Cohort definitions** (`cohort` kind) — the registries the
    snapshots/export operate on (unchanged Phase 3 surface).
11. **Capabilities** (`permissions/role-matrix`) — three new
    capabilities with seeded defaults: `messages.use`
    (admin/configurator/clinician), `portal.self-schedule`
    (admin/clinician/patient), `pophealth.export`
    (admin/configurator). The web capability mirror
    (`web/src/pages/studio/models.ts`) gained the same rows — and
    Phase 8's three `interop.*` rows, which the mirror was missing
    (pre-existing gap, closed here because the mirror's contract
    is "mirrors configstore exactly").

## Mechanism changes to existing behaviour (with justification)

1. **`fhir.py`: `Communication` added to SUPPORTED + validation.**
   Secure messaging needs the resource type; validation requires
   status + subject.reference + ≥1 payload contentString. No
   existing resource behaviour changed.
2. **`configstore.py`: new config kind `measure` + validator.**
   Measures must be governed config (§6); the kind registry is the
   extension point every prior phase used.
3. **`configstore.py`: three new capabilities** (above) with
   defaults that change nothing for existing roles except granting
   the new surfaces.
4. **`orders.py`: `bodySite` passthrough in order creation.** A
   standard FHIR order field, copied verbatim from the placement
   payload / order-set item; presence is enforced per order type
   by the catalogue's existing requiredFields mechanism. No other
   order behaviour changed (all prior order tests unmodified and
   green).
5. **`pharmacy.py`: medication-monitoring gate in `dispense`.** A
   generic evaluator over published rules, inserted after quantity
   validation and before stock movement; completely inert when no
   `terminology/medication-monitoring` config is published
   (test-proven against the seeded formulary). Justification: the
   brief asked for the QC-gate PATTERN as config; the lab QC gate
   itself evaluates lab runs, not dispensing, so a small generic
   hook was genuinely required — it evaluates config, it contains
   no psychiatry logic.
6. **`models.py`: new `cohort_snapshots` table** (count + version
   + actor + timestamp only — never membership).
7. **`pophealth.py`: snapshots + export endpoints** — additive;
   registry/summary/explorer behaviour unchanged.
8. **Web test adaptations (not weakenings):** the Studio
   permissions-model test's capability count went 30 → 36 (the
   mirror grew by the 6 rows above; its comment now enumerates
   them); two PopHealthPage assertions were re-anchored because
   snapshot rows and the measure badge added legitimate new
   "members"/"suppressed" text to the page (snapshot rows now say
   "in cohort"; the measure badge says "small-cell suppressed").

## Design notes (recorded, not hidden)

- **Diabetes pack vs Phase 3's seeded `diabetes` registry**: the
  pack's cohort key is `diabetes-endo-registry`, deliberately
  distinct — the seeded registry (with its HbA1c lab rule) is
  Phase 3's and was not silently replaced by a pack.
- **ANC code 751-8 is not in the Phase 2 lab catalogue**: the
  monitoring hook reads released chart Observations from ANY
  source (a site whose LIS reports ANC under 751-8 lights the
  gate up with no code change); Observations tagged lab-unreleased
  are explicitly excluded.
- **Lab-rule `windowDays`** in measure criteria is measured back
  from the warehouse reference date (the latest measurement date),
  consistent with how the Phase 3 registry lab rule behaves.
- **Read receipts are explicit**: replying to a thread does not
  mark the other side's messages read; the read endpoint does.
  This is stated in the API and exercised by tests.
- **Messages carry no PHI beyond the chart**: threads hang off the
  patient record and inherit its scoping; there is no
  patient-to-patient messaging.

## NOT RUN / not claimed

- **Real video provider** — none integrated, by design: the join
  link is the organisation's configured provider URL (data). No
  video stack was built or faked.
- **Browser click-through E2E** — NOT RUN (standing gap; web is
  covered by component/client tests only).
- **Postgres runtime** — NOT RUN (suites run on SQLite; the
  snapshot table is plain SQLAlchemy like every other table).
- **CGM/pump and other device-data ingest into flowsheets** — not
  built (roadmap §2 device ingest remains deferred from Phase 8).
- **Portal submission of PROM/questionnaire packs beyond the
  telehealth intake** — the portal renders the telehealth intake
  via FormRenderer; a general portal questionnaire inbox was not
  built.
- **Trial screening / research recruitment workflows** — out of
  scope; the workbench is definitions + snapshots + aggregate
  export only. Row-level export was refused in-scope by design.
