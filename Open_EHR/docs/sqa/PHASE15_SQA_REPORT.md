# Phase 15 SQA Report — Burn-down BD-03…BD-35, Laboratory completion, Radiology department, Surgery depth, UX (dropdown management + master-detail), Results Inbox rebuild, Barcode visibility + RFID

**Date:** 2026-10-18/19
**Scope:** Phase 15 of the pre-release burn-down plan
(`OpenCare-PreRelease-Burndown-Plan.md`, 33 items BD-03…BD-35 assigned
to this phase, followed verbatim) plus the standing Phase 15 scope
(oncology regimen depth, infection prevention, payer/care-management)
plus four scope additions made by Min during live click-through on
2026-10-18: **(B)** laboratory completion — Test Manager + Test Panel
Manager as in-panel Lab tabs, specimen aliquot/split lifecycle,
parent–child barcode chain-of-custody; **(D)** a full Radiology
department ("Radiology is empty"); **(E)** Surgery department depth;
**(F)** dropdown list management for authorised roles + a shared
master-detail component; **(G)** the Results Inbox rebuild ("Result in
Box does not work — need more details and features"); **(H)** barcode
visibility (label printing, universal lookup, seeded demo barcodes)
and a new RFID workstream on the Phase 14 device spine.
**Staging only** — not published to GitHub, not synced to any Mac.
**Synthetic data only.** The parked `agents/` draft and
`docs/AGENTS.md` were excluded throughout, per standing rule.
**Coordination:** 24 build workers (14 backend, 10 web) built against
a shared brief (`~/workspace/phase15_work/SHARED_BRIEF.md` /
`WEB_BRIEF.md`); the coordinator pre-wired 15 skeleton modules into
`main.py`, applied every cross-cutting integration edit (nav seed,
capabilities, rules-registry signals, FHIR Immunization support, the
BD-31 booking-path gate, the portal telehealth list endpoint), and ran
the full gate sequentially on the final tree. Worker-reported counts
were never banked; every number below is from the coordinator's own
runs.

## Gate results (coordinator-run, sequential, on the final tree)

| Suite | Result |
|---|---|
| Backend `pytest tests/` | **897 passed, 4 skipped, 0 failed** — 901 tests, 76 files. Baseline 695 + 202 new Phase 15 tests. Run as 8 sequential alphabetical chunks (~9–10 files each) with per-chunk private `--basetemp` directories deleted after each chunk (see Incidents: a single half-run now fills the 512 MB /tmp tmpfs at 75 files) + a full-file re-run of `test_realignment.py` after its nav snapshot fix + the post-gate closure run (`test_phase15_telehealth_portal_list.py` 2 + `test_phase15_access_services.py` 10 = 12 passed). Logs: `~/workspace/phase15_work/gate_c00.log`…`gate_c07.log`, `gate_realignment.log`. The 4 skips are the standing env-gated live-engine tests (Postgres/MariaDB URLs unset in the default gate). |
| Web `npm test` (vitest) | **389 passed, 0 failed, 58 files** — full run 387 passed / 57 files (`gate_web.log`) after one fixture fix (`gate_web_fix.log`), plus the post-gate portal-telehealth closure file (2 tests) verified with the pre-existing portal suite (6/6, `gate_telehealth_web3.log`). Baseline 322 + 67 new. |
| Web `npm run build` | **clean** — exit 0, zero TypeScript errors on the final tree (`gate_build_final.log`; only the pre-existing chunk-size advisory). |
| ADK service `pytest` | **8 passed** (unchanged). |
| Go `go test ./...` (services/go) | **ok** — all packages. |
| Rust `cargo test` (services/rust) | **33 passed** (unchanged). |
| `/tmp` pruned / private basetemps per chunk | done — see Incidents. |
| Suites run sequentially, never overlapped | done. |

New backend test files (16, 202 tests): `test_phase15_oncology_infection.py`
(11), `test_phase15_barcodes_rfid.py` (12), `test_phase15_care_coordination.py`
(12), `test_phase15_laboratory.py` (22), `test_phase15_surgery.py` (12),
`test_phase15_listmgmt.py` (10), `test_phase15_radiology.py` (23),
`test_phase15_support_ops.py` (13), `test_phase15_packs.py` (18),
`test_phase15_support_services.py` (16), `test_phase15_care_management.py`
(13), `test_phase15_platform_depth.py` (10), `test_phase15_access_services.py`
(10), `test_phase15_results_inbox.py` (13), `test_phase15_bd31_gate.py`
(5, coordinator-written), `test_phase15_telehealth_portal_list.py`
(2, coordinator-written).

## What was built — scope A: the 33 burn-down items, one by one

**BD-03 — Specialty depth for the seven structural packs.** cardiology,
diabetes-endocrinology, gastroenterology, geriatrics,
pulmonology, obstetrics-gynecology and orthopedics each move
1.0.0 → 1.1.0 in `backend/packs/` reaching the Phase 9 exemplar depth:
service-line order sets (gated on their own pack's service),
registries, pack-specific flowsheets/forms, and at least one
pack-specific safety/workflow rule per pack — cardiology post-MI
beta-blocker BLOCK + cardiology registry case-task FLAG;
diabetes A1c > 9 FLAG + care-gap FLAG; GI upper-GI-bleed transfusion
BLOCK + cirrhosis FLAG; geriatrics Beers-criteria BLOCK +
anticholinergic FLAG + a governed `rule` config (the pack's third
mechanism); pulmonology COPD FLAG + COPD-exacerbation BLOCK on an
empty monitoring set; OB postpartum-haemorrhage BLOCK + preeclampsia
FLAG; orthopedics VTE-prophylaxis BLOCK + implant-infection FLAG.
Rule BLOCK/FLAG behaviour is proven by mechanism tests, not asserted:
each pack's rule is evaluated against a seeded patient whose data
meets its threshold. All artefacts are pack/config data — no code
carries a specialty difference.

**BD-04 — ICU / acute-care module.** New `app/icu.py`: governed forms
`icu-acuity` (SOFA + ventilation + vasopressor + RASS items),
`icu-devices` (central line / urinary catheter, with insertion dates)
and `icu-handoff` (I-PASS items) authored as governed form configs;
device-day counting with same-day dedupe on patient + device +
calendar day; the ICU board is computed from `inpatient.census()`
(patients in published ICU-type units only — `unit-icu` seeded,
`icu`-type unit types in `terminology/locations`) with device days,
acuity and outstanding-handoff flags per patient; sign-in/out,
pronation and spontaneous-breathing-trial events on the board
workflow; ECMO/CRRT/IABP tracked as device-day kinds. Web: the ICU
rides the documentation machinery (governed forms); there is no
dedicated ICU web page in this phase — recorded as a limitation.

**BD-05 — Transplant service line.** NEW governed pack
`specialty-transplant` 1.0.0 at exemplar depth: waitlist flowsheet
(`transplant-waitlist`), donor–recipient linkage form
(`transplant-donor-linkage` with a governed
`transplant-donor-types` terminology — living/deceased/…),
immunosuppression monitoring on the generic pharmacy hook, ONE seeded
DRAFT agent (`transplant-waitlist-coordinator`, honest-off, never
enabled), and service-line dashboards.

**BD-06 — Dialysis service line.** NEW governed pack
`specialty-dialysis` 1.0.0: treatment flowsheet
(`dialysis-treatment`), adequacy form (`dialysis-adequacy` — Kt/V,
URR) evaluated on the real lab spine, vascular-access tracking
(`dialysis-vascular-access`), and session scheduling via a republish
of `terminology/self-scheduling` slot types adding `dialysis-session`
(240 minutes) — proven by test (the new slot type and duration appear
in the published config).

**BD-07 — Dedicated therapy (PT/OT/SLP) scheduling.** New therapy
service on the Phase 7 prior-auth machinery: recurring treatment
plans, therapist calendars, and visit-count enforcement against the
covering authorisation (`access.find_covering_auth`): completing a
session with no covering authorisation returns a `no-authorisation`
state and records ZERO sessions; completing beyond the authorised
count is refused 409. No auth machinery was duplicated.

**BD-08 — Paediatrics pack + paediatric capacity.** NEW governed pack
`specialty-paediatrics` 1.0.0: age-banded vitals on the
`paeds-vitals` flowsheet (age-band choice field), and paediatric
capacity as a distinct class — `terminology/locations` republished
adding `unit-paeds` (4 beds); the command-centre census total moves
18 → 22 and the paeds unit appears as its own capacity row,
test-proven. Gentamicin monitoring uses the pharmacy hook (BLOCK on
no level in 72 h). **Named residual against the plan's wording:**
weight-based dose checking is NOT delivered — the published
monitoring vocabulary carries lab presence/range signals per
medication code and cannot express mg/kg dosing; nothing claims it.
Extending the vocabulary is configuration-surface work for a later
phase; it is recorded here, not silently dropped.

**BD-09 — Blood-bank lookback & FDA deviation reporting.** On the
Phase 6B blood-bank spine: a donor test-result trigger opens a
lookback that traces components → recipients, quarantines affected
units and records consignee/recipient notifications; a
biological-product deviation report is generated as a PDF through the
report engine (`reports.build_pdf`). Traceability retention is
configuration: published `terminology/blood-bank` carries
`lookback.retentionYears: 10` and `retain_until` computed from it
(21 CFR 606.160). Scope note (design decision, recorded): the trace
covers components dispensed from the same order and units sharing the
same donation attribute — the two linkage paths the blood-bank data
model carries.

**BD-10 — CLIA six-element competency model.** Published
`terminology/lab-competency` carries the six CLIA elements; per
testing-person, per test-system competency records with currency
computed from the records; expiry surfaces on a workforce-pattern
worklist. Compliance entry below (CLIA 42 CFR 493.1235).

**BD-11 — Hospital-at-home operating stack.** On the Phase 14 device
spine: home telemetry bindings tie published device-registry devices
to home patients; a reading breaching the device's published range
raises a REAL FHIR Task (escalation path proven end to end); an
in-home visit/supply dispatch queue; rapid-response coordination;
kit issue/return records. Readings carry `source_label: "simulated"`
— no home device feed exists in this environment.

**BD-12 — Patient transport dispatch.** Built ON the Phase 12
`TransportRequest` model (no second transport table): a
`SupportTransportDetail` side table carries dispatcher assignment,
transporter lifecycle and turnaround metrics; the queue, assignment
and lifecycle are served from `app/support_ops.py` and surfaced on
the command-centre surface.

**BD-13 — Command-centre desk/role/cadence model.** Published
`terminology/command-desks` defines the named desks (bed management,
access/transfer, transport), role staffing per shift (overlap
refused 409), a capacity-huddle artefact produced on cadence, and an
ICU-triage role whose `triageAuthorityRole` is `physician`:
a triage decision by a caller without that job role is refused 403,
and every decision is audit-chained with the authority recorded.

**BD-14 — Access-line call-centre surface.** Telephony intake LOG
(call logging and workflow, not a telephony platform): caller,
referral source, clinical summary, disposition; a disposition of
transfer-requested creates a REAL Phase 12 `TransferRequest` — the
test asserts the SAME id, so the access line joins the transfer
centre rather than shadowing it; volume reporting by source.

**BD-15 — CUSP unit-based safety programme.** On the Phase 12 safety
spine: unit safety-team workspace, CUSP meeting artefacts, and a
defect-learning log whose defects link to validated SafetyEvents
(a defect referencing a non-existent event is refused).

**BD-16 — HICS incident-command layer.** Governed configuration over
the downtime/continuity mode: incident roles, incident log,
situation reports; activation publishes a NEW governed version of
`integration/downtime` and is fully audited. **Design decision
recorded:** activation does NOT set the downtime `readOnly` flag —
doing so would block the incident response's own writes; the flag
remains an operator decision.

**BD-17 — Supply chain / materials management.** Non-pharmacy
supplies catalogue, unit PAR levels, requisition → fulfilment
workflow, and OR preference cards (shared with BD-17's surgery
consumer, scope E). The procurement exchange is a governed interface
verified on file fixtures and labelled `basis: "file-fixture"` —
no live ERP exchange exists or is claimed.

**BD-18 — Food & nutrition / diet office.** Diet orders flow from
the chart to a diet-office worklist; diet types are the published
`terminology/diet-types` list (now also manageable in context,
scope F). Unit attribution per row names its source (order-explicit
unit, else bed-derived — the source is stated on the row, never
hidden). Allergy cross-check at tray assembly is a HARD STOP: a tray
check against a patient's recorded allergen is refused 422 naming
the allergen.

**BD-19 — Interpreter services.** Request → scheduling → fulfilment
lifecycle with modality (in-person/phone/video) as published config
(`terminology/languages` + interpreter config), request-to-encounter
linkage, and fulfilment timing metrics. The video modality's join
link is generated from the SAME published `integration/telehealth`
URL template the telehealth lane uses — one template, proven by test.

**BD-20 — Patient experience surveying + service recovery.**
HCAHPS-class survey instrument as a governed form pack; distribution
lists derived from discharge (ADT A03) events; results land on the
quality surface; low scores/complaints open a service-recovery
workqueue. Small-cell suppression (< 5) is applied to reported
results, consistent with the quality engine's existing rule.

**BD-21 — Utilisation review against clinical criteria.** UR case
queue, medical-necessity review status, physician-advisor second
review (advisor == first reviewer refused 409), and denial-risk
flags JOINED to revenue's existing denials queue (`/ur/denial-risk`
is a labelled join feed, not a duplicate queue). Criteria are
bring-your-own governed configuration: published
`terminology/ur-criteria` ships **SITE-FIXTURE examples only** —
licensed InterQual/MCG-class content is never shipped or invented,
and every surface that shows criteria carries the fixture label.

**BD-22 — Online patient bill pay.** Portal surface on the Phase 7
statement/ledger (`revenue.patient_statement` reused): balance
view, payment recording against the ledger through the governed
`integration/payment-provider` interface. **Test mode only:**
a non-test-mode provider configuration is refused 409; receipts
carry `live: false` and the test-mode disclaimer verbatim
(proof: balance 20.00 → pay 12.50 → 7.50 remaining). A live
processor is a deployer contract — NOT RUN.

**BD-23 — Portal proxy access.** A patient grants a named proxy
scoped access with scopes from published `terminology/proxy-scopes`
(schedule/messages/results subsets), revocable at any time; EVERY
proxy read is hash-audit-chained with `on_behalf_of` attribution —
the test asserts the AuditLog rows name the proxy acting for the
patient.

**BD-24 — Coordinating-physician longitudinal model.** New
`app/care_coordination.py`: a named coordinating clinician per
patient, assigned on the chart, carried through referrals and
episodes (BD-25), visible as the accountable owner on panel/home
surfaces. Reassignment SUPERSEDES — the prior assignment is never
edited; the longitudinal chain is the record.

**BD-25 — Multispecialty episode object.** Episodes are their OWN
table (FHIR EpisodeOfCare is not in the store's supported set — a
documented adjustment, not a silent substitution): one shared
episode spanning specialties with linked encounters/orders/notes and
per-specialty contributions under a single coordinated plan; the
episode carries the BD-24 coordinator.

**BD-26 — GME operations.** On the workforce spine: duty-hour logging
with limit rules as published config (80 h/week, 24 h continuous,
8 h off — violations computed with exact arithmetic, test-proven at
the boundary), evaluations as governed forms, trainee rotation
scheduling.

**BD-27 — eConsults + automatically triggered consults (TACo).**
The advice-only consult object: ask → answer in-chart as a completed
unit of work (no visit), with its own queue, SLA and closure states.
Published-rule initiation: the trigger scan reads governed
`terminology/econsult-triggers` (seeded: diabetes HbA1c — LOINC
4548-4 — greater than 9.0) and opens an eConsult from chart data
with the triggering rule cited in the audit. Agent-initiated
consults are NOT built here (Phase 16 supervised workflows gate
them, per the plan).

**BD-28 — Telehealth at scale.** Per-service configuration layered
over the Phase 9 lane (`integration/telehealth-services`), virtual
check-in/waiting states, and a virtual-no-show terminal state
distinct from a plain no-show. Portal outcome capture is now RUN
and evidenced (this converts the audit's NOT RUN): a new
`GET /portal/telehealth/visits` lists the signed-in patient's own
visits only (staff refused 403), and the portal's "My virtual
visits" section renders the governed `form/telehealth-outcome`
through the shared FormRenderer and submits a real
QuestionnaireResponse. In-product video is NOT built — the
external-provider join pattern remains the architecture and the UI
says so.

**BD-29 — Service-line & episode cost accounting.** Activity costs
from published `terminology/activity-costs` rolled up by service
line and by BD-25 episode over live module data (including a real
ETL/warehouse leg). Exact-total test: $3,897.00 for the fixture
inpatient stay, $3,925.50 grand total including the ETL-attributed
activity. Honest note: OR minutes attribute 0 in the fixture (no
timed OR activity exists in the synthetic data) — shown as 0, not
estimated.

**BD-30 — Dashboards rewired to the indicator library + seeding
decision.** A widget-to-indicator trace for ALL seeded dashboards
is served at `GET /platform-depth/dashboard-indicators`: every
widget of every seeded dashboard appears with either its indicator
endpoint or a named reason. 9 of 41 widgets across 9 dashboards are
rewired to resolve through `/indicators/<key>/value` at dashboard
v2, each keeping its original endpoint recorded as `sourceEndpoint`.
The remaining widgets carry their reasons in the trace (multi-row
tables and charts cannot be a scalar indicator value; several
scalars have no named indicator source yet). **Seeding decision
recorded:** dashboards stay explicitly seeded config (no
auto-seeding); widgets migrate to indicators incrementally as named
sources are added — the trace endpoint is the migration ledger.
Disposition against the plan's literal wording ("every widget
resolves through the library"): the TRACE is complete; the
REWIRING is partial (9/41) with per-widget reasons — recorded as
partial, not rounded up.

**BD-31 — Credential-gated scheduling.** A checker service
(`evaluate_credential_check`) reads the Phase 13 credential records
at booking time; it is WIRED into the real booking path —
`access.accept_waitlist_offer` evaluates the check when a
practitioner and a named privilege are both present: an absent or
expired credential writes the audit row
(`scheduling.credential_check`) and refuses the booking 409 with
the reason; an allowed booking proceeds; offers without a named
privilege behave exactly as before (zero behaviour change).
Coordinator-written gate tests: 5 (blocked on expired credential,
allowed control, audit attribution, no-privilege passthrough,
endpoint path).

**BD-32 — Visual report designer.** A Studio surface (mounted as a
Studio tab) composing report definitions visually —
dataset/fields/filters/grouping/layout — over the platform report
engine; definitions remain governed config and raw SQL is still
refused. The backend datasets endpoint (`GET /designer/datasets`)
serves the palette from the engine's named queries; a parity test
proves the designer's palette equals the hand-authored dataset set.
Two honest gaps, labelled in the UI: there is no validate-only
endpoint (Validate runs server validation through the governed
draft endpoint; a 422 persists nothing), and no draft-preview run
(Run preview executes the PUBLISHED definition and is labelled as
such). A designer-built definition renders identically to its
hand-authored equivalent where both exist (parity test).

**BD-33 — Immunisation record + VXU reporting.** FHIR Immunization
is now a supported resource in the store (validation: status,
vaccineCode, patient reference; patient-reference resolution
extended accordingly). Capture validates vaccine codes against the
published CVX terminology; portal capture is restricted to the
patient's own records. VXU^V04 generation (`hl7.build_vxu`) rides
the Phase 8 HL7 spine for config-listed registries; generated
messages are queued-for-gateway — **live gateway submission is NOT
RUN and never claimed sent**. (The RXC component segment was cut:
no component data exists in the model to populate it.)

**BD-34 — External EQA/PT provider integration.** Governed
`integration/eqa-provider` interface: programme enrolment, result
return and scoring import verified on provider-format fixtures
(format id `opencare-eqaprovider-csv-v1`). Live provider exchange
is a deployer enrolment — NOT RUN, labelled in the config and here.

**BD-35 — DICOM pixel data.** Delivered as scope D below: DICOM
objects (synthetic fixtures generated in-test with pydicom) are
stored to the data platform's object tier with metadata indexing
and WADO-RS retrieval. Storage leg note, per the plan's own
caveat: the tier is the platform pass's filesystem ColdStore behind
the S3-shaped interface; the real S3/MinIO backing is BD-63
(Phase 17) and slips with it — the ingest → store → retrieve
round-trip is proven on the current tier.

## What was built — scopes B–H and the standing scope

**Scope B — Laboratory completion.** (1) Test Manager + Test Panel
Manager INSIDE the Laboratory module as in-panel tabs ("Test
Catalogue" + "Panels") in the management cluster alongside QC & EQA
(nav seed sections added in seed position, deep-linkable). The tabs
compose onto the governed `terminology/lab-catalog` DRAFT
(`GET/PUT /lab/manager/catalog|panels/draft`); publishing happens in
Studio; the write gate is `lab.work` AND (admin/configurator OR the
caller's job role equals the catalogue's published
`terminology/lab-governance` `directorJobRole`, seeded
`lab-director`). Panels are governed definitions — component tests,
reflex rules, specimen/container requirements — consumed by
ordering, benches, delta checks and auto-verify. (2) Specimen
lifecycle: derivation (aliquot/split) creates REAL FHIR Specimen
children with parent links and volume deduction from the parent;
subculture/isolate derivation in microbiology; add-on testing
against an existing specimen; each test's aliquot/derivation rules
live in the Test Manager catalogue and panel definitions state
required aliquots. (3) Barcoding + chain of custody: every derived
aliquot/split carries its OWN barcode (`OCSP1|key|kind-seq|check`,
format from governed config extending the Phase 11 barcode-standard
pattern; ISBT 128 governs the blood chain); every scan event
(collection → transport → receipt → bench transfer → storage →
send-out dispatch → disposal) appends a custody event (who/where/
when/action — vocabulary from `terminology/specimen-lifecycle`) to
that child's record; scanning any family member reconstructs the
full tree with per-member custody timelines; a custody BREAK flags
the chain to a worklist. The chain view renders on specimen detail
in the Laboratory module.

**Standing scope — oncology regimen depth + infection prevention.**
Oncology packs plus `app/infection_prevention.py`: device-associated
infection surveillance on the device-day spine, isolation
precautions, and SSI intake fed by scope E's post-op outcomes;
surfaces on the Safety page as an Infection prevention tab.

**Scope C — full barcode (Track G remainder).** Patient wristbands,
ISBT 128 blood-product labels and medication labels as governed
print artefacts (scope H carries the user-facing print actions);
the ISBT 128 donation identification number format is published
config (`integration/barcode-standards`, DIN pattern
`^=[A-Z0-9]{5}[0-9]{8}$`), validated at issue.

**Scope D — Radiology department.** New `app/radiology.py` (23
backend tests). RIS worklist with states ordered / scheduled /
arrived / in-progress / completed and priority incl. STAT. DICOM
ingest (`POST /radiology/dicom/ingest`, pydicom 3.0.2 — added to
`requirements.txt`) stores pixel bytes to the platform tiering
layer (ColdStore) with series/instance metadata indexed; WADO-RS
retrieval is hash-verified and QIDO-RS search is served under
`/radiology/dicomweb/…`. DICOM Modality Worklist is generated from
scheduled orders. Protocoling workflow with pre-exam screening as
governed forms (contrast / allergy / eGFR / pregnancy) — screening
gates flip-proven. Radiation dose records per study and per patient.
Structured report templates as governed forms with report ADDENDA
(the original report transitions to FHIR `corrected`; nothing is
silently overwritten). Critical-result closed loop with mandatory
read-back on the lab critical-value precedent, plus a seeded
PUBLISHED escalation rule `radiology-critical-escalation` (SLA
30 minutes, event `radiology.critical.overdue`). TAT and worklist
dashboards as dashboard config (`dashboard/radiology-operations`).
Governed configs: `workflow/radiology-flow`,
`terminology/radiology-ops`, `terminology/imaging-protocols`. Seed:
three synthetic studies WITH pixel fixtures, created only when the
seeded patients pat-1/2/3 exist, so a fresh database shows a living
department. Web: Radiology page with TAT tiles and tabs
Worklist | Critical | Dose; screening via the governed forms +
shared FormRenderer; the viewer remains the boards `viewer_url`
labelled external (the honest pattern — no fake embed).
NOT BUILT (recorded): a DICOM ingest UI and an MWL viewer page —
the backend endpoints and tests exist.

**Scope E — Surgery department depth.** New `app/surgery.py`
(12 backend tests) on its own `surgery_*` tables: case requests +
surgical waitlist; surgeon block scheduling with OR utilisation
reporting; preference cards (per surgeon/procedure supply + implant
picks, lot-tracked on the pharmacy inventory/DSCSA patterns);
sterile processing (instrument trays, sterilisation cycles with
shelf-life from published `sterile-processing` config — 30 days —
case linkage); intra-operative record depth as governed forms
(counts, specimens, positioning); PACU scoring with Aldrete-style
criteria as published config (`terminology/pacu-scoring`: discharge
threshold 9, must-pass respiration and O₂ criteria) GATING the
pacu→done transition — the gate lives in `boards.transition_case`
(gate order: surgical-safety checklist first, then PACU) and is
controlled by `workflow/surgery-flow` `pacuScoreGate: true`
(governed-appended this phase; republish-off restores free
transitions, flip-proven). Post-op outcomes + surgical-site-
infection tracking feed infection prevention (standing scope).
Governed configs: `terminology/surgery-urgency`, `or-blocks`,
`instrument-trays`, `sterile-processing`, `pacu-scoring`,
`surgery-complications`. Web: Surgery page tabs Board | Requests |
Blocks | Preference cards | Sterile | Outcomes, with the PACU
panel on case detail.

**Scope F — UX: dropdown management + master-detail.** (1) Every
selection list is backed by governed config: the published registry
`terminology/manageable-lists` now carries 28 real lists (26 at
seed + `diet-types` + `languages` added when their in-context
mounts landed; `terminology/lab-catalog` and the RFID registry are
excluded WITH REASONS recorded in the registry — they have their
own governed surfaces). The shared `ListManager` component renders
an in-context Manage affordance ONLY when the server computes the
caller may manage that list (admin/configurator + the list's
department-manager job role; the component renders NULL otherwise —
the client never decides). Adds/updates compose onto the governed
DRAFT through `configstore.validate_payload`; removing a published
code is refused 422 (deactivate instead — published values are never
hard-deleted); a human publishes for changes to take effect; all
changes are hash-audit-chained. Flip-proven end to end on radiology
modalities. Mounted in context: RadiologyPage modality filter,
SupportPage diet-types and languages lists. (2) Shared
`MasterDetail` component (list/grid + detail pane, coordinator-
appended two-pane CSS) adopted on PatientsPage and WorkforcePage;
the Inbox, Radiology, Lab and Surgery pages use brief-sanctioned
inline two-pane layouts of the same grammar (the shared component
landed after those pages were built; retro-fitting them is
recorded as follow-up, not silently claimed).

**Scope G — Results Inbox rebuild.** New `app/results_inbox.py`
(13 backend tests) replacing the single-patient lookup: a
cross-patient worklist (`GET /inbox/results`) of RELEASED results
only (released-ness via the store's unreleased-tag check; the
abnormal/critical flag is derived ONLY from stored data, never
recomputed), newest and most severe first. Acknowledgement is real
data: an `ResultAcknowledgement` row per result per reviewing
clinician, idempotent (exactly-once state, test-proven), one
hash-chained `inbox.acknowledge` audit per acknowledgement. Detail
carries values with reference ranges, report text, patient +
encounter context and trend where data exists, with a 1-click jump
to the chart. Actions: acknowledge, follow-up Task, message the
patient (through the Phase 9 engagement path), escalate.
Unacknowledged CRITICAL results past the SLA in published
`integration/inbox` (`slaHours: 4`, `pageSize: 50`,
`escalationAudience: charge-nurse`) emit
`inbox.critical.unacknowledged` into the Phase 10 rules engine; the
seeded PUBLISHED rule `inbox-critical-sla-escalation` fires on it.
Personal scoping — mine (attending extension) / unit
(`continuum._admission_unit`) / all for authorised roles; the
`fhir.inbox` capability remains the gate. Cross-link: `GET
/inbox/summary` calls the Phase 12 continuum async inbox itself,
so the Results page shows a summary strip of the tasks/messages
inbox and links to it. Seed: guarded demo results (only when pat-1
AND pat-2 exist) with fixed ids `inbox-demo-*`, including one
CRITICAL unacknowledged potassium (HH) on pat-2 — the inbox is
alive on first run. Web: InboxPage rewritten with tabs
Results | Messages (the Phase 9 message threads preserved verbatim
under Messages), summary strip, scope switcher mine/unit/all, and
an inline two-pane detail.

**Scope H — barcode visibility + RFID.** (a) Label PRINTING through
the Phase 13 print machinery (published `integration/print`
templates, label-sized pages via ReportLab): "Print wristband" on
the patient header/chart, ISBT label printing from blood-bank rows,
"Print med label" in Pharmacy (Dispense & stock tab, from the last
dispense/BCMA state), aliquot labels from specimen detail.
(b) Universal lookup: one barcode lookup surface resolves ANY
barcode — wristband, specimen/aliquot (with the full family chain
and custody history), blood product, medication — and shows what
it is, its chain and its history; scan events land as real events
on the custody spine (specimens) and BCMA (medications).
(c) Seeded demo barcodes across wristband/specimen/blood/med so a
fresh DB demonstrates the feature. **RFID (new workstream)** on the
Phase 14 device spine — `terminology/rfid` (tag types: asset,
patient wristband, medication, inventory) + `integration/rfid-readers`
(reader registry, zone map) as published Studio config; a tag
registry binds EPC/tag id → entity (device/patient/lot) with
bind/unbind actions for authorised roles; reader events ingest
through the existing device-ingestion contract (reader id, tag id,
timestamp, signal/zone) and append to the bound entity's history —
asset location trail on the device registry, wristband sightings
on the patient record; asset find/last-seen on the Devices page
with an RFID section (nav deep-link). Mismatch alerts ride the
rules engine: the seeded PUBLISHED rule `rfid-zone-mismatch` fires
on `rfid.zone.mismatch` (asset seen in a zone it is not assigned
to → Task). **HONEST LIMIT: no physical RFID hardware exists in
this environment** — the feed is labelled `source_label:
"simulated"` everywhere it appears; real reader integration is
site work, NOT RUN (below).

**Payer care management (standing scope).** New
`app/care_management.py` (13 tests): published
`terminology/care-programmes` (diabetes-care 30 d, heart-failure
14 d, COPD 21 d cadences); programme attributions validated against
the published `terminology/payers`; enrolments name the care
manager explicitly (directory-validated username, never defaulted);
outreach cadence enforcement with a config-flip proof; care gaps
are a FULL REUSE of the continuum engine's gap computation
(`continuum._gap_items`) — no second gap logic. Recorded decision:
the config's `careManagerRole` is descriptive; enforcement matches
the named manager, not the role string.

## Configuration-coverage list (Studio surface per item)

Standing rule: an item with no Studio surface is incomplete. Every
Phase 15 item names its surface:

- BD-03: the seven packs themselves (manifests, order sets,
  registries, flowsheets, monitoring rules, geriatrics' governed
  `rule` config) — all versioned pack/config data.
- BD-04: governed forms `icu-acuity` / `icu-devices` / `icu-handoff`;
  ICU unit types in `terminology/locations`.
- BD-05/BD-06/BD-08: the transplant, dialysis and paediatrics packs;
  dialysis slot types via republish of `terminology/self-scheduling`;
  paediatric beds via republish of `terminology/locations`.
- BD-07: therapy plans ride Phase 7 authorisation config; no new
  surface needed beyond the covering-auth data.
- BD-09: `terminology/blood-bank` (`lookback.retentionYears`,
  `retain_until` computation).
- BD-10: `terminology/lab-competency` (six CLIA elements, currency
  windows).
- BD-11: Phase 14 device registry + published device ranges;
  HaH bindings reference registry devices only.
- BD-12: Phase 12 transport config; detail fields are workflow data.
- BD-13: `terminology/command-desks` (desks, shift staffing,
  `triageAuthorityRole`, huddle cadence).
- BD-14: access-line config on the command-centre surface; referral
  sources are a manageable list (scope F registry).
- BD-15: Phase 12 safety config (harm taxonomy) + CUSP workspace
  artefacts as governed forms.
- BD-16: HICS activation PUBLISHES a new governed version of
  `integration/downtime`; incident roles are config.
- BD-17: supplies catalogue + PAR levels as governed data;
  exchange labelled `basis: "file-fixture"` in `integration` config.
- BD-18: `terminology/diet-types` (also ListManager-manageable in
  context).
- BD-19: `terminology/languages` + interpreter modality config;
  video template = published `integration/telehealth`.
- BD-20: survey instrument as a governed form pack; distribution
  and suppression thresholds in quality config.
- BD-21: `terminology/ur-criteria` — SITE-FIXTURE examples only,
  labelled everywhere criteria are shown.
- BD-22: `integration/payment-provider` (test mode enforced in
  code: non-test config refused 409).
- BD-23: `terminology/proxy-scopes`.
- BD-24/BD-25: coordinator + episode data ride the attending
  extension and panel config; no forked surfaces.
- BD-26: duty-hour limits as published config (80/24/8).
- BD-27: `terminology/econsult-triggers` (seeded diabetes rule:
  LOINC 4548-4 gt 9.0) + the Phase 10 rules engine.
- BD-28: `integration/telehealth-services` layered over the Phase 9
  `integration/telehealth`; outcome capture = governed
  `form/telehealth-outcome`.
- BD-29: `terminology/activity-costs`.
- BD-30: dashboards remain governed `dashboard` config (v2
  rewiring keeps `sourceEndpoint`); the trace endpoint is the
  migration ledger; auto-seeding decision recorded (explicit seed,
  no auto-seed).
- BD-31: privilege names ride Phase 13 credential config; the
  check reads published credential records at booking time.
- BD-32: report definitions remain governed `report` config; the
  designer composes drafts only — publish stays a human Studio act.
- BD-33: CVX codes in published terminology; registry list in
  `integration` config (VXU queued-for-gateway).
- BD-34: `integration/eqa-provider` (fixture format id
  `opencare-eqaprovider-csv-v1`, live exchange labelled off).
- BD-35 / scope D: `workflow/radiology-flow`,
  `terminology/radiology-ops`, `terminology/imaging-protocols`,
  dose + TAT in `dashboard/radiology-operations`, escalation rule
  `radiology-critical-escalation`.
- Scope B: `terminology/lab-catalog` (draft-compose, Studio
  publish), `terminology/lab-governance` (`directorJobRole`),
  `terminology/specimen-lifecycle` (custody vocabulary), barcode
  payload format config.
- Scope C/H: `integration/barcode-standards` (ISBT 128 DIN),
  `integration/print` templates; RFID: `terminology/rfid`,
  `integration/rfid-readers` (reader registry + zone map), rule
  `rfid-zone-mismatch`.
- Scope E: `terminology/surgery-urgency`, `or-blocks`,
  `instrument-trays`, `sterile-processing`, `pacu-scoring`,
  `surgery-complications`; gate flag `workflow/surgery-flow`
  `pacuScoreGate`.
- Scope F: `terminology/manageable-lists` registry (28 entries;
  exclusions carry reasons); every managed list is itself governed
  terminology/config.
- Scope G: `integration/inbox` (SLA, page size, escalation
  audience) + published rule `inbox-critical-sla-escalation`.
- Care management: `terminology/care-programmes`,
  `terminology/payers`.
- New capabilities this phase: `support.read/work`, `coord.read/work`,
  `ur.read/work`, `imm.read/work`, `lists.manage`, `rfid.read/manage`,
  `barcode.read/manage`, `ip.read/work` (server defaults + web
  mirror). New nav module: `support` (Support services) — 25 modules
  total; module-only governance unchanged.

## Compliance-coverage list (regulation is configuration)

- **CLIA 42 CFR 493.1235 (competency)** — BD-10: the six competency
  elements as lab-owned records per testing person/test system,
  currency computed, expiry on the worklist.
- **21 CFR 606.160 (blood records / retention)** — BD-09: lookback
  traceability retained ≥ 10 years via published
  `terminology/blood-bank` `lookback.retentionYears`; deviation
  reports generated as artefacts.
- **FDA biological product deviations (21 CFR 606.171 pattern)** —
  BD-09 deviation report PDF through the governed report engine.
- **EMTALA / transfer workflow** — BD-14 access-line dispositions
  join the Phase 12 transfer centre (same record, same audit); no
  parallel transfer path exists.
- **HIPAA audit controls** — every new write path is hash-chained:
  inbox acknowledgements, proxy reads (`on_behalf_of`), list
  management, credential-gated booking refusals, HICS activation,
  triage decisions (authority recorded).
- **Advice vs referral boundary** — BD-27 eConsults are advice-only
  completed units of work; automatic initiation cites its
  triggering governed rule in the audit; no eConsult creates a
  visit.
- **Licensed-content boundary** — BD-21 criteria are site fixtures,
  labelled; InterQual/MCG-class content is never shipped or
  invented. Drug-interaction checking remains INERT by the standing
  evaluated decision (no redistributable open source); unchanged
  this phase.
- **CMS/Joint Commission patterns** — BD-13 huddle artefacts and
  BD-26 duty-hour limits (80/24/8) are config, so a site under a
  different regime republishes rather than forks.
- **Immunisation reporting** — BD-33 VXU^V04 generation conforms to
  the HL7 spine; submission is queued-for-gateway only (live
  gateway = external dependency, NOT RUN).
- **EQA/PT (CLIA proficiency testing pattern)** — BD-34 fixture
  exchange round-trip; live provider exchange is a deployer
  enrolment (NOT RUN).

## Adjusted-tests register (none weakened)

Every adjustment below is additive or a snapshot update for a
deliberate, SQA-recorded behaviour change — the established project
precedent. No assertion was deleted or loosened.

1. `test_phase10_rules.py` — the exact rule-event registry set gains
   the three Phase 15 events (`rfid.zone.mismatch`,
   `radiology.critical.overdue`, `inbox.critical.unacknowledged`),
   with a precedent-style comment. The set remains exact.
2. `test_phase4b_boards.py::test_surgery_flow_and_board_grouping` —
   records a passing PACU score (total 9 = the published threshold)
   before the pacu→done transition, because that transition is now
   gated by published config (scope E). All original assertions
   stand.
3. `test_depth_journeys.py::test_safety_gate_blocks_inop_and_done_until_sections_recorded`
   — additionally asserts the PACU 409 (naming "PACU") after
   sign-out, then records a passing score before done-200. The
   original sign-out assertion is untouched; gate order in
   `boards.py` is safety → PACU, verified by reading the code. The
   flip-off publish in the same area also sets
   `pacuScoreGate: false` so the off state restores free
   transitions.
4. `test_depth_nav.py` — seeded-group/section snapshots gain the
   deliberate seed changes: `support: Operations` group entry; Lab
   sections + Test Catalogue/Panels/Specimens (seed position);
   Command sections Desks/Access line; Devices section RFID; the
   no-sections loop exemption extended to command/devices. Both
   seeded tests assert the new constants.
5. `test_phase10_studio.py::test_operations_overview_dashboard_seeded_published`
   — now asserts the three widgets rewired by BD-30 resolve through
   `/indicators/…/value` AND that each widget's `sourceEndpoint`
   preserves its original endpoint. Original intent (seeded
   published dashboard, working widgets) kept and strengthened.
6. `test_realignment.py` — module count 23 → 24 → 25 across the
   phase (support module) and the ordered module list gains
   `support` after `safety`, with a Phase 15 comment.
7. `test_nav_seed_consistency.py` — `MODULE_GATES` transcription
   gains `support: support.read`.
8. Web `session.test.ts` — all four role arrays gain the support
   page per its capability gate.
9. Web `runtimeConfig.test.tsx` — the SEED_NAV fixture gains the
   support module at order 18 with orders 19–25 renumbered (fixture
   mirrors the deliberate seed change).
10. Coordinator build fixes (not test changes): three TypeScript
    errors that vitest/esbuild never surfaced were fixed at the
    production build — `api/rfid.ts` unified `SpecimenFamily`
    interface (the backend returns the same family object under two
    keys), two type annotations in `SupportPage.tsx`, one cast in
    `AccessServices.test.tsx`. The build is the type gate; it is
    green on the final tree.

## Incidents

1. **/tmp filled WITHIN a single half-suite run.** At 75 backend
   test files, pytest's retained basetemp now exceeds the 512 MB
   tmpfs before a half finishes: one gate attempt produced 210
   passed / 4 failed / **247 errors, root cause
   `sqlite3.OperationalError: database or disk is full`** — every
   error was a fixture-setup disk failure (the inbox "failure" in
   that run was DDL on a full disk, not a code defect). The gate
   moved to ~9–10-file chunks with per-chunk private `--basetemp`
   directories deleted after each chunk; the chunked run is the
   gate of record.
2. **Two runtime restarts killed long gate runs** (at ~2.5 min and
   ~36 min). Both outcomes were treated as UNKNOWN, discarded, and
   never assumed — the runs were repeated from scratch.
3. **A disk-backed (workspace-fs) basetemp attempt ran ~15 % in
   36 minutes** and was abandoned for chunked tmpfs.
4. **Parallel workers pruned each other's basetemps.** Early in the
   build, workers sharing `/tmp/pytest-of-root` destroyed each
   other's ACTIVE runs (sqlite readonly / FileNotFound errors that
   looked like code failures). Workers converged on private
   `--basetemp` dirs; several worker suites took 250–570 s under
   sibling contention (the care-management worker's pytest log is
   kept as evidence at `~/workspace/phase15_work/caremgmt_pytest.log`).
5. **The ListManager `key` prop discovery.** The F worker proved
   empirically that React 19 strips the JSX `key` prop, so the
   component's list identifier prop is named `listKey`. Ratified by
   the coordinator; renamed in component, tests and docs.
6. **A false duplicate-router claim, checked and rejected.** The
   surgery worker reported its router might be double-included;
   the coordinator verified `main.py` includes it exactly once
   (line 407). Recorded so the claim is not repeated as fact.
7. **One coordinator edit briefly contained a typo** (`} wanting>`)
   during the build type-fix round; it was corrected and
   grep-verified absent BEFORE the passing build ran. No gate
   result rests on the typo'd tree.

## Explicit NOT RUN list (never claimed, never passed)

- **Live VXU gateway submission** (BD-33) — messages are generated
  and queued-for-gateway; no registry gateway exists here.
- **Live EQA/PT provider exchange** (BD-34) — fixture round-trip
  only; provider enrolment is the deployer's.
- **Live payment processing** (BD-22) — test mode only, enforced
  in code (non-test config refused 409).
- **Licensed UR criteria content** (BD-21) — site fixtures only,
  by design and by licence.
- **Real RFID readers** — registry, ingestion contract, event
  spine and UI are built; the feed is simulated and labelled.
  Real reader integration is site work.
- **Real DICOM modality ingest** — synthetic pydicom fixtures only;
  no modality is connected.
- **Real S3/MinIO cold-tier backing** (BD-63, Phase 17) — BD-35
  rides the filesystem ColdStore behind the S3-shaped interface.
- **DICOM ingest UI + MWL viewer page** — backend endpoints and
  tests exist; the pages were not built this phase.
- **Dedicated ICU and therapy web pages** (BD-04/BD-07) — backends
  are evidenced; ICU forms ride the documentation machinery.
- **Report-designer validate-only endpoint + draft-preview run**
  (BD-32) — no new backend endpoints were opened in Phase 15 for
  these; both gaps are labelled in the UI.
- **Systematic browser click-through E2E** — VM-blocked (standing
  limitation); Min's own click-through on the Air is the live
  substitute and is in progress.
- Standing list unchanged: Docker runtime (YAML-validated only),
  live Keycloak OIDC/MFA, live Epic SMART launch, live LLM run (no
  model endpoint anywhere), live TLS serving, pack-gate default-ON
  (Phase 17), X12 clearinghouse + Surescripts + live payer EDI,
  real physical devices / external MQTT broker / ASTM analyser
  links, pipeline scheduler daemon, MySQL/MSSQL/Oracle live
  adapters, live MCP/A2A (Phase 16), mobile compilation,
  drug-interaction checking (INERT by evaluated decision).

## Named partials (graded honestly, not rounded up)

- **BD-08** — weight-based (mg/kg) dose checking not expressible in
  the current monitoring vocabulary; not claimed. Age-banded vitals
  and paediatric capacity are delivered.
- **BD-30** — widget-to-indicator TRACE complete for all seeded
  dashboards; rewiring partial (9/41 widgets) with per-widget
  named reasons in the trace.
- **Formulary/monitoring singleton pattern** (pre-existing, surfaced
  by BD-03/BD-05/BD-06/BD-08): `terminology/formulary` and
  `medication-monitoring` are singletons — when several packs are
  enabled, the last-enabled pack's version wins. Recorded for a
  future multi-pack merge design; not changed this phase.
- **MasterDetail retro-fit** — the shared component is live on
  Patients/Workforce; Inbox/Radiology/Lab/Surgery use the same
  two-pane grammar inline (brief-sanctioned). Retro-fitting the
  shared component into those four pages is follow-up.
- **BD-09 trace scope** — lookback covers same-order components and
  same-donation-attribute units (the linkage paths the data model
  carries); recorded in the BD-09 entry above.

---

## Parent re-verification addendum (2026-10-18, parent-run)

The parent re-ran the full gate independently on the final tree: backend as 8 sequential alphabetical chunks with per-chunk private basetemps (the half-suite method no longer fits the 512 MB /tmp at 76 test files — the chunked method is now the gate of record), then web, build, ADK, Go and Cargo, strictly sequential. Artefacts: `~/workspace/p15gate/` (summary.txt, chunk_00–07.log, web/build/adk/go/cargo logs).

| Suite | Parent result |
|---|---|
| Backend `pytest tests/` (8 chunks) | **897 passed, 4 skipped, 0 failed** — identical totals to the coordinator's claim |
| Web vitest | **389 passed** |
| Web `npm run build` | **exit 0** |
| ADK | **8 passed** |
| Go `go test ./...` | **ok (exit 0)** |
| Cargo `cargo test` | **33 passed** |

**Watch item resolved:** the coordinator's working chunk log (gate_c07) recorded one failure — `tests/test_realignment.py::test_navigation_and_branding_seeded_as_published_config` — against the headline "0 failed". The parent's chunk_07 (which contains test_realignment.py) passed in full (114 passed, exit 0) on the final tree, and the parent totals match the coordinator's final numbers exactly. The failure is therefore attributable to a mid-gate tree state superseded before the coordinator's final runs; it does not reproduce on the delivered tree. Recorded here so the discrepancy is explained, not buried.

Named partials stand as reported: BD-30 rewiring 9/41 widgets with per-widget reasons; BD-08 weight-based mg/kg dosing not expressible in the monitoring vocabulary and NOT claimed; DICOM ingest UI + MWL viewer page not built (backends + tests exist); RFID feed simulated and labelled, real readers NOT RUN. A runtime restart killed the parent's first gate attempt after chunk_00 (142 passed); the gate was resumed from chunk_01 and completed — outcomes of killed runs were treated as unknown and discarded, per standing discipline.

**Phase 15 is parent-verified.**
