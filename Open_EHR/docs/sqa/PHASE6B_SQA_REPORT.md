# Phase 6B SQA Report — Full Clinical Laboratory Completion

**Date:** 2026-10-17 · **Scope:** roadmap §7 (clinical laboratory
completion) under the §6 configure-don't-fork rule, built ON the
Phase 2 order → specimen → result → verify → release spine
(`backend/app/lab.py`, extended — not rewritten) plus one new module
(`backend/app/lab_benches.py`) for the bench surfaces.
**Data:** synthetic only. **State:** built + tested in staging
`~/workspace/Open_EHR` only — NOT published to GitHub, NOT synced to
any Mac, per instruction.

## Gate results (all re-run at delivery)

| Gate | Baseline (Phase 6) | Phase 6B |
| --- | --- | --- |
| Backend pytest | 227 passed | **249 passed** (22 new in `tests/test_phase6b_lab.py`) |
| Web vitest | 162 passed (24 files) | **175 passed** (26 files; +6 page tests, +7 client-shaping tests) |
| `npm run build` | clean | **clean** |

No pre-existing test was weakened, skipped or deleted. Every
pre-existing suite passes unchanged against the extended code.

## What landed, in §7 order

### 1. Pre-analytical
- **Collection worklist** — `GET /lab/collection-worklist` (lab.read):
  orders in `ordered` state grouped by patient + location (encounter
  reference), each group's orders sorted by collection priority from
  the published catalog's `collection.priorityOrder`
  (stat → asap → urgent → routine; default priority also config).
- **Chain of custody** — collected / received stamps already existed
  on the specimen; Phase 6B adds the **in-analysis stamp**
  (`urn:opencare:extension/in-analysis-at`, set on the `in-process`
  transition) and exposes the full chain on the order detail as
  `custody` {ordered_at, collected_at, received_at, in_analysis_at}
  (missing steps are null, never invented).
- **Specimen rejection + recollection** — rejection criteria are
  published terminology in the catalog (`rejectionCriteria`:
  hemolysed, clotted, insufficient, mislabelled, wrong-container,
  delayed-transport, …). The reject transition accepts an optional
  `reason_code` validated against that list (422 when unknown); every
  rejection appends a structured entry to the order's
  `rejectionHistory` (criterion, reason, timestamp, actor, specimen
  id/accession) and spawns a **recollect Task** for the patient. The
  rejected specimen record is never deleted. `POST
  /lab/orders/{id}/recollect` (lab.work) returns the order to
  `ordered`, clears the specimen link and completes the open
  recollect tasks; the next collection creates a fresh specimen.

### 2. Bench catalogues
- The published lab catalog (seeded v1 payload, extended in place per
  the defaults pattern) gains the **coagulation** bench (PT 5902-2,
  INR 6301-6, aPTT 3173-2 + `COAG-PANEL`) and the **urinalysis** bench
  (`24356-8` panel: dipstick components + pH + specific gravity), all
  LOINC-coded with ranges. Concepts carry a `bench` field that routes
  orders (chemistry default, coagulation, haematology, microbiology,
  blood-bank, pathology).
- **Qualitative results**: concepts flagged `qualitative: true` in
  config carry a published `values` list; result validation accepts
  exactly those coded text values (stored as `Observation.valueString`)
  and rejects anything else 422. The numeric path is byte-for-byte the
  previous validation — a string for a numeric concept is still 422,
  a number for a qualitative concept is 422.

### 3. Microbiology
- Culture concepts (blood culture 87040-1, urine culture 87086-6,
  wound culture 87070-0) run their **own published state machine**
  (`workflow/lab-micro-flow`: ordered → collected → plated →
  preliminary-reported → organism-identified →
  susceptibility-reported → final) through the same transition
  endpoint and the same machine machinery — no fork.
- Organisms and antibiotics are published terminology
  (`terminology/micro-organisms`, `terminology/micro-antibiotics`),
  validated on every bench write (422 for unlisted codes).
- Staged reporting (`backend/app/lab_benches.py`): preliminary
  finding → organism identification → susceptibility results as
  structured Observations with **S/I/R** interpretations → finalize
  (lab.verify). All staged content lives on one DiagnosticReport
  carrying the Phase 2 `lab-unreleased` visibility tag; preliminary
  culture results are invisible to the portal, the inbox and
  patient by-id reads until finalize strips the tag — the Phase 2
  invariant, extended to a new bench. A no-growth culture finalises
  without susceptibilities.

### 4. Blood bank / transfusion
- Catalogue: type & screen panel `BB-TS` (ABO/Rh 883-9 qualitative
  over the published blood-group list, antibody screen 890-4) and
  crossmatch `BB-XM` — resulted on the standard spine.
- **Unit inventory** (own table `blood_units` — inventory is not a
  FHIR resource): unit id (unique), product type validated against
  the catalog's `bloodProducts`, blood group, ISO expiry. Issue is
  refused **409 for an expired unit** (and marks it expired),
  refused 409 when not available or when the patient is under a
  reaction hold, and 422 against a non-blood-bank order.
- **Transfusion record**: `POST /lab/blood-bank/transfusions` stores
  a FHIR Procedure linking unit → patient (unit + group as
  extensions), the blood-bank analogue of the MAR record; the unit
  moves to `transfused`. Unused issued units return to `available`.
- **Reaction workup**: reporting a reaction quarantines the unit,
  annotates the transfusion Procedure, and places a **hold**
  (`blood_bank_holds`) that blocks all further issues to that patient
  until a clinician clears it (`POST /lab/blood-bank/holds/{patient_id}/clear`,
  lab.verify).

### 5. Anatomic pathology (lite but real)
- Published workflow `workflow/lab-pathology-flow` (ordered →
  collected → accessioned → grossed → histology → reported →
  signed-out) on the same spine; concept 11529-5 (surgical pathology).
- The **synoptic report is a published form**
  (`form/pathology-synoptic`: specimen type, tumour site, histologic
  type, margin status required) stored through the Phase 5
  documentation machinery as a real QuestionnaireResponse (required
  items enforced there) and attached to a preliminary, tagged
  DiagnosticReport. **Sign-out** (`POST /lab/pathology/reports/{id}/sign`,
  lab.verify) is the release-equivalent: unsigned reports are
  invisible to portal/inbox/patient reads; signed reports are final
  and visible.

### 6. Post-analytical
- **Auto-verification** — config: catalog `autoVerify.enabled`
  (seeded **false** — behaviour unchanged until a site publishes it
  on) + per-analyte `autoVerify {low, high, delta}`. A draft result
  set auto-verifies only when every result is inside its auto-verify
  range, inside its delta bound (when configured), non-critical, and
  no result is QC-blocked. Auto-verify performs the verify step
  only — **it never releases** (the report stays tagged and
  portal-invisible), and it is audited with actor
  `system:auto-verify`, action `lab.report.verify`, the matched rule
  ids in the payload.
- **Delta checks** — per-analyte `delta {pct|absolute}` config;
  compared against the patient's previous RELEASED value at result
  creation; out-of-threshold results surface as `delta_flags` on the
  verification queue item. Flags are informational and block
  nothing (test-proven: a flagged report verifies + releases
  normally).
- **Reflex rules** — catalog `reflexRules` (seeded
  `tsh-low-add-ft4`: TSH below its low bound → add free-T4 draft
  order). Firing creates a normal draft ServiceRequest in `ordered`
  state carrying `urn:opencare:extension/reflex` = rule id and a
  source-report link; deduplicated per (source report, rule).
- **Amended reports** — `POST /lab/reports/{id}/amend` (lab.verify):
  reason is mandatory (422 without). Creates a NEW DiagnosticReport
  with FHIR status `amended`, the corrected results as new final
  Observations (uncorrected results referenced unchanged from the
  original), an `amends-report` link + the reason; the original is
  preserved untouched except a forward `amended-by-report` link.
  Both versions stay visible in the inbox and the portal — the
  portal shows the current version with status `amended`, honestly.
  Amending an unreleased report → 409; amending a superseded
  version → 409.

### 7. Laboratory operations
- **QC programme** — `LabQcRun` extended with `level`, `flags`,
  `z_score`. Materials + levels (mean/SD per analyte+material+level)
  and the Westgard-style rule toggles live in the catalog's
  `qc.programme` config; flags (`1-2s` warning, `1-3s` / `2-2s`
  rejection) are computed at run creation against the programme
  mean/SD and the previous same-series run, and stored on the run.
  `passed` keeps its original range meaning — the existing
  latest-failed QC gate semantics are unchanged.
  `GET /lab/qc/levey-jennings` returns the run series for one
  analyte+material with the series mean/SD and ±2SD/±3SD limits
  computed server-side.
- **EQA / proficiency** — `lab_eqa_records` table + CRUD API;
  `passed` is computed server-side from the acceptable range;
  analyte validated against the published catalog.
- **Instrument registry** — `lab_instruments` +
  `lab_instrument_logs` tables + API (id, name, bench, status
  active/maintenance/down; maintenance/calibration log entries).
  Result ingest may name `instrument_id`: unknown instruments are
  refused 422; a registered one is recorded as report metadata
  (extension) only.
- **TAT dashboard** — `GET /lab/tat`: per-bench and per-analyte
  order→release and received→release p50/p90 (hours), computed by
  **live query** over the FHIR store's custody timestamps
  (authoredOn / receivedDateTime / issued) for released orders —
  deliberately NOT the warehouse (no ETL lag, no new dependency);
  the response carries `source: "live-query"` to say so.

### Web (LabPage)
Existing sections unchanged in behaviour; additions:
- **Collection worklist** table (grouped payload, collect action).
- **Microbiology** section — culture orders worked through their
  staged bench flow (collect/plate via transitions, then
  preliminary/organism/susceptibility/finalize controls, terminology
  from the API).
- **Blood bank** section — unit registration + inventory table,
  issue against a blood-bank order, transfusion record, return,
  reaction reporting, and an active-hold banner with clinician
  clear. Type & screen results enter through Result entry
  (qualitative selects).
- **Anatomic pathology** section — case progression buttons, the
  synoptic form rendered with the shared FormRenderer against the
  published form, sign-out.
- **Laboratory operations** section — TAT table, Levey-Jennings
  series loader + table, EQA form + records, instrument registry
  with status + calibration-log actions.
- Verification queue: delta-flag badges, AMENDED badge with reason,
  amend control (released items, reason mandatory in the UI too);
  qualitative results render their text values.
- Order queue: coded rejection-criterion select (from the published
  catalog), recollection button for rejected orders, reflex badge on
  reflex-created orders. Bench orders (micro/pathology) are worked in
  their bench sections, not the standard queue/result entry.
- QC table: level + Westgard flags + z-score columns; QC form gains
  an optional level field.
- New client surface is defensive in the established page pattern
  (`typeof client.x === "function"` guards, as CriticalSection
  already did) so partial stub clients in older tests keep rendering.

### Pack governance
`backend/packs/lab.pack.json` bumped **2.0.0 → 2.1.0**: the 28 new
endpoints, 5 new modules (collection, micro, blood bank, pathology,
operations) and the synoptic form are declared — all
`enabled: false`, following the pack lifecycle (install = draft;
nothing self-enables). New capabilities: **none** — benches reuse
`lab.read` / `lab.work` / `lab.verify` / `lab.order` (the realignment
pattern: behaviour is config + existing capabilities, not new
permission surface).

## §6 configuration-coverage list

Everything a site would change to make the laboratory *theirs* is
Studio configuration data (published, versioned, governed):

| Behaviour | Configuration |
| --- | --- |
| Test/panel catalogue, ranges, units, benches | `terminology/lab-catalog` concepts (`bench` per concept) |
| Qualitative analytes + allowed values | concept `qualitative` + `values` |
| Rejection criteria | catalog `rejectionCriteria` |
| Collection priority order + default | catalog `collection` |
| Standard / POCT workflows | `workflow/lab-order-flow`, `workflow/lab-order-flow-poct` (unchanged) |
| Microbiology workflow states/governance | `workflow/lab-micro-flow` |
| Pathology workflow states/governan
...[truncated 5351 chars]