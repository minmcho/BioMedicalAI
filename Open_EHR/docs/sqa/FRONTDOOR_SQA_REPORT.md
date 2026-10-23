# Front Door SQA Report — User Accounts, Master Data & Role Home Pages

**Date:** 2026-10-18
**Scope:** The Front Door & Master Data pass, answering the user's direct
inspection findings: master-data CRUD gaps (no user accounts, no patient
edit, no staff edit, no bed/ward management surface) and role home pages
that did not lead with the user's own work. Delivers: (1) real user
accounts with dev-token sign-in integration, (2) patient registration
with duplicate protection + a FHIR Patient update endpoint + edit forms
+ entered-in-error, (3) staff edit, (4) a Units & Beds manager over the
governed locations config, (5) unit-first home with attending / My
Patients, (6) a front-desk audience with a register → book flow,
(7) unit-scoped audience dashboards, (8) one compliance register entry
for unique user identification.
**Staging only** — not published to GitHub, not synced to any Mac.
**Synthetic data only.** The parked `agents/` draft and `docs/AGENTS.md`
were excluded throughout, per standing rule.

## Gate results (all re-run on the final tree, sequentially)

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **516 passed** (492 baseline + 24 new front-door tests) — deciding gate: two sequential half-suite runs on pruned /tmp, junitxml artefacts `~/workspace/gateFD_A.xml` (273 tests, 0 failures/errors/skipped) + `~/workspace/gateFD_B.xml` (243 tests, 0/0/0); the backend suite no longer fits this VM's 512 MB /tmp in one run (standing environment rule) |
| ADK service `pytest` | **8 passed** (unchanged) |
| Web `npm test` (vitest) | **290 passed, 43 files** (274 baseline + 16 new) |
| Web `npm run build` | **clean** (exit 0; only the pre-existing chunk-size advisory) |
| `/tmp` pruned before every full-suite run (standing environment rule) | done |
| Suites run sequentially, never overlapped | done |

New tests: backend `tests/test_frontdoor.py` (24); web
`src/pages/FrontDoor.test.tsx` (16).

Existing tests adjusted — each a legitimate growth/mechanism change,
each commented in code, no assertion weakened:

- `test_phase10_compliance.py`: HIPAA requirement count 6 → 7, plus a
  citation assertion for 45 CFR 164.312(a)(2)(i) (the new register entry
  in §8 below).
- `test_phase13_reports.py`: compliance register total 26 → 27.
- `test_phase13_people.py`: staff directory total 18 → 19 and the
  medicine service-line rollup 7 → 8 (seeded staff-019, front desk).
- `web/src/pages/studio/models.test.ts`: capability mirror 56 → 58
  (`accounts.manage`, `registration.work`).
- Three web test doubles completed with the new client methods the
  reworked pages now call on mount — `BedBoardPage.test.tsx`
  (`inpatientLocations`, `locationsDraftStatus`,
  `composeLocationsDraft`), `Phase13Pages.test.tsx` HomePage clients
  (`myPatients`, `myUnitPatients`) and its workforce client
  (`workforceConfig`, `listAccounts`). The doubles return honest
  empty/minimal fixtures; every pre-existing assertion is unchanged.

## Delivered scope

### 1. User accounts & registration (`backend/app/accounts.py`, router `/accounts`)

New `UserAccount` table (`user_accounts`): username (unique), full name,
sign-in role, unit, service line, email, phone, linked staff-directory
entry (`staff_id`), linked patient reference (required for the patient
role), status (`active`/`disabled`), created/updated audit fields.
Endpoints: create (admin-only, new capability `accounts.manage`, 201),
list (status filter validated), get, update (username immutable → 422),
`POST /{id}/disable` and `POST /{id}/enable` (already in the requested
state → 409). Every mutation writes a hash-chained audit event
(`accounts.create` / `accounts.update` / lifecycle); update audits carry
changed-field NAMES, never values.

**Dev sign-in integration (design decision — token handling).** In
`AUTH_MODE=dev`, `get_current_user` resolves the bearer token against
account records FIRST, by SHA-256 hash (`dev_token_hash`); only the hash
is stored, never the token. Built-in accounts are seeded for every
existing dev user, so the current tokens (`admin-token`,
`clinician-token`, …) keep working byte-identically — the entire
pre-existing suite passes unmodified on this point. An account created
through the API receives a generated dev token (`acct-<username>-<hex>`)
returned **once**, in the creation response only; it is not re-readable
(test-proven). A **disabled** account's token stops authenticating
(401, test-proven); re-enabling restores it. Creating an account with a
`staff_id` links the directory entry (writes the username onto the
unlinked staff record).

**Passwords are NOT stored and NOT faked.** The account model has no
password field. The Accounts UI states plainly that production sign-in
credentials live in the identity provider (Keycloak/OIDC — the OIDC mode
already implemented in `security.py`); accounts govern identity, role,
unit and status. Web: an Accounts section on the Workforce page
(`accounts.manage` gate) with a registration form carrying exactly the
fields above, a one-time-token reveal panel, and disable/enable actions
with the lifecycle status labelled in text (Active/Disabled) + badge.

### 2. Patient registration + edit (`backend/app/registration.py`, `app/fhir.py`)

**Registration config.** Published config `integration/registration`
carries the MRN system (`urn:opencare:mrn`), prefix `MRN`, separator
`-`, numeric width 6, and the duplicate-check match fields
(family/given/birthDate) — all configuration, editable in Studio.

**Endpoints.** `POST /registration/patients/check-duplicates` returns
candidate matches; `POST /registration/patients` creates (201) via the
existing FHIR Patient create. Validation is typed and server-side
(shared helpers in `app/validate_fields.py`): family name required;
birthDate a real calendar date and not in the future; gender in the
value set; email/phone format-checked; identifiers a list of
system/value strings; emergency-contact phone requires a contact name.
Duplicates (per the configured match fields) return **409 with the
candidate list in the body**; creation proceeds only when the caller
sends the explicit confirm flag, and the audit records
`duplicates_confirmed`. MRN generation takes the maximum numeric suffix
among identifiers matching the exact generated pattern + 1 — a manual
identifier in the MRN system is respected instead of generating one,
and a manual value that does not match the generated pattern never
moves the sequence (both test-proven).
`GET /registration/frontdesk/today` feeds the front-desk dashboard
(§6).

**FHIR update (design decision — Patient only, deliberately).**
`PUT /fhir/Patient/{id}` is the only update route added. The generic
FHIR store holds every resource type; opening a generic PUT would let
any `fhir.write` holder rewrite orders, results and encounters with no
per-type validation, which is not safe to ship in one pass. The Patient
route performs a full-resource replace with Patient-specific validation
(birthDate a real date, gender in the value set), requires the body id
to match the path id (422), returns 404 for an unknown id, and returns
404 — not 403 — to patient-role callers for other patients' records
(non-disclosure). The audit event `fhir.update` carries changed-field
NAMES only, never PHI values. **Concurrency:** last-write-wins. The
store's model carries a `versionId` that bumps on write but offers no
`If-Match` check; rather than fake one, the endpoint docstring states
the semantics plainly and the hash-chained audit makes every overwrite
reconstructible. `PUT /fhir/Encounter/{id}` returns 405 (test-proven).
**Entered-in-error** is the same PUT with `active: false` — there is no
delete route for patients, anywhere.

**Web.** `RegisterPatientForm` and `PatientEditForm`
(`web/src/components/PatientForms.tsx`) share one typed validation
module that mirrors the server rules. The register form pre-checks
duplicates and shows a warning panel naming the candidates; creating
anyway requires the explicit "Register anyway" action (the server 409
path is handled identically). The Patients page gains the register
panel (gated by the new `registration.work` capability) and a per-row
Edit toggle; the chart header gains "Edit patient details" (gated by
`fhir.write`). Entered-in-error is a two-step confirm in the edit form.
Create → edit → void round-trips are test-proven on the backend.

### 3. Staff edit (`PUT /people/staff/{staff_id}`)

Updates job role, unit, service line, status and contact fields,
validated against the published workforce config exactly like create
(unknown unit/role/service line → 422; unknown body fields → 422;
unknown staff id → 404). Audited as `workforce.staff.update` with
changed-field names. Web: an Edit action in the Workforce directory
(`workforce.work` gate) with selects populated from the workforce
config and inline errors.

### 4. Units & Beds manager (Bed Board page)

`GET /inpatient/locations/draft-status` reports the published locations
version and any pending draft. `POST /inpatient/locations/draft`
(`inpatient.work`) composes operations — add-unit, add-bed, rename,
retire — onto the locations config through the existing governed flow:
it starts from the published version or **extends the existing draft**,
preserving the Studio one-draft rule. Validation: code format
(`^[A-Za-z0-9][A-Za-z0-9-]*$`), duplicate code → 409, add-bed unit must
exist, retire of an occupied bed → 409, retire of a unit with active
children → 409, already-retired → 409; the composed payload passes the
Studio terminology validator before the draft is saved. **Nothing
changes live until a human publishes in Studio** — test-proven: a
manager action leaves the published census unchanged; publishing the
draft through the Studio route puts the new bed on the census; retiring
a bed drops it from the census (the locations read now excludes
inactive concepts). The UI says exactly this: a draft-pending banner
names System Studio → Terminology → locations, and every successful
compose shows "Nothing is live yet…".

### 5. Unit-first home + attending / My Patients

- `GET /people/my-unit-patients`: the caller's unit (via their
  staff-directory link) → live active admissions in that unit:
  encounter id, patient ref/id/name, bed, attending id/name, admitted
  date, length of stay in days, and a discharge-readiness flag computed
  from the continuum readiness logic. A caller with no unit gets
  `{unit: null, items: [], total: 0}` — no section, house fallback
  unchanged (test-proven).
- **Attending (design decision — stored on the encounter).** Admission
  accepts an optional `attending_staff_id`, validated against the
  staff directory (422 when unknown) and stored as the encounter
  extension `urn:opencare:extension/attending`, carried on the A01
  event. `POST /inpatient/encounters/{id}/attending` reassigns (must be
  in-progress → 409 otherwise; `null` clears), audited as
  `inpatient.attending.assign` with from/to. `GET /people/my-patients`
  returns active encounters whose attending is the caller's staff id.
- **Home layout (design decision — clinician always sees My Patients).**
  The Home page renders the caller's own work above the dashboard grid:
  clinician audience → My Patients first (always rendered for
  clinicians, even when empty, so the absence of assigned patients is
  itself visible), then the unit list; nurse / unit-manager → unit list
  first; a nurse/manager with no unit gets a plain-text explanation,
  then the dashboard. Readiness is shown as a labelled badge
  ("Ready for discharge" / "Not ready") — text + colour, never colour
  alone.

### 6. Front-desk audience + home

The workforce config gains the `front-desk` job role and an audience
mapping to a new seeded published dashboard `audience-front-desk` whose
widgets read `GET /registration/frontdesk/today`: today's appointments
with status, arrivals awaiting check-in (booked, not roomed),
appointments awaiting a room (arrived/checked-in without a rooming
extension), open waitlist count, and registrations today. Seeded
synthetic staff-019 (Rita Santos, front-desk, clinic unit, medicine)
plus a built-in dev account `dev-frontdesk` / `frontdesk-token`
**(design decision — front-desk role):** the sign-in role is `viewer`;
the audience is driven by the front-desk JOB ROLE through the Phase 13
directory resolution, which keeps sign-in roles coarse and job roles
expressive, exactly as the workforce model intends. The booking gate
was widened honestly: `POST /continuum/clinic/book` now accepts
`continuum.work OR registration.work` (a local gate in `continuum.py`);
rooming and transition gates are **untouched** — a front-desk account
booking works, rooming as front-desk is 403 (test-proven, documents the
boundary). Check-in continues to use the existing appointment
transition/rooming machinery; nothing was duplicated. **Register →
book in one flow:** after a successful registration the Patients page
hands the new patient to the Clinic Day page with the booking form
pre-filled; both pages show the step strip (§Design) naming the current
step. The front-desk home leads with quick actions — "Register a new
patient" and "Book an appointment".

### 7. Unit-scoped dashboards

`/people/analytics/overview`, `census-series`, `units` and `coverage`
accept an optional `unit` query parameter scoping their computation
(unknown unit → 422). The home-dashboard payload rewrites widget
endpoint URLs with `?unit=<caller's unit>` for the **nurse and
unit-manager audiences only** — a deep copy; clinician, executive and
department-head payloads are byte-identical to before (test-proven:
two units' managers see different, correct numbers; clinician and
executive payloads carry no unit parameter).

### 8. Compliance

One register entry added, following the register's existing dedupe/seed
patterns: **HIPAA 45 CFR 164.312(a)(2)(i) — Unique user identification**,
mapped to the account system (unique username enforced at the database
and API layers, disable stops authentication, full lifecycle audited).
Register total 26 → 27; HIPAA entries 6 → 7. No other register change.

### 9. Web plumbing

`/accounts` and `/registration` added to both proxy lists
(`vite.config.ts`, `nginx.conf`) — the Phase 9/10 lesson that every
route group must be listed. Dev sign-in gains the front-desk account.
New styles are limited to three classes in the existing design system
(`.field-error`, `.workflow-steps`, `.patient-row-line`).

## Configuration-coverage list (standing rule: configure, don't fork)

Everything behavioural in this pass is configuration data, versioned
and governed like the rest of the system:

- **Capabilities:** `accounts.manage` (admin) and `registration.work`
  (admin/configurator/clinician/viewer) join the published capability
  defaults — 58 total; sites re-map them in Studio.
- **MRN format:** system URI, prefix, separator and numeric width are
  the published `integration/registration` config, as are the
  duplicate-check match fields (family/given/birthDate).
- **Workforce config:** the `front-desk` job role, its audience mapping
  (`audienceByRole` / `audienceDashboards`), units and service lines
  used by every account/staff select — all published workforce config.
- **Audience dashboard:** `audience-front-desk` is a seeded published
  dashboard payload (widgets over `/registration/frontdesk/today`),
  editable like every other audience dashboard.
- **Locations:** units/beds remain the published `terminology/locations`
  config; the manager only composes drafts of it.
- **Accounts themselves** are data: creating, disabling or reassigning
  an account is an admin operation on records, never a code change.
- **Attending** is an encounter extension URN
  (`urn:opencare:extension/attending`), consistent with the store's
  extension model.

## Compliance-coverage list

- **HIPAA 45 CFR 164.312(a)(2)(i)** (unique user identification) — NEW,
  mapped to §1: unique usernames, token stops on disable, audited
  lifecycle. Register 26 → 27 entries, HIPAA 6 → 7.
- **Audit integrity (existing framework, extended):** every new
  mutation — account create/update/disable/enable, patient create,
  Patient PUT, staff update, attending assign, locations draft compose
  — writes a hash-chained audit event; PHI-bearing audits carry field
  NAMES or identifiers (MRN, staff ids), never clinical values.
- **Non-disclosure:** patient-role callers receive 404 (not 403) for
  other patients' records on the new PUT route, matching the store's
  existing read posture.
- No other register entries were added, removed or reworded.

## Quality bar (user standing rule: complete system, not a prototype)

Every form this pass created or touched was held to the user's
validation standard:

1. **Validation by value type, client AND server.** Shared server
   helpers (`app/validate_fields.py`) and a shared client module
   (`PatientForms.tsx`) enforce the same rules: birth dates must be
   real calendar dates (2026-02-30 is rejected on both sides) and not
   in the future; emails and phones are format-checked (phone: dialable
   characters, 7–15 digits); codes match their pattern; required fields
   are enforced. Failures produce inline, field-level error messages a
   user can act on — never a silent failure. All forms authored in
   this pass carry `noValidate`, because native HTML5 validation
   otherwise swallows the submit event before the app's own validation
   runs (found by test; true in real browsers too). Tests prove
   rejection of bad types on the registration and patient forms on
   both sides (backend 422 cases; web inline-block cases asserting the
   API is never called).
2. **Selects are populated from real endpoints/config — or disabled
   with the reason shown.** Role/unit/service-line/staff-link selects
   read the published workforce config and account data; the clinic
   session select reads published sessions; the bed-manager unit
   select reads published locations. When a source is empty the
   control renders disabled with explanatory text (test-proven), never
   a blank mystery list.
3. **No dead controls.** Every button in the touched panels performs
   its action or shows a visible error: account disable/enable,
   staff edit save, manager compose actions, registration submit,
   duplicate override, entered-in-error confirm, booking submit — each
   has a success notice or an inline error path, covered by tests.

## Design standard (user steer: beautiful, simple, workflow-visible)

The new and reworked pages follow the existing design system only — no
new visual language (three small CSS classes added, all in the current
idiom). Each panel has a clear title and ONE obvious primary action;
secondary actions (disable, retire, entered-in-error) are visually
quiet. Workflow participation is shown visibly and in words: the
front-desk flow carries a step strip (Register → Book → Check in) with
the current step named in text and highlighted; account lifecycle is a
labelled Active/Disabled status; discharge readiness is a labelled
badge; the bed manager's draft state is a labelled banner with the
publishing path spelled out — always text + colour, never colour
alone. Common tasks take as few clicks as the flow allows: registration
hands straight into a pre-filled booking; the clinician home opens on
the clinician's own patients. Anything not needed for the task at hand
stays out of the panel (the account form carries no password field,
because passwords are not the account system's job — and says why).

## NOT RUN / gaps (honest list)

- **Browser click-through E2E: NOT RUN** — the standing VM limitation
  (loopback browser checks are blocked in this environment); not
  retried, per instruction. The user's own Mac run remains the browser
  gate. All behaviour above is proven by API and component tests.
- **Postgres/Docker runtime: NOT RUN** (standing) — the suite runs on
  the repository's SQLite test configuration, as in every prior phase.
- **Live Keycloak/OIDC sign-in: NOT RUN** (standing) — OIDC mode exists
  in `security.py`; this pass integrates accounts with the dev-token
  mode only and says so in the UI copy.
- **Live LLM, live TLS, pack route gating, mobile compile: NOT RUN** —
  standing list, unchanged and unaffected by this pass.
- **Generic FHIR update: deliberately NOT built** — Patient only (§2);
  other resource types keep their existing domain-specific write paths.
- **Password management: deliberately NOT built** — identity-provider
  responsibility; not faked (§1).

## Incidents / notes from this build

- Three real bugs were caught by this pass's own tests and fixed, not
  papered over: (a) MRN sequencing initially matched manual identifiers
  by substring, letting a hand-typed value move the sequence — now an
  exact generated-pattern match; (b) forms without `noValidate` had
  their submits swallowed by native browser validation before React
  validation could run — applied to every form this pass authored;
  (c) the first full web run failed 12 pre-existing tests because the
  reworked pages call new client methods on mount that the older test
  doubles did not stub — the doubles were completed with honest
  fixtures (§Gate results); no page behaviour was changed to suit a
  double.
- The first background launch of backend half B was lost when its exec
  session was reaped; it was re-run cleanly and its junit artefact
  (`gateFD_B.xml`, 243 tests, 0 failures) is the deciding record.
- Nothing was cut from the assigned scope.

---

## Parent verification addendum (2026-10-18)

Independently re-run by the parent agent after the build handoff; the
builder's numbers were NOT taken on trust.

- Artefacts checked first: `gateFD_A.xml` records 273 tests and
  `gateFD_B.xml` 243 (builder halves, 516 claimed); `app/accounts.py`,
  `app/validate_fields.py`, and this report all present; the PHASES.md
  "Front door" row present.
- Backend, parent run, same gate procedure (no pytest running at
  start, `/tmp/pytest-of-root` pruned, two sequential halves over the
  test files, alphabetical split): half A **285 passed** (exit 0),
  half B **231 passed** (exit 0) — total **516 passed**, matching the
  builder's 516 exactly. The halves split differently from the
  builder's (285+231 vs 273+243) because the file split differs; the
  total is the deciding figure, and it agrees.
- Web, parent run: **290 passed** (43 files), `npm run build` clean.
- ADK service suite, parent run: **8 passed**.
- Every builder figure confirmed exactly; no count correction is
  needed for this pass (contrast Phases 13/14, where overlapped runs
  had corrupted the builder's arithmetic).
- Standing NOT RUN list unchanged: real-browser click-through (VM
  loopback block), Postgres/Docker runtime, live Keycloak OIDC/MFA,
  live LLM run, live TLS serving, pack-enablement route gating, mobile
  compilation.

