# Phase 6 — Inpatient Operations — SQA Report (2026-10-17)

Scope per `docs/FULL_SYSTEM_ROADMAP.md` §3 Phase 6 + §6 (the standing
rule: configure, don't fork — every phase SQA from Phase 5 on includes
a configuration-coverage list). All data synthetic. Baselines held:
**backend 212 → 227 tests, web 150 → 162 tests, `npm run build`
clean** — no existing behaviour weakened (the mechanism-test changes
are listed in §4 with justifications).

## 1. Results

| Gate | Result |
|---|---|
| Backend suite (`pytest tests -q`) | **227 passed** (212 baseline + 15 new Phase 6) |
| Web suite (`npx vitest run`) | **162 passed / 24 files** (150 baseline + 12 new) |
| Web build (`npm run build`, incl. tsc) | **clean** |
| ADT chain smoke (TestClient, fresh DB) | **PASS** — admit → 409 on occupied → transfer (old bed → cleaning; census 18 beds / 16 available / 1 occupied / 1 cleaning / 5.6%) → discharge → event log A01→A02→A03 with config-driven labels; patient role 403 on census |
| Patient-scoping invariants | untouched — bed board is staff-only by capability (`inpatient.read`/`inpatient.work` have no patient role); full pre-existing suite green |
| Publish / sync | **NOT DONE by design** — staging only; no GitHub push, no Mac sync (per task instruction) |

## 2. What was built

### 2.1 Locations & beds as data (`backend/app/inpatient.py`, new)

- The location hierarchy is **published Studio terminology config**
  (`terminology/locations`): facility → unit → bed concepts in payload
  order, each bed carrying `room`/`bed` labels, plus the `bedStatuses`
  value list. Seeded v1 = a synthetic 2-unit hospital: Medical-Surgical
  (12 beds, rooms 101–106 × A/B) + ICU (6 beds, rooms 201–206).
- FHIR **Location resources are materialised from the config**
  (facility/unit/bed rows with `partOf` chains and FHIR physical-type
  codings; `"Location"` added to the generic surface's SUPPORTED set —
  it previously 400'd). Materialisation is idempotent and
  create-only: live bed state is never overwritten by a restart or a
  config read. Beds declared by a later config publish appear on
  first read — **proven by test**: publishing a v2 with a new
  Step-Down unit + 2 beds puts the unit on the census and the beds
  accept admissions with no code change.
- A bed's live status (`available`/`occupied`/`cleaning`/`blocked`)
  and occupant live on its Location resource (extensions). The ONLY
  mutation path is one helper that bumps the resource version and
  writes a hash-chained audit row (`inpatient.bed.status`) in the
  same transaction — **bed status changes are audited events, never
  silent edits**.

### 2.2 ADT event system

- New table `adt_events` (`app.models.AdtEvent`): event type,
  encounter, patient, from/to location, actor, detail, timestamp.
  Every movement writes one event row **and** one hash-chained
  AuditLog entry (`inpatient.adt.A01` etc.) in the same transaction.
  Event-type labels (A01 admit / A02 transfer / A03 discharge / A08
  update) are **terminology config** (`terminology/adt-event-types`),
  served with each event as `event_display`.
- `POST /inpatient/admissions` creates the inpatient Encounter
  (class IMP, in-progress, bed as its active location entry), occupies
  the bed, writes A01 — one act. The bed must be `available` (**409**
  naming the live status otherwise; unknown/inactive bed = 422). A
  patient with an active admission cannot be admitted twice (409,
  pointing at transfer/assign-bed).
- `POST /inpatient/encounters/{id}/assign-bed` completes an admission
  begun elsewhere — this is how the Phase 4B **ED board's admit**
  (which creates the IMP Encounter bed-less) gets its bed; A01.
- `POST .../transfer`: old bed → `cleaning`, location history entry
  completed + new active entry, new bed occupied, A02 with from→to.
- `POST .../discharge`: closes the Encounter **through the published
  `workflow/inpatient-flow` machine** loaded by the same shared
  loader (`configstore.load_workflow_machine`) that drives the
  fhir.py ADT machine — per-transition roles honoured, no forked
  machine. Period end stamped, location entry completed, disposition
  recorded, bed → `cleaning`, A03. Housekeeping flips
  `cleaning → available` via `POST /inpatient/beds/{code}/status`
  (also `available ↔ blocked`); occupancy is never set by a bare flip
  (422), and an occupied bed refuses flips (409).
- LOS and census figures are **computed** from stored state + clock,
  never stored (the Phase 5 MAR rule).

### 2.3 Census + bed board

- `GET /inpatient/census` — per-unit counts by live status +
  occupancy % + hospital totals + today's expected discharges;
  `GET /inpatient/beds` — patient-per-bed listing; `GET
  /inpatient/locations` — config hierarchy joined with live status;
  `GET /inpatient/adt-events` — the movement log (filterable).
  Census math is asserted exactly on a seeded synthetic ward
  (Med-Surg 12 beds: 3 occupied / 8 available / 1 blocked = 25.0%;
  ICU 6: 1 occupied = 16.7%; totals 18 / 4 / 22.2%).
- Web **Bed Board page** (nav module `beds`, capability-gated via
  `inpatient.read`, runtime-config aware like the other boards):
  units as sections, beds as cards (patient + LOS + status), admit
  form (patient + available-bed select), transfer select + discharge
  action per occupied bed, cleaning→available and block/unblock
  flips, expected-discharges-today strip. New route prefix
  `/inpatient` added to the Vite dev proxy and the nginx proxy regex.

### 2.4 Case management + discharge planning

- Care plan = FHIR **Tasks** linked to the admission Encounter
  (requested → in-progress → completed/cancelled; terminal states
  refuse further transitions 409), the Phase 5 Task patterns.
- Discharge plan lives on the Encounter (extension): target date,
  disposition, barriers, checklist linkage. Plan writes are **A08**
  ADT events. Dispositions are **terminology config**
  (`terminology/discharge-dispositions`: home, home-health, snf,
  rehab, ltach, hospice), served at
  `GET /inpatient/discharge-dispositions` and validated on every
  write (unknown/inactive = 422); discharge falls back to the plan's
  disposition when the caller omits one.
- The **discharge checklist is a published Studio form**
  (`form/discharge-checklist`) rendered by the shared FormRenderer
  (bed board discharge panel + chart Admission tab): submitting it
  stores a real QuestionnaireResponse (validated against the
  published form by the generic surface), links it into the plan,
  and (on the board) discharges.
- `GET /inpatient/expected-discharges?date=` (default today, UTC)
  powers the census strip.
- Discharge summary was already a Phase 5 note type
  (`discharge` → `form/note-discharge`); Phase 6 verified it in the
  catalogue and added the two perioperative types (§2.5).

### 2.5 Anaesthesia + perioperative depth (on Phase 4B surgery)

- **Case timeline**: surgery cases now carry a server-timestamped
  timeline (extension on the case Procedure), stamped at scheduling
  and at every workflow transition in `app.boards` — scheduled →
  pre-op → in-op → pacu → done, exposed on the board entry and the
  case record. The workflow itself was NOT extended: the published
  `workflow/surgery-flow` already carries exactly these states, so
  no new config version was needed (recorded here per the task's
  "publish a new version rather than hardcode" rule — there was
  nothing to add).
- **Pre-op checklist** (`form/preop-checklist`) and **anaesthesia
  record** (`form/anaesthesia-record`: type, airway, agents, fluids,
  EBL, urine output, complications) are published forms; submissions
  are validated + stored by the Phase 5 documentation machinery
  (`documentation._store_response`) and linked from the case
  (extension: response id + form version + author + summary).
- **Anaesthesia vitals** (`form/anaesthesia-vitals`) are a flowsheet
  on the same machinery: one QuestionnaireResponse per reading,
  server-timestamped, tagged with the case id; the case record
  returns the series in the Phase 5 flowsheet view shape.
- **Note types**: `preop` and `pacu` were appended to the published
  `terminology/note-types` as a **new governed version at startup**
  (v1 concepts preserved verbatim, hash-audited as config
  create+publish), with template forms `form/note-preop` /
  `form/note-pacu`. PACU note create → sign → final is test-proven.
- `GET /inpatient/surgery-cases/{id}/record` returns the whole
  perioperative record; the Surgery board page renders the timeline
  on each card and a **Case record** panel (pre-op status, anaesthesia
  summary, vitals series, and the three forms via FormRenderer).
- Chart gains an **Admission tab**: current admission (bed, LOS),
  discharge-plan editor (dispositions from config), discharge
  checklist form, care-plan list with transitions + add, ADT history.

## 3. New tests

Backend (`tests/test_phase6_inpatient.py`, 15 tests):
locations materialised from config (21 Location resources, partOf
chains); **new unit via config publish appears + admits**; admit to
occupied bed 409 (+ blocked 409, unknown bed 422, double-admit 409);
transfer frees old bed to cleaning; **event sequence A01→A02→A03
asserted from the log** with from/to + config displays; discharge
closes encounter (status finished, period end, location completed)
+ bed released; double-discharge 409; bed-flip rules (occupied 409,
bare-occupied 422, bogus 422, cleaning→available, block/unblock);
bed changes audited (`inpatient.bed.status` + `inpatient.adt.A01`
in `/studio/audit`); **exact census math**; discharge checklist form
submit via the FHIR surface + plan + expected-discharge listing
(explicit date and default-today) + census embedding + bad
disposition 422 + discharge disposition fallback; care-plan lifecycle
(409s on illegal transitions); dispositions served from config; ED
admit → assign-bed (409 on second assignment); anaesthesia flowsheet
attach (pre-op + record + 2 vitals readings + timeline order
scheduled→pre-op→in-op→pacu); PACU note type signs to final; patient
role 403 on every inpatient read/write, viewer read-only.

Web (+12 tests): `BedBoardPage.test.tsx` (8 — unit sections + census
totals + expected discharges, housekeeping flip, admit, 409 surfacing,
transfer, checklist submit → plan + discharge, viewer read-only);
`SurgeryBoardPage.test.tsx` (+2 — timeline renders; case-record panel
shows pre-op + vitals and submits the anaesthesia form);
`chart/ChartTabs.test.tsx` (+2 — admission tab renders bed/plan/care
plan/ADT and transitions an item; viewer sees no work actions).

## 4. Existing-test changes (mechanism, not behaviour)

- `backend/tests/test_realignment.py` — two assertions updated: the
  seeded navigation module list gains `beds` (12 → 13 items) and the
  derived count in the nav-removal test goes 11 → 12. Justification:
  these pin the seed snapshot; Phase 6 adds one module through the
  same seeded-config path, exactly as earlier phases extended it.
  The assertions' intent (seed == published v1; a published removal
  is honoured) is unchanged.
- `web/src/auth/session.test.ts` — per-role nav arrays gain `beds`
  for staff roles; two `canAccess` assertions added (viewer true,
  patient false). Same justification: the nav snapshot mechanism.
- `web/src/auth/runtimeConfig.test.tsx` — the SEED_NAV fixture (a
  literal copy of the backend seed) gains the `beds` item with
  renumbered orders; the canAccessWith page list gains `beds`.
- `web/src/pages/studio/models.test.ts` — capability count 24 → 26.
  Justification: Phase 6 adds two capabilities (`inpatient.read`,
  `inpatient.work`) through the realignment pattern — defaults added
  to `configstore.CAPABILITY_DEFAULTS` (server) and the web mirror in
  the same change; the test counts the mirror's keys.
- No other existing tests were modified; no existing behaviour was
  weakened. Two reconciliations worth naming, both covered by new
  tests: (a) the generic FHIR surface now accepts `Location`
  (previously an unsupported-type 400) — additive only; (b) surgery
  case entries gain a `timeline` field and cases gain a timeline
  extension — additive only, Phase 4B suites green unchanged.

## 5. Configuration-coverage list (roadmap §6)

Made configurable in this phase (all Studio config: versioned,
draft→publish, role-gated, hash-audited):

| Behaviour | Config | Notes |
|---|---|---|
| Location hierarchy (facility/units/rooms/beds, labels, parents) | `terminology/locations` | materialised to FHIR Locations; new units/beds are data (test-proven) |
| Bed status value list | `terminology/locations` → `bedStatuses` | engine validates flips against it |
| ADT event-type labels (A01/A02/A03/A08) | `terminology/adt-event-types` | served per event as `event_display` |
| Inpatient Encounter lifecycle + per-transition roles | `workflow/inpatient-flow` | discharge runs through it via the shared machine loader |
| Discharge dispositions | `terminology/discharge-dispositions` | validated on plan + discharge writes |
| Discharge checklist (fields, required, choices) | `form/discharge-checklist` | rendered by the shared FormRenderer |
| Pre-op checklist | `form/preop-checklist` | attached to the surgery case |
| Anaesthesia record (agents, airway, fluids…) | `form/anaesthesia-record` | stored via Phase 5 machinery |
| Anaesthesia vitals flowsheet fields | `form/anaesthesia-vitals` | one QR per reading, case-scoped series |
| Note types: pre-op + PACU (+ templates) | `terminology/note-types` (new published version) + `form/note-preop`, `form/note-pacu` | appended at startup only if absent; site edits preserved |
| Bed board nav membership + labels + roles | `navigation/main` | seeded default gains `beds` (see known behaviour below) |
| Who may read / work the bed board | `permissions/role-matrix` → `inpatient.read`, `inpatient.work` | new capabilities via the realignment pattern |

Left coded, with justification:

- **The ADT invariants themselves** (admit needs an available bed;
  transfer/discharge release to `cleaning`; occupancy only via
  admission paths; double-admit refused): patient-safety /
  data-integrity invariants of the engine, the same class as the lab
  visibility tag — sites configure the *data* (which beds exist,
  what they're called), never whether a bed can be double-booked.
- **Census / occupancy / LOS arithmetic**: pure computation over
  config-supplied structure + stored timestamps.
- **The ADT event log shape + audit coupling** (one event row + one
  hash-chained audit entry per movement, same transaction): a
  governance invariant; the *labels* are config.
- **FHIR shapes** (Location hierarchy via `partOf`, Encounter
  location history, Task care plan): the interoperability contract.

Known behaviour (honest): config seeding is idempotent, so an
**existing deployment's already-published `navigation/main` v1 does
not gain `beds` automatically** — the Bed board appears in the shell
when the site republishes navigation including it (Studio →
Navigation), which is the realignment governance working as
designed. Fresh installs get it in the seed.

## 6. NOT RUN (honest list)

- **Live browser E2E** — the web is proven by the vitest suite (162)
  and a clean production build; no live browser click-through of the
  bed board was run in this environment (Min's rendered check on his
  machine remains the live gate, as in prior phases).
- **Postgres runtime** — suites run on SQLite via the portable
  repository layer, as in every prior phase; the Postgres/Docker
  path is still unverified anywhere. The new `adt_events` table is
  plain SQLAlchemy (portable types only).
- **Real device feeds** — no bed-management / RTLS feed and no
  anaesthesia-machine data capture: vitals are manual form entry
  against the published form; device integration is interface work
  for a later phase, not faked here.
- **Outbound HL7 v2 ADT messaging (MLLP)** — the event log carries
  the HL7 event-type labels as data, but no ADT messages are emitted
  to an interface engine; live HL7 v2 is roadmap Phase 8.
- **Housekeeping as a distinct role** — cleaning→available rides the
  `inpatient.work` capability in v1 (the task's clinician/admin/
  configurator default); a dedicated housekeeping role is a
  permissions-config + role change a site can govern later.
- Pack enablement still does not gate module routes (the standing
  known gap from the realignment report) — unchanged by this phase.

## 7. Files

Backend: `app/inpatient.py` (new), `app/models.py` (+ `AdtEvent`
table), `app/fhir.py` (`Location` on the generic surface),
`app/boards.py` (case timeline stamping + `case_timeline` /
`_is_surgery_case` helpers), `app/configstore.py` (2 capabilities,
`beds` nav seed), `app/main.py` (router, startup seeding, health
phase "6"), `tests/test_phase6_inpatient.py` (new, 15 tests),
`tests/test_realignment.py` (2 mechanism assertions, §4).

Web: `src/pages/BedBoardPage.tsx` + test (new), `src/pages/chart/
AdmissionTab.tsx` (new), `src/pages/SurgeryBoardPage.tsx` (+ timeline
+ case-record panel) + test, `src/pages/ChartPage.tsx` (Admission
tab), `src/App.tsx` (beds route), `src/auth/session.ts` (PageKey /
capability / nav), `src/pages/studio/models.ts` (capability mirror),
`src/api/types.ts` + `src/api/client.ts` (inpatient surface +
generic create), `src/test/fixtures.ts` (census / admission /
anaesthesia fixtures), `vite.config.ts` + `nginx.conf` (`/inpatient`
proxy prefix), `src/auth/session.test.ts`,
`src/auth/runtimeConfig.test.tsx`,
`src/pages/studio/models.test.ts`, `src/pages/chart/ChartTabs.test.tsx`
(mechanism updates, §4).
