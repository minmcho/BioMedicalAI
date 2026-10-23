# Depth SQA Report — Organisation, Layout & Clinical Depth Pass

**Date:** 2026-10-18
**Scope:** The Organisation, Layout & Clinical Depth pass, answering the
user's 2026-10-18 directives (complete system, not a prototype; clinical
workflows at large-institution depth; beautiful, simple, easy to operate;
three-tier organisation). Three workstreams: (1) **Organisation &
layout, shell-wide** — grouped navigation with sub-menus as published
navigation config, a shared in-panel Tabs component, shared Stepper +
StatusBadge, (2) **Clinical journeys deepened** — perioperative safety
checklist, inpatient rounds checklist, discharge medication
reconciliation, with ED and clinic audited step-by-step and verified
complete, (3) **Laboratory completion** — virology bench, serology /
immunology + toxicology catalogue benches, cytology within pathology,
send-outs / reference laboratory, POCT programme on the Phase 14 device
registry, delta-check windows, stewardship duplicate alerts, an
auto-verify governance surface (still seeded OFF), bench worklists.
**Staging only** — not published to GitHub, not synced to any Mac.
**Synthetic data only.** The parked `agents/` draft and
`docs/AGENTS.md` were excluded throughout, per standing rule.

## Gate results (all re-run on the final tree, sequentially)

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **549 passed** (516 baseline + 33 new: 7 navigation + 9 journeys + 17 laboratory — full collection is 549, cross-checked) — deciding gate: two sequential half-suite runs on pruned /tmp, junitxml artefacts `~/workspace/gateDEPTH2_A.xml` (289 tests, 0 failures/errors/skipped) + `~/workspace/gateDEPTH2_B.xml` (260 tests, 0/0/0), each artefact's counts verified against its own console summary; the backend suite no longer fits this VM's 512 MB /tmp in one run (standing environment rule). See Incidents for why this gate carries the `2` suffix |
| ADK service `pytest` | **8 passed** (unchanged) |
| Web `npm test` (vitest) | **299 passed, 44 files** (290 baseline + 9 net new: 4 laboratory depth + surgery/continuum journey coverage) |
| Web `npm run build` | **clean** (exit 0; only the pre-existing chunk-size advisory) |
| `/tmp` pruned before every full-suite run (standing environment rule) | done |
| Suites run sequentially, never overlapped | done |

New tests: backend `tests/test_depth_nav.py` (7),
`tests/test_depth_journeys.py` (9), `tests/test_depth_lab.py` (17);
web `src/pages/LabPageDepth.test.tsx` (4) plus journey tests in
`ContinuumPages.test.tsx` / `SurgeryBoardPage.test.tsx` (+5 net).

Existing tests adjusted — each a legitimate growth/mechanism change,
each commented in code, no assertion weakened:

- `test_phase4b_boards.py::test_case_lifecycle_and_double_booking`,
  `test_phase6_inpatient.py::test_case_timeline_stamped_server_side`,
  `::test_preop_anaesthesia_forms_attach_to_case`: the surgical-safety
  gate is seeded ON (see §2), so each lifecycle walk now records the
  time-out / sign-out checklist sections where it transitions the case
  — the transitions themselves are asserted exactly as before.
- `test_phase14_devices.py`: the device-type set assertion gains the
  new `poct-analyser` type with a comment; device ROW counts (6) are
  unchanged.
- Web `Phase13Pages.test.tsx` (3 tests), `Phase14Pages.test.tsx`
  (4 tests), `FrontDoor.test.tsx` (3 tests): Workforce / Reports /
  Data Platform sections moved behind the shared Tabs, and accounts
  behind the Workforce Accounts tab, so the tests open the relevant
  tab before their original assertions (unchanged in substance).
- Web test doubles completed with the client methods the reworked pages
  now call on mount — `PharmacyPage.test.tsx` (verification queue and
  related reads), `BedBoardPage.test.tsx` (`listAccounts`). The doubles
  return honest empty/minimal fixtures; pre-existing assertions are
  unchanged.
- No session/runtimeConfig test changes were needed: the navigation
  fallback merging kept them green as-is.
- Web `LabPage.test.tsx`, `LabPage6b.test.tsx`,
  `LabPageCritical.test.tsx`: the laboratory page is tabbed and only the
  active panel renders, so section assertions now open their tab first
  (via the shared `clickTab` helper, commented as a Depth-pass change).
  Every pre-existing assertion is unchanged.

## Delivered scope

### 1. Organisation & layout (Workstream 1)

**Grouped navigation as published config.** The navigation config
validator now accepts an optional `group` (non-empty string) and
optional `sections` (list of `{label, section}` with section slugs
matching `^[a-z0-9][a-z0-9-]*$`) on each module entry. The governance
rule is unchanged and test-proven: entries are still modules only —
page/form keys anywhere (including inside `sections`) are rejected
400. The seed navigation now carries the institution grouping:
Patient Care (patients, scheduling, inbox, portal) · Clinical (ed,
surgery, radiology) · Laboratory (lab) · Pharmacy (pharmacy) ·
Operations (beds, command, er-ops, unit-board, clinic, aftercare,
safety, workforce) · Revenue (billing) · Data & Platform (pophealth,
reports, imports, devices, data-platform) · System Studio (studio).
Grouping is a presentation layer over modules: **existing deployments
gain grouping on republish of the navigation config** — exactly the
Phase 6 beds precedent; regrouping later is a republish, never code.
Seeded sections deep-link where a page supports them: laboratory
(orders, microbiology, virology, blood-bank, pathology, qc, sendouts,
poct) and pharmacy (verification, inventory, controlled, compounding,
bcma).

**Shell rendering.** The web shell renders group labels in order; the
active module expands its section sub-buttons; the shell holds the
selected `{page, section}` and passes `section` / `onSectionChange`
into pages that support sections (Laboratory, Pharmacy). The built-in
fallback navigation mirrors the published seed, so a fresh deployment
shows the same grouping before any publish.

**Shared components.** `Tabs` (renders ONLY the active panel;
`role=tablist`/`tab`, `aria-selected`), `Stepper` (steps with
Done/Current/Pending words), `StatusBadge` (tones ok/warn/bad/info/
neutral — text + colour, never colour alone). Tabs rolled out to:
Laboratory (11 tabs, see §3), Pharmacy, Workforce, Data Platform,
Reports; the Studio page's tab bar is grouped (Configure /
Intelligence / Trust) over its existing machinery. The Laboratory
result-entry panel carries a Stepper over the order lifecycle
(Ordered → Collected → Received → In process → Resulted → Verified →
Released).

### 2. Clinical journeys (Workstream 2)

**Perioperative — built (was the thinnest area).** A governed form
`surgical-safety-checklist` (WHO-style sign-in / time-out / sign-out)
served through the existing FormRenderer machinery.
`POST /surgery/cases/{case_id}/safety-checklist` validates that the
submitted section matches the answers' item prefixes; each section is
submittable once (409 on repeat). The surgery board payload gains
`safety_checklist {sign-in, time-out, sign-out}` booleans per case.
The gate is configuration: `safetyChecklistGate: true` in the
published `workflow/surgery-flow` payload — transition to in-op 409s
naming the missing time-out, transition to done 409s naming the missing
sign-out; republishing the gate off restores free transitions
(flip-proven both ways in tests). The board page shows per-section
Done/Pending state (text + colour) and renders the next section's form
inline; server 409s surface inline, never silently.

**Inpatient rounds — built.** Governed form `rounds-checklist`;
`POST /continuum/inpatient/rounds-checklist` (404 unknown encounter,
409 non-admitted). The rounds list gains `rounds_checklist_today` per
patient (date-only comparison against the stored submission), shown as
a chip on the unit board with the checklist form inline. I-PASS was
verified present from Phase 12 and is unchanged.

**Discharge medication reconciliation — built.** Governed form
`discharge-med-rec`; `POST /continuum/inpatient/discharge-med-rec`.
Discharge readiness gains a `medrec_done` element surfaced as the
missing element `medication-reconciliation`, gated by the continuum-ops
topology key `discharge.requireMedRec`, **seeded FALSE** (the Phase 12
precedent: a new requirement ships off and is proven both ways by
republishing the ops config). Teach-back was verified present from
Phase 12; the Aftercare page carries the med-rec form beside it.

**ED — audited, verified complete, nothing built.** Step-by-step
against the operating-model research: arrivals/triage (ed-flow, ESI
1–5) → medical screening exam + EMTALA transfer gate → activations →
disposition/board including boarding timers, breach flags, diversion →
bed-request assignment (real A01) → LWBS + results-callback queue. Two
by-design notes, not gaps: ED encounters live in the boards module and
continuum ER ops key off them; ESI is set at arrival (no re-triage
timestamp exists).

**Clinic — audited, verified complete, nothing built.** Templates /
overbooking caps → booking (front-desk-widened gate) → rooming → visit
(chart) → no-show flags / backfill → follow-up via the aftercare
48–72 h call queue and referral loop closure. Rooming is the only
appointment-state writer by design; the front-desk rooming 403 is a
tested boundary.

### 3. Laboratory completion (Workstream 3)

**Catalogue benches.** Virology bench in the catalogue: SARS-CoV-2
qualitative NAAT (LOINC 94500-6), local qualitative influenza A/B + RSV,
a local respiratory viral panel, HIV antigen/antibody + differentiation
locals, HIV-1 viral load (20447-9), HBV DNA (42595-9). Serology locals
(HBsAg, HCV antibody, RPR, HAV IgM). Toxicology locals (cocaine,
opiates, THC, amphetamines, benzodiazepines, ethanol numeric) + a local
urine drug screen panel. Local codes are flagged "local code" in
displays, per the BB-*/COAG-PANEL precedent. Send-out analytes carry
bench `sendout` (see below).

**Reflex rules.** Published rule `hiv-reactive-add-diff`: a Reactive
qualitative HIV screen auto-creates the differentiation order. The
reflex engine gains a `qualitative-match` branch; numeric reflex rules
are untouched.

**Delta checks with windows.** Delta configuration gains `windowHours`
per analyte (creatinine 72 h, haemoglobin 168 h seeded). The previous
released result is now compared by value AND timestamp; the breach flag
on the verification queue carries `previous_at` and `window_hours`.

**Cytology within pathology.** Governed form `pathology-cytology`;
catalogue analytes flagged `cytology: true` (LOINC 10524-7 + a local
non-gyn code) report through
`POST /pathology/orders/{id}/cytology-report` and share the existing
sign-out. Guards are symmetric: the synoptic endpoint 422s cytology
orders and the cytology endpoint 422s surgical orders. The Pathology
tab picks the form by the catalogue flag.

**Send-outs / reference laboratory.** Reference laboratories are
published config (terminology `sendout-labs`: ref-regional, TAT 120 h;
ref-specialty, TAT 240 h). Lifecycle on the order: awaiting dispatch →
`POST /lab/sendouts/dispatch` (collected orders only; one laboratory
per manifest — mixed-lab selection 422s; sets dispatched state on order
and specimen, manifest id) → `POST /lab/sendouts/{order_id}/
receive-result` (dispatched only; files the returned results as
preliminary drafts through the standard result path — they queue for
verification like every other result, never auto-released) → TAT per
laboratory (`GET /lab/sendouts/tat`: resulted count, in-flight count,
average hours, within-TAT %).

**POCT programme.** Built on the Phase 14 device registry — no second
registry. A `poct-analyser` device type joins the device config.
Programme shape is published config (terminology `poct-programme`:
device types, QC levels L1/L2, competency validity 12 months, expiring
window 30 days, both enforcement switches seeded **OFF**).
`GET /lab/poct/devices` (registry devices of programme types with
today's QC per level + current/due state), `POST/GET /lab/poct/qc`
(device 404, level/result 422), `POST/GET /lab/poct/competency`
(catalogue code 422, future assessment date 422, method required;
currency computed from the published validity window — expired records
are never edited back to life), `GET /lab/poct/overview` (enforcement
state label, devices due QC, expiring/expired competencies). Result
entry for POCT orders enforces the programme inside the existing
result-write path: operator competency 409, missing device key 422,
QC-not-current 409 — **both switches seeded OFF**, so today the
programme tracks and displays; a site turns the blocks on by
republishing the config (flip-proven in tests). Filed POCT results
carry the instrument identity as a report extension.

**Stewardship / utilisation.** Published stewardship config
(`duplicateCheckEnabled: true`, `duplicateWindowHours: 24`). Order
placement returns a `warnings` key only when a duplicate open order
exists inside the window — a warning, never a block.
`GET /lab/stewardship/duplicates` lists live duplicate groups.

**Auto-verify governance surface.** `GET /lab/governance` renders the
rules a laboratorian is accountable for, read from the published
catalogue: auto-verify enabled flag + unmistakable state label, the
per-analyte ranges, recent auto-verifications from the hash-chained
audit log, reflex rules, delta checks with windows, stewardship state,
and the published catalogue version. **Auto-verify remains seeded OFF**
— enabling is a site decision made in Studio; the Governance tab shows
the off state as data (badge + label), never as an absence. Editing
stays in Studio; the tab is the operational read.

**Bench worklists.** `GET /lab/orders` gains a `bench` query filter;
the tabbed page (Workstream 1 layout) gives each bench its own
worklist view. The amended/corrected-report flow is unchanged and
lives on the Verification tab with the queue it belongs to.

**Web.** New client methods + types for trends, governance,
stewardship duplicates, send-outs, cytology, and the POCT programme.
New Laboratory tabs: Virology (bench worklist + viral-load trend viewer
— released results only, log10 shown; honest empty state: "drafts never
appear in a trend"), Send-outs (per-laboratory dispatch with tracking
reference, returned-result filing with local numeric validation, TAT
table), POCT (enforcement banner with the tracking-only state as text,
device QC table, QC + competency forms), Governance (auto-verify
off-state banner, rules/reflex/delta tables, duplicate worklist). All
new forms carry `noValidate`; selects are populated from real endpoints
or disabled WITH the reason shown; no dead controls.

## Configuration-coverage list (standing rule: configure, don't fork)

Everything a site or specialty would reasonably vary in this pass is
published, versioned, governed configuration:

- **Navigation grouping + section deep-links** — navigation config
  (`group`, `sections` per module entry); regrouping = republish.
- **Surgical safety gate** — `workflow/surgery-flow`
  (`safetyChecklistGate`, seeded ON); the checklist itself is a
  governed form (`surgical-safety-checklist`).
- **Rounds checklist** — governed form (`rounds-checklist`).
- **Discharge med-rec requirement** — continuum-ops topology
  (`discharge.requireMedRec`, seeded OFF); the form is governed
  (`discharge-med-rec`).
- **Laboratory catalogue** — virology / serology / toxicology /
  cytology / send-out analytes are catalogue concepts (bench, flags,
  reflex rules, delta `windowHours`, auto-verify ranges) in the
  published laboratory catalogue; local codes flagged as local.
- **Reference laboratories + contracted TAT** — terminology
  `sendout-labs`.
- **POCT programme** — terminology `poct-programme` (device types, QC
  levels, competency validity + expiring window, both enforcement
  switches seeded OFF); POCT device types in the Phase 14 device
  config.
- **Stewardship** — duplicate-check switch + window in the published
  laboratory stewardship config.
- **Cytology reporting** — governed form (`pathology-cytology`) +
  catalogue `cytology` flag.

Code was written only for genuinely new capabilities: the qualitative
reflex branch, windowed delta comparison, the send-out / POCT /
governance / trends endpoints, and the shell's group/section
rendering. No site-specific behaviour is hardcoded anywhere in this
pass.

## Compliance-coverage list

- **Register unchanged at 27 entries; capabilities unchanged at 58.**
  No new route groups were introduced — every new endpoint rides the
  existing proxied `/lab`, `/continuum`, and `/surgery` groups (no
  vite/nginx proxy change needed), and the new endpoints reuse the
  existing `lab.read` / `lab.work` / `lab.verify`, continuum, and
  surgery capabilities.
- New laboratory state (POCT QC + competency records) lives in two new
  tables with the same audit discipline as the rest of the laboratory;
  governance reads recent auto-verifications from the existing
  hash-chained audit log.
- No requirement in the register genuinely maps to new scope in this
  pass, so no entry was added and no total-asserting test changed —
  adding entries to inflate the count would be the dishonest move.

## Quality bar (user standing rule: complete system, not a prototype)

- Every added workflow step is test-proven end-to-end at the API level
  (33 new backend tests across the three workstreams) and, for the
  laboratory, at the component level (4 new web tests + journey suites).
- Shared client + server validation: send-out returned values, POCT QC
  level/result, competency fields, and checklist sections validate on
  both sides with inline errors; nothing fails silently (server 409s
  render inline on the surgery board and laboratory tabs).
- Every authored form carries `noValidate`; every select reads a real
  endpoint or published config, or renders disabled WITH the reason
  (POCT device/QC-level selects, virology analyte select).
- No dead controls: the virology tab's jump button routes to the tab
  where result entry actually lives; governance is deliberately
  read-only and says where editing happens (Studio).

## Design standard (user steer: beautiful, simple, workflow-visible)

- One design language: the existing design system/CSS only; the shared
  Tabs/Stepper/StatusBadge components replace per-page inventions.
- Three-tier organisation delivered as specified: grouped menu →
  module sub-menu sections → in-panel tabs.
- Workflow state is visible as text + colour everywhere this pass
  touched: checklist Done/Pending per section, rounds/med-rec chips,
  POCT QC current/due, enforcement Tracking-only/Enforced, auto-verify
  ENABLED/DISABLED, order-lifecycle Stepper.
- One obvious primary action per panel; honest empty states carry the
  reason (e.g. POCT's "register a device on the Devices page — the
  programme reads that registry, it keeps no second one").

## NOT RUN / gaps (honest list)

- **Browser click-through E2E — NOT RUN** (standing VM limitation:
  loopback blocked; not retried this pass — the user's own Mac run
  remains the browser gate).
- Postgres/Docker runtime — NOT RUN (unchanged standing item).
- Live Keycloak/OIDC sign-in — NOT RUN (unchanged).
- Live LLM — NOT RUN (unchanged).
- Live TLS — NOT RUN (unchanged).
- Pack route gating — NOT RUN (unchanged).
- Mobile compile — NOT RUN (unchanged; source only).
- Drug-interaction checking remains deliberately INERT from the
  front-door pass (no openly-licensed source); untouched here.

## Incidents / notes from this build

- **First gate artefacts discarded and the gate re-run.** The first
  half-suite pair wrote junit artefacts (`gateDEPTH_A.xml` /
  `gateDEPTH_B.xml`) whose contents did not match their own console
  summaries, and one half carried 13 `sqlite3 disk I/O error` setup
  errors in `test_phase12_continuum` — the signature of suites
  contending for this VM's 512 MB /tmp. Those artefacts were treated
  as corrupt evidence, not results: the gate was re-run clean
  (sequential, /tmp pruned, nothing else running) as
  `gateDEPTH2_A.xml` / `gateDEPTH2_B.xml`, and the numbers in this
  report are the re-run's, with each artefact cross-checked against
  its console summary and against a full `--collect-only` count
  (549). The discarded pair remains on disk under its original names
  for inspection; it is not gate evidence.
- The terminology validator requires a non-empty `concepts` list on
  every terminology payload; both new terminology payloads
  (`sendout-labs`, `poct-programme`) carry one — caught by a failing
  test during the build, fixed in the seed, now covered by
  `test_depth_lab.py`.
- The /tmp discipline held for every run this report relies on:
  `pgrep pytest` confirmed 0 and `/tmp/pytest-of-root` pruned before
  each, backend run as two sequential halves (see the artefact
  incident above for the discarded first attempt).
- Workstream 2's audit conclusion (ED and clinic verified complete) is
  a finding, not a shortcut: both journeys were traced step-by-step
  against the operating-model research before anything was declared
  present.

---

## Parent verification addendum (2026-10-18)

Independently re-run by the parent agent after the coordinator's
handoff (including its supplementary reconciliation note); the
coordinator's numbers were NOT taken on trust.

- Artefacts checked first: `gateDEPTH2_A.xml` records 289 tests and
  `gateDEPTH2_B.xml` 260 (coordinator halves, 549 claimed); this
  report and the PHASES.md depth row present.
- Backend, parent run, same gate procedure (no pytest running at
  start, `/tmp/pytest-of-root` pruned, two sequential halves,
  alphabetical split): half A **306 passed** (exit 0), half B **243
  passed** (exit 0) — total **549 passed**, matching the
  coordinator's 549 exactly. The halves split differently (306+243 vs
  289+260); the total is the deciding figure, and it agrees.
- Incident, disclosed: the first parent run was killed mid-half-B by
  a runtime restart, which also wiped /tmp (including the split
  lists). Half A's result was already banked in its log
  (`pDEPTH_parent_A.log`: 306 passed, exit 0); half B was re-run
  cleanly from a regenerated split (`pDEPTH_parent_B.log`: 243
  passed, exit 0). No result was carried on assumption.
- Web, parent run: **299 passed** (44 files), `npm run build` clean.
- ADK service suite, parent run: **8 passed**.
- Every coordinator figure confirmed exactly; no count correction is
  needed for this pass.
- Standing NOT RUN list unchanged: real-browser click-through (VM
  loopback block — the Air sync that follows this verification is the
  browser gate), Postgres/Docker runtime, live Keycloak OIDC/MFA,
  live LLM run, live TLS serving, pack-enablement route gating,
  mobile compilation.

