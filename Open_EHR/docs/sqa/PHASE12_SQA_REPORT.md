# Phase 12 SQA Report — Care-continuum operations + Capacity Command Center + safety operations

Date: 2026-10-18. Scope: PHASE10_ROADMAP Track D (care-continuum
operations), the Capacity Command Center + transfer centre addition,
Armstrong-pattern safety operations, Track J EMTALA pack. Staging
only; synthetic data only.

## Gate results

- **Backend: 428 passed** (393 Phase 11 baseline + 35
  new Phase 12 tests: `tests/test_phase12_continuum.py`). Final full
  run logged at `~/workspace/p12_final_suite.log` (428 passed in
  347.9s, exit 0).
- **ADK runtime service: 8 passed** (own suite, own venv; unchanged).
- **Web: 246 passed** (227 baseline + 19 new: 12 client wire-mapping
  tests in `src/api/continuum.test.ts`, 7 page/nav tests in
  `src/pages/ContinuumPages.test.tsx`), `npm run build` clean
  (exit 0, typecheck included).
- Two legacy register tests were adjusted for the same reason: the
  Phase 11 seeds test (`test_phase11_compliance_seeds`) and the
  Phase 10 compliance test (`test_register_seeded_with_hipaa_and_onc`)
  asserted the register total as exactly 15 and (the latter) the exact
  body set. Phase 12 legitimately grows the register to 21 entries
  under new bodies (EMTALA, CMS, The Joint Commission,
  CMS/The Joint Commission), so both assertions became floors /
  subsets. All citation-membership assertions in those tests are
  unchanged, and the HIPAA-only total (6) remains exact. The register
  is designed to grow phase by phase; exact-count assertions on it
  will keep breaking by design, which is why they were relaxed rather
  than bumped to 21.
- Two realignment navigation snapshot tests were extended the way
  earlier phases extended them (Phase 6 did the same for `beds`):
  the seeded-module list gained the six Phase 12 modules in order,
  and the without-a-module count went 12 → 18 (19 seeded − lab).
- Process note: the first final full run collided with a full /tmp
  tmpfs (retained pytest temp dirs from prior runs) and produced 33
  `sqlite3 disk I/O error` fixture errors in unrelated Phase 3/4A/4B
  files plus the two realignment failures above. The affected files
  were re-run clean (62 passed), /tmp was cleared, the realignment
  snapshots were extended, and the definitive full run above is green
  end-to-end on the final tree.

## Delivered scope

All Phase 12 surface hangs off one new backend module
(`backend/app/continuum.py`, router `/continuum`) and one published
config document (`terminology/continuum-ops`). New capabilities:
`continuum.read` (staff incl. viewer), `continuum.work` (admin /
configurator / clinician), `safety.report` (every staff role),
`safety.review` (admin / configurator / clinician) — server defaults
in `configstore.CAPABILITY_DEFAULTS` with the web mirror updated to
match, per the realignment pattern.

1. **ER operations.** The Phase 4B ED board enriched with per-ESI
   door-to-provider targets from config, breach flags, split-flow
   (fast-track = configured ESI levels 4–5) vs main queues, and the
   diversion/surge banner. STEMI / stroke / trauma activations:
   pathway + target minutes + milestone targets copied from config at
   start, milestone recording with per-milestone breach flags,
   duplicate-open and already-recorded guards (409), close with
   outcome. Diversion status is a config-driven state set, audited
   (`continuum.ops.diversion` in the hash chain). LWBS capture only
   from waiting/triage (a patient already in treatment is not LWBS),
   stamped as a chart extension. Results-callback queue derived from
   live data: departed ED patients with a DiagnosticReport whose order
   has not been released (or no order link), aged against the
   configured callback SLA, with completion recording. **EMTALA
   gate:** the MSE and transfer certification are governed forms
   whose required elements are the config lists; `transfer-out`
   refuses (422, naming the missing screening elements) when no MSE
   is on file for the encounter, validates the certification through
   the form machinery, and only then takes the governed departed exit.
2. **Inpatient operations.** Acuity-based nurse assignments (upsert
   per admission; acuity must be a configured level; per-nurse acuity
   load with an overload flag against the configured max), structured
   rounds lists per unit, I-PASS handoffs as a governed form (all five
   sections required). Discharge planning feeds a readiness board —
   plan target date / disposition / barriers from the Phase 6
   discharge plan, checklist completion, teach-back sign-off and
   follow-up-booked each computed from live records, with an explicit
   `missing` list per patient. The board is published as the
   `discharge-readiness` **dashboard config** (Phase 10 mechanism).
   EVS turnaround tasks are auto-created when a bed flips to cleaning
   and auto-completed when it flips back to available (both on the
   Phase 6 bed-status path), with waiting/turnaround minutes; a manual
   complete endpoint covers the same task record.
3. **Outpatient operations.** Clinic session templates (start time,
   slot minutes, capacity, overbook limit) are config; booking walks
   the template slots and refuses beyond capacity + overbook limit
   (409), flagging overbooked appointments. Rooming is recorded on the
   appointment as an extension and drives the rooming queue. No-show
   flags are **deterministic counts over the patient's own history**
   (fulfilled vs noshow; high-risk at the configured minimum history
   and miss-rate), with the basis string travelling in the payload —
   no model is claimed. Backfill worklist pairs freed slots
   (cancelled/noshow appointments in the next 7 days) with waiting
   waitlist entries, matching on the Phase 7 offer flow's own basis
   (slot type = appointment type / session). Referral loop closure
   derives the stage (sent → scheduled → awaiting-report → closed)
   from the Phase 7 referral + its report-back extension, with
   config-threshold stale flags; report-back is refused before
   completion (409). The async inbox aggregates open Tasks, patient
   message threads awaiting staff, pending critical-result
   notifications and pending callbacks, each aged against the
   per-type SLA from config and sorted breach-first. Panel care gaps
   are computed from Phase 9 data: patients on the diabetes /
   hypertension registries (by condition code) whose latest check
   observation is missing, stale or out of range per the configured
   comparator; outreach task generation is idempotent per
   patient + gap.
4. **Aftercare.** Teach-back sign-off as a governed form feeding the
   readiness computation; the follow-up-booked discharge guard exists
   and is **config-gated, shipping OFF** (Phase 7
   inert-until-published pattern): with the guard published on,
   `POST /inpatient/.../discharge` refuses (409) until a future
   appointment exists — proven by a config-flip test both ways. The
   48–72h post-discharge call queue is derived from A03 events with
   scheduled / due / overdue states from the configured window;
   outcomes are validated against the configured outcome list and
   recorded calls drop off the queue. Portal check-ins reach the
   inbox as patient message threads (Phase 9 engagement data).
   Home-health tracking is task-based with its own extension marker.
   The readmission-risk worklist scores finished discharges with
   **deterministic, config-weighted factors** (age bands, LOS, prior
   admissions, condition count, medication count, facility
   disposition) and returns the per-factor breakdown plus a basis
   string labelling it a triage aid, not a validated model.
   Hospital-at-home: eligibility flag on the chart, enrolment creates
   a class-HH encounter, a home-census board with open visit-task
   counts — workflow only; the board carries an explicit note that
   device data is Phase 14 and **no device feeds are fabricated**.
5. **Capacity Command Center.** `GET /continuum/command/overview`
   composes live census (Phase 6), the ED board (Phase 4B), the
   boarding snapshot, accepted transfers, today's A03 discharges
   (before-noon split) and open transport — the predicted census is
   deterministic arithmetic (occupied + boarding + accepted transfers
   − expected discharges today) and is labelled as such in the
   payload. Bed-request queue merges ED boarders, direct-admit
   boarders and accepted transfers; assign performs the real Phase 6
   admission (A01) through the inpatient module's own helpers.
   **Transfer centre:** intake, pending queue, accept/decline —
   decline requires a reason (422), decisions are one-shot (409),
   accept creates the admission Task; every step audited. Transport
   dispatch follows requested → assigned → in-transit → completed
   with illegal jumps refused. Boarding pressure feeds the Phase 10
   rules engine: bed-affecting events emit
   `continuum.boarding.changed`, and the seeded published rule
   `ed-boarding-escalation` (boarding_count ≥ 4) creates the
   bed-management escalation task — proven end-to-end with four ED
   admissions.
6. **Safety & quality operations (Armstrong pattern).** Any staff
   role can file (`safety.report`); harm taxonomy and event
   categories are config; serious harm per config auto-opens an RCA.
   Listings and detail shield a confidential reporter as the literal
   string "confidential" for everyone except the reporter; the detail
   endpoint admits the reporter or a `safety.review` holder only.
   Triage queue with status flow, harm reclassification, RCA
   workqueue, and a two-way reviewer↔reporter message thread
   (reporter-authored messages stay shielded on confidential
   events). Unit safety summaries back the published `unit-safety`
   dashboard config. A **safety-triage assistant agent definition is
   seeded as a DRAFT** (empty model route, `/continuum` allowlist) —
   definition only; runs remain honest-off like every agent seed.
7. **Regulatory.** The EMTALA regulatory pack is seeded as a DRAFT
   (packs never seed published) with parameters pointing at the
   controls in (1). Six register entries cite the built controls:
   EMTALA 42 CFR 489.24(a), (d) (MSE + stabilisation), 489.24(e)
   (transfer certification), 489.20(r)(3) (central log), CMS
   42 CFR 482.43 (discharge planning — cited twice: readiness board
   and aftercare controls), The Joint Commission Provision of Care
   (I-PASS handoffs) and CMS/Joint Commission discharge instructions
   (teach-back).
8. **Web.** Six new pages wired into the shell, navigation config
   and the permissions mirror: Command Center (census, projection,
   bed-request assign, transfer-centre intake/decide, transport,
   diversion/surge banner), ER operations (queues + targets,
   activations, callbacks, LWBS), Unit board (assignments +
   overload flags, readiness, I-PASS capture, EVS), Clinic day
   (sessions + overbook state, rooming + no-show flags, referral
   loop, care gaps, SLA inbox), Aftercare (call queue, readmission
   worklist, HaH board) and Safety (report form open to all staff,
   triage + RCA queues, follow-up thread). `/continuum` added to
   both proxy lists (`web/vite.config.ts`, `web/nginx.conf`) — the
   Phase 9 proxy-gap lesson applied to the new route group.

## Configuration-coverage list

All Phase 12 behaviour differences are published Studio config
(`terminology/continuum-ops` unless noted): ESI target minutes per
level; fast-track ESI levels; activation pathways with target +
milestone minutes; diversion states and surge levels (the payload
doubles as the diversion-state value set, which keeps it a valid,
republishable `terminology` document — a configurator can change
every value below through the governed API, proven by tests that
republish the document and watch behaviour change); EMTALA required
elements for the MSE and transfer certification (which also build
the seeded forms' items); inbox SLA minutes per type; clinic session
templates with capacity + overbook limits; no-show risk minimum
history + miss-rate threshold; referral stale days; readmission-risk
window, flag threshold, disposition list and per-factor weights;
call-queue window hours + outcome list; the discharge follow-up
guard switch (ships off); nurse acuity load limit + acuity levels;
harm taxonomy, safety categories and RCA-triggering harm levels;
care-gap definitions (registry key, condition code, check code,
comparator, target, recency window). Also config: the
`ed-boarding-escalation` rule (kind `rule`, threshold in rule
config), the `discharge-readiness` and `unit-safety` dashboards
(kind `dashboard`), the four governed forms (kind `form`), the six
new navigation modules (kind `navigation`), and the four new
capabilities in the permissions matrix (server defaults + web
mirror) — the same matrix a site can republish to move every gate.

## Compliance-coverage list

- EMTALA (42 CFR 489.24) → MSE documentation with config-listed
  required elements; stabilisation + transfer certification as
  governed forms; the transfer-out endpoint cannot complete without
  both (422 gate, test-proven); central-log retention parameter in
  the draft pack; diversion changes and transfers in the hash-chained
  audit. Register: three EMTALA entries + draft pack `emtala`.
- CMS Conditions of Participation, discharge planning
  (42 CFR 482.43) → discharge plan from admission, readiness board,
  teach-back sign-off, follow-up-booked guard, 48–72h call queue.
  Register: two entries citing the readiness and aftercare controls.
- The Joint Commission Provision of Care / NPSG handoff
  communications → I-PASS handoffs as a governed form with all five
  sections required. Register: one entry.
- Patient-safety reporting (Armstrong / PSO pattern) → confidential
  reporting with reporter shielding, harm taxonomy, RCA workqueue
  for serious events, two-way follow-up. (Operational control; no
  external submission is claimed — nothing leaves the system.)

## Honest scope notes

- Predicted census is a labelled deterministic projection, never a
  forecast model; readmission and no-show figures are deterministic
  config-weighted / history counts, labelled in every payload.
- Portal check-ins reach the inbox as Phase 9 message threads whose
  latest message is from the patient; there is no separate check-in
  resource type.
- EVS tasks auto-create/close on the Phase 6 bed-status path; a
  discharge that never flips its bed back leaves the task open for
  manual completion, by design.
- The discharge follow-up guard ships OFF: the control exists and is
  test-proven, and a site turns it on by publishing the config — the
  Phase 7 inert-until-published pattern.
- `terminology/pharmacy-ops` (Phase 11) has the same document shape
  as continuum-ops but no `concepts` value set, so it cannot be
  republished through the governed API either; Phase 12's document
  carries its diversion-state value set so the continuum surface does
  not inherit that limitation. Noted, not silently fixed (Phase 11
  scope).

## NOT RUN / gaps — honest list

- **Browser click-through E2E: NOT RUN** (unchanged VM limitation —
  Chromium local-network-access blocks loopback automation; do not
  retry. The pages are covered by vitest with stub clients at the
  same boundary as every prior phase, and the API surface is covered
  end-to-end by backend tests. A real-browser pass on the user's Mac
  remains the browser gate, after a sync that has not been requested
  for Phase 12.)
- **Live LLM run: NOT RUN** (unchanged; no model endpoint). The
  safety-triage assistant exists as a draft definition only.
- **Postgres/Docker runtime, live Keycloak OIDC: NOT RUN**
  (unchanged).
- **Pack-enablement route gating:** unchanged documented design gap
  (the EMTALA pack is a governed draft whose parameters describe the
  controls; publishing it does not yet rewire routes).
- Nothing was cut from the Phase 12 scope. Two scope calls made
  during the build, both recorded above: portal check-ins surface
  through the existing message-thread mechanism rather than a new
  resource type, and the follow-up discharge guard ships disabled
  (config-gated) rather than hard-on.

---

## Parent verification addendum (2026-10-18, independently re-run)

- Backend suite re-run by the parent agent (`pytest tests/` from
  `backend/`, log `~/workspace/p12_parent_verify.log`): **428 passed**,
  exit 0 — confirms the build run. Web suite re-run by the parent:
  **246 passed** (38 files); `npm run build` clean. ADK suite: **8
  passed**. Spot checks: `backend/app/continuum.py` present,
  `/continuum` in both proxy configs, PHASES.md Phase 12 row present.
- Environment note: the parent's first backend attempt errored with
  231 fixture errors and the first web attempts collected files but
  ran 79/0 tests — all three were the same cause: `/tmp` (a 512 MB
  tmpfs) was 100% full from retained pytest basetemps
  (`/tmp/pytest-of-root`, ~510 MB across two runs). After pruning, all
  suites pass at the counts above. The build's own first final run
  hit the same wall (33 disk-I/O fixture errors, recorded in the
  report body). House rule added to AGENTS.md: prune
  `/tmp/pytest-of-root` before every full-suite gate.
