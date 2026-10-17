# Phase 5 — Orders, Documentation & Profiles — SQA Report (2026-10-16)

Scope per `docs/FULL_SYSTEM_ROADMAP.md` §3 Phase 5 + §5 deployment
profiles + §6 (the standing rule: configure, don't fork — every phase
SQA from Phase 5 on includes a configuration-coverage list). All data
synthetic. Baselines held: **backend 165 → 212 tests, web 135 → 150
tests, `npm run build` clean** — no existing behaviour weakened.

## 1. Results

| Gate | Result |
|---|---|
| Backend suite (`pytest tests -q`) | **212 passed** (165 baseline + 47 new Phase 5) |
| Web suite (`npx vitest run`) | **150 passed / 23 files** (135 baseline + 15 new) |
| Web build (`npm run build`, incl. tsc) | **clean** |
| Solo-practice day scenario (roadmap §5 gate) | **PASS** — `tests/test_phase5_solo_day.py`, every step asserted through the public APIs |
| Patient scoping + lab visibility invariants | untouched — full pre-existing suite green, incl. inbox/portal unreleased-tag tests |

## 2. What was built

### 2.1 General orders / CPOE (`backend/app/orders.py`, new)

- Orders are FHIR resources over the existing store: **MedicationRequest**
  for medication orders, **ServiceRequest** for everything else, **Task**
  for consult fulfilment. Lab orders stay in `lab.py` (Phase 2) and are
  excluded from the general queue by the order-type marker extension.
- The **order-type catalogue is published Studio terminology**
  (`terminology/order-types`, seeded v1): medication, procedure,
  consult, imaging, nursing, diet, activity — each with its FHIR
  resource type, per-type `requiredFields` (consult → performer role,
  imaging → modality) and medication `timingDefaults`. Unknown type or
  a missing required field is a 422 naming the problem.
- The **lifecycle is the published workflow** `workflow/order-flow`
  (draft → signed → active → completed / discontinued / cancelled),
  enforced with the codebase's uniform machine order (unknown state
  422 → illegal transition 409 → restricted role 403). **Unsigned
  (draft) orders are invisible to the fulfilment queue** and to the
  consult inbox.
- **Order sets are a new Studio config kind `orderset`** (validator
  added to `configstore.py` following the existing kind/validator
  pattern), seeded: `admission-general`, `diabetes-admission`.
  Placement expands a set into individual orders, each individually
  signable/discontinuable; placement **validates every item first** —
  one bad item (e.g. an allergy conflict) creates nothing.
- **Decision support (honest, config-driven):**
  - *Allergy check* — a medication order whose code matches an ACTIVE
    `AllergyIntolerance` code for the patient is refused **422, naming
    the allergen**. It can be overridden only with
    `allergy_override_reason`, recorded on the order as an extension
    and surfaced as a warning. The check runs at placement **and again
    at signing**, so an allergy recorded after placement still blocks
    the signature; the recorded override carries the signature.
    Inactive/resolved allergies do not block.
  - *Duplicate check* — ordering a medication that already has a
    draft/signed/active order returns a `duplicate-active-order`
    **warning on the response; it is never a hard stop**.
  - *Drug-interaction checking is NOT implemented* — there is no
    interaction database in this system and nothing fakes one (see §7).
- **Consults** create a Task at placement (performer role on an
  extension); the task reaches the receiving role's inbox
  (`GET /orders/inbox?role=`) only once the order is signed/active, and
  completing/discontinuing/cancelling the order syncs the Task status.
- **Imaging orders** are ServiceRequests the existing radiology flow
  picks up unchanged (`POST /radiology/studies` with the order id —
  proven by test).
- New capabilities via the realignment pattern, with defaults on both
  sides: `orders.read` (admin/configurator/clinician/viewer),
  `orders.place` (admin/configurator/clinician).

### 2.2 Clinical documentation (`backend/app/documentation.py`, new)

- **Notes are FHIR DocumentReference** resources. Note types are
  published Studio terminology (`terminology/note-types`: progress,
  H&P, procedure, discharge) mapping each type to its **template — a
  published Studio FORM** (`form/note-progress`, `note-hp`,
  `note-procedure`, `note-discharge`). Structured answers are stored as
  a real **QuestionnaireResponse** (validated by the existing FHIR
  form validation — a missing required template item is a 422) and the
  note carries a Composition-lite content summary (template form,
  response id, sections, free text) on an extension; the API decorates
  responses with `noteType` / `noteContent`.
- **Lifecycle:** preliminary (draft) → final (signed); signing a
  non-draft is 409. **Amendment creates a NEW DocumentReference**
  (docStatus `amended`, `relatesTo` → replaces) — the original is never
  written again (test asserts version + body unchanged), and both
  versions are on the hash-chained audit log through the normal FHIR
  write path.
- **Flowsheets:** vitals + assessment rows are QuestionnaireResponse
  collections against published forms (`form/flowsheet-vitals`,
  `form/flowsheet-assessment`, seeded). `GET /flowsheets/{form}?patient=`
  returns the time-series view (form items × entries, answers
  normalised to value/unit). Unpublished form or missing required item
  → 422.
- New capabilities: `notes.read` (admin/configurator/clinician/viewer),
  `notes.write` (admin/configurator/clinician). Patient-role reads are
  scoped to the linked patient, as elsewhere (a patient token reading
  another patient's notes is 403 — tested).

### 2.3 MAR (`orders.py`: `GET /orders/mar/due`)

- Built from **signed/active MedicationRequests** plus the
  MedicationAdministration records written by the **existing pharmacy
  BCMA path** — administration is recorded there, never duplicated
  here (the MAR test dispenses + administers through `/pharmacy/*`
  and sees the dose turn `given`).
- Dose schedule: the order's own `dosageInstruction` timing when
  present, else the order type's published `timingDefaults`
  (frequency/window hours). Each dose is classified at read —
  **given** (an administration falls in its window), **due** (window
  open), **missed** (window closed, nothing given), **scheduled** —
  plus a summary block. **Missed is computed, never stored.**
- One cross-phase reconciliation, documented in code: Phase 4A
  dispensing writes status `dispensed` onto the request; the MAR
  keeps dispensed orders (the course is still being administered)
  until the order lifecycle completes/discontinues/cancels them. The
  fulfilment queue is unchanged (signed/active only).

### 2.4 Deployment profiles (roadmap §5)

- Two packs in `backend/packs/`: **`profile-solo-practice.pack.json`**
  and **`profile-clinic.pack.json`** (v1.0.0), each carrying a
  `configuration` list in its manifest: a navigation variant (module
  subset), a permissions variant, the POCT lab workflow variant, a
  branding variant, and the recall cohorts (`diabetes-recall`,
  `annual-physical-recall`, `flu-recall`) for the Phase 3 engine.
- **Apply-on-enable** (`app/studio.py`): enabling a pack with a
  `configuration` list publishes each entry as a **new version**
  (superseding the current published row), hash-chain audited with
  `via_pack` + `profile_apply` attribution, and the enable response
  lists what was applied. The apply is **atomic**: every entry is
  validated (payload validators + parameter resolution + no open
  draft on a target key) before anything is written; a failure aborts
  the enable with the installation still `attached` (409 for a draft
  conflict — tested, with the rest of the configuration verified
  untouched). Packs without `configuration` (all pre-Phase-5 packs)
  behave exactly as before.
- Manifest payload strings may reference pack parameters as
  `{{orgName}}`, supplied in the enable body
  (`{"parameters": {...}}`); `orgName` falls back to the currently
  published branding org name, then "OpenCare". Other missing
  parameters are a 422.
- **Disabling a profile does NOT unpublish** its configuration —
  applied configuration is governed Studio data, rolled back through
  the normal config path. This is stated in both pack descriptions
  and pinned by test.
- Solo vs clinic differences are data, not code: solo nav excludes
  ED/radiology/surgery and turns the patient self-scheduling request
  flag ON (capability `portal.self-schedule`, defined ahead of
  enforcement per the realignment pattern — the portal does not
  consume it yet); clinic adds radiology to the nav and leaves the
  flag off.
- **POCT lab mode** (`app/lab.py`): a lab order placed with
  `poct: true` carries an order-level extension. When — and only
  when — the deployment has published `workflow/lab-order-flow-poct`
  (the profile packs publish it at enable), result entry for a POCT
  order is single-step: the QC gate is checked **before any write**
  (a blocked entry writes nothing), then entry finalises and releases
  in the same transaction (report final, tags stripped, order
  released, result visible in the inbox/portal at once). Without the
  published workflow, a `poct: true` order follows the standard
  two-step verify → release path **exactly**; non-POCT orders are
  unaffected either way (all three combinations tested). The hospital
  path is the default and untouched.

### 2.5 Web

- **Patient chart tabs** (Overview / Orders / Notes / Flowsheet /
  MAR), each loading its data only when opened; write actions follow
  the runtime permissions matrix (`orders.place`, `notes.write`) for
  the signed-in role.
- **`components/FormRenderer.tsx`** — the generic published-form
  renderer the realignment audit found missing: it renders any
  published Questionnaire (typed inputs, choice options, groups,
  required markers) and builds exactly the QuestionnaireResponse
  items the server validates. **Reused by the Notes tab (note
  templates) and the Flowsheet tab (entry forms)** — one renderer,
  two config-driven surfaces, no per-form code.
- Orders tab: place single orders (type catalogue + required fields
  from config), place from order sets, warnings surfaced, lifecycle
  buttons per state. Notes tab: author from template, sign, amend
  (with the immutability explained in the UI). Flowsheet tab:
  items × time grid + entry form; the form list comes from the
  published form configs. MAR tab: window picker, summary counts,
  dose table with given/due/missed/scheduled.
- `orderset` added to the web `ConfigKind` union; the four new
  capabilities added to the web permissions mirror; the Vite dev
  proxy and the nginx same-origin proxy both learned the
  `/orders`, `/notes`, `/flowsheets` prefixes (the lesson of the
  2026-10-15 live CORS failure: new API surface must arrive on both
  proxy paths or the deployed UI cannot reach it).

## 3. New tests

Backend (47):
- `tests/test_phase5_orders.py` (19) — catalogue from config, draft
  placement, unknown type / missing required field / unknown patient,
  role gates, unsigned hidden from queue, illegal/unknown transitions,
  workflow-variant flip (removing draft→cancelled from the published
  machine makes it a 409 — behaviour follows config), allergy 422
  naming the allergen, override recorded + carried to signing, allergy
  recorded after placement blocks signing, inactive allergy ignored,
  duplicate warning (never a block), order-set expansion +
  per-order lifecycle, order-set atomic validation, consult Task +
  inbox visibility + Task sync, imaging → radiology pickup.
- `tests/test_phase5_documentation.py` (13) — note types from config,
  template answers stored as a real QuestionnaireResponse, required
  answers enforced, free-text notes, sign lifecycle + double-sign 409,
  amend creates a linked version with the original byte-identical,
  viewer read-only, patient cross-chart 403, flowsheet submit +
  time-series view, required-item 422, unpublished form 422,
  patient scoping of entries.
- `tests/test_phase5_mar_profiles.py` (14) — MAR due list from order
  timing, drafts excluded, given/missed computed against real BCMA
  records (clock moved in-module to prove `missed` is computed),
  unknown patient 404; profile enable applies all 7 configuration
  entries (nav subset, branding parameter, self-schedule flag, POCT
  workflow shape, recall registries listed), audit rows carry
  `via_pack`, enable supersedes the previously published version,
  clinic/solo differences, draft-conflict enable is 409 + atomic +
  installation stays attached, disable does not unpublish, plain packs
  unaffected; POCT without profile = two-step, POCT with profile =
  single-step released + inbox-visible, POCT entry refused whole
  (409, nothing written) when QC blocks, non-POCT order still two-step
  under a published POCT workflow.
- `tests/test_phase5_solo_day.py` (1, the §5 gate) — profile enabled
  with `orgName` parameter → 12 fabricated patients booked → patient
  solo-01: appointment check-in (booked → arrived), encounter +
  T2DM condition, vitals flowsheet via the form (view asserts the
  recorded values), progress note from template signed to final,
  POCT HbA1c resulted + released in one step and present in the
  results inbox, metformin refill signed → active, superbill claim
  generated from the encounter with both priced lines from the
  published fee schedule (44054006 $55 + 4548-4 $28 = $83 total).

Web (15):
- `api/client.test.ts` +5 — Phase 5 request shapes (order placement
  body, from-set, order transition `{to_status}`, note create
  snake_case + answers, sign/amend endpoints, MAR + flowsheet query
  encoding).
- `components/FormRenderer.test.tsx` +5 — per-type answer typing,
  optional-omission, group nesting, initial values, render + submit
  round trip, server-error surfacing.
- `pages/chart/ChartTabs.test.tsx` +5 — orders list + sign through the
  client, viewer sees no placement/actions, notes tab renders the
  published template form and submits typed answers, flowsheet grid
  from the view API, MAR summary + given/missed badges.

## 4. Existing-test changes (mechanism, not behaviour)

- `web/src/pages/studio/models.test.ts` — capability count 20 → 24.
  Justification: Phase 5 adds four capabilities (`orders.read`,
  `orders.place`, `notes.read`, `notes.write`) through the realignment
  pattern — defaults added to `configstore.CAPABILITY_DEFAULTS`
  (server) and the web mirror in the same change; the test counts the
  mirror's keys. The assertion's intent (the serialized matrix is
  complete) is unchanged and now covers the new capabilities.
- No backend test files were modified. Two backend behaviours were
  consciously reconciled, both covered by new tests and by the
  untouched pre-existing suites: (a) the lab order-detail 404 now
  precedes the governed-state 422 for a *nonexistent* order id on the
  generic transition endpoint (the order must be loaded to know
  whether it is a POCT order; existing tests use real ids and are
  unaffected); (b) the MAR includes `dispensed` medication requests
  (see §2.3) — a read-side widening of a new endpoint only.

## 5. Configuration-coverage list (roadmap §6)

Made configurable in this phase (all Studio config: versioned,
draft→publish, role-gated, hash-audited):

| Behaviour | Config | Notes |
|---|---|---|
| Order types + FHIR mapping + per-type required fields | `terminology/order-types` | new types are data |
| Medication timing defaults (frequency / dose window) | `terminology/order-types` → `timingDefaults` | per order type |
| Order lifecycle states, transitions, per-transition roles | `workflow/order-flow` | flip-proven by test |
| Order sets (membership, per-item extras) | `orderset/*` (new kind) | seeded admission-general, diabetes-admission |
| Note types + their template forms | `terminology/note-types` | template = published form key |
| Note templates (fields, required, types) | `form/note-*` | rendered by the generic web FormRenderer |
| Flowsheets (items, required, types) | `form/flowsheet-*` | same renderer; view is generic |
| Profile module subsets (navigation) | `navigation/main` variants in packs | applied at enable |
| Profile permission variants (incl. `portal.self-schedule` flag) | `permissions/role-matrix` variants in packs | merged over defaults server-side |
| POCT lab workflow (single-step states/governance) | `workflow/lab-order-flow-poct` in packs | applies to `poct`-flagged orders only |
| Profile branding (org name parameter) | `branding/default` variant in packs | `{{orgName}}` from enable parameter |
| Recall registries | `cohort/diabetes-recall`, `annual-physical-recall`, `flu-recall` in packs | Phase 3 engine consumes unchanged |

Left coded, with justification:

- **The allergy rule itself** (code-equality against active
  AllergyIntolerance, hard stop, override-with-reason): a patient-
  safety invariant, not a site preference — the same class as the
  lab visibility tag. What sites configure is the *data* it runs over
  (formulary/allergy codes). If a site ever needs warn-only allergy
  behaviour, that is a new governed policy config + SQA, not a
  per-site code fork.
- **Dose-window classification** (given/due/missed/scheduled): pure
  computation over config-supplied timing; the thresholds (frequency,
  window) are config, the arithmetic is the engine.
- **Profile apply mechanics** (validate-all → publish-all → enable, or
  nothing): a governance invariant of the pack lifecycle; the
  *contents* applied are entirely config.
- **FHIR resource shapes** (MedicationRequest/ServiceRequest/Task/
  DocumentReference/QuestionnaireResponse): the interoperability
  contract, not site configuration.

## 6. NOT RUN (honest list)

- **Drug-interaction checking** — NOT IMPLEMENTED and not faked: the
  system has no interaction database (RxNorm alone carries no
  interaction pairs). Allergy + duplicate checks are the decision
  support that ships; interaction checking waits on a licensed/curated
  interaction dataset becoming Studio terminology in a later phase.
- **Live browser E2E** — the web is proven by the vitest suite (150)
  and a clean production build; no live browser click-through of the
  new tabs was run in this environment (same standing caveat as prior
  phases: Min's rendered check on his machine remains the live gate).
- **Postgres runtime** — suites run on SQLite via the portable
  repository layer, as in every prior phase; the Postgres/Docker path
  is still unverified anywhere.
- **E-prescribing (Surescripts)** — a network integration, out of
  scope by the roadmap's own honesty rule (Phase 9 note); refill
  orders here are internal orders, not transmitted prescriptions.
- Pack enablement still does not gate module routes (the standing
  known gap from the realignment report) — profiles change
  configuration, and configuration drives the shell; route-level
  gating by pack state remains a design note, unchanged by this phase.

## 7. Files

Backend: `app/orders.py` (new), `app/documentation.py` (new),
`app/configstore.py` (orderset kind + validator, 4 capabilities),
`app/fhir.py` (DocumentReference + AllergyIntolerance support),
`app/lab.py` (POCT mode), `app/studio.py` (apply-on-enable),
`app/main.py` (wiring, health phase "5"),
`packs/profile-solo-practice.pack.json` (new),
`packs/profile-clinic.pack.json` (new),
`tests/test_phase5_orders.py`, `tests/test_phase5_documentation.py`,
`tests/test_phase5_mar_profiles.py`, `tests/test_phase5_solo_day.py` (new).

Web: `src/pages/ChartPage.tsx` (tabs + role), `src/pages/chart/`
(OrdersTab, NotesTab, FlowsheetTab, MarTab — new),
`src/components/FormRenderer.tsx` (new), `src/api/client.ts`,
`src/api/types.ts`, `src/pages/studio/models.ts` (+ test count),
`src/App.tsx`, `src/test/fixtures.ts`, `src/api/client.test.ts`,
`src/components/FormRenderer.test.tsx` (new),
`src/pages/chart/ChartTabs.test.tsx` (new), `src/styles.css`,
`vite.config.ts` + `nginx.conf` (proxy prefixes).

Not touched, per instructions: `mobile/`, `smart-app/`, `warehouse/`,
`agents/`. Not published to GitHub; not synced to any Mac.
