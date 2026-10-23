# OpenCare — Phase 10+ Roadmap: From Breadth to Operational Depth

_Drafted 2026-10-17 evening, from Min's depth verdict after the first real
run on the Air: "every feature is very minimum" — modules exist and their
core safety paths work, but no department could actually operate in the
system yet. This roadmap is the plan to close that gap. Verdict on track
order is Min's; the proposed sequence is at the end._

## Standing principles (unchanged, they govern every track)

1. **Configure, don't fork.** Every difference between sites, specialties,
   units, and roles is Studio configuration data — never a code branch.
   If a requirement cannot be met by configuration, extend the config
   surface. Every phase SQA carries a configuration-coverage list.
2. **Agents draft, humans act.** AI agents summarise, draft, forecast, and
   recommend. They never sign, dispense, discharge, or send on their own.
   No model available = the capability is honestly off, never faked.
3. **Governance is not optional.** New capability ships as Studio-managed
   configuration: draft → approve → attach → enable; published is
   immutable; hash-chained audit on everything.
4. **SQA gate per phase, including a real-browser pass.** The 2026-10-17
   fetch bug (native fetch invoked with the wrong receiver; every
   real-browser request dead while 525 automated tests stayed green) is
   the standing lesson: no phase is done until a scripted day-in-the-life
   scenario suite passes end-to-end AND a browser click-through runs.
5. **Synthetic data only.** No real PHI anywhere in development or SQA.
6. **Regulation is configuration.** How the system operates must be
   aligned with the regulatory bodies that govern each workflow.
   Requirements land as governed Studio packs (Track J) that
   parameterise workflows — timers, required documentation, consent
   models, retention — per jurisdiction. Every phase SQA from Phase 10
   carries a compliance-coverage list next to its configuration list.

## Reference operating model — how the reference institutions run

_Re-anchored 2026-10-17 from a sourced research pass over the
institutions named at this project's start (full report in the
research notes; figures are as published by the institutions/vendors,
not audited)._

- **Cleveland Clinic organises by institute, not department.** Care is
  grouped around diseases/organs (Heart, Vascular & Thoracic unites
  cardiology with cardiac surgery; the Integrated Hospital Care
  Institute groups the hospital-platform functions — hospital medicine,
  critical care, emergency medicine, anaesthesiology). Consequence for
  this roadmap: Tracks C–D organise operational depth by **service
  line/institute** (our 11 specialty packs are the seeds), with
  departments as work locations inside them — not the other way round.
- **Johns Hopkins runs a Capacity Command Center.** A physical,
  staffed 24/7 operations centre (bed management nurses, a transfer
  line taking ~700 transfers/month, transport dispatch, analytics)
  watches census, flow, and transfers across hospitals and acts on
  them. Consequence: the command centre is a first-class product
  surface in Track D, not a dashboard afterthought.
- **Johns Hopkins' Armstrong Institute runs safety as a system:**
  enterprise event reporting (their "Hero" system, ML-triaged,
  confidential, two-way reporter↔reviewer) and unit-based safety
  teams (CUSP). We have quality *measures* but no safety *event*
  operations — a genuine gap, added to Track D.
- **Kaiser's operations are panel operations.** Prepaid, integrated
  payer-provider: the work is keeping a defined panel healthy — a
  panel-support tool surfacing guideline care gaps, proactive
  encounters that close gaps opportunistically at any visit, and
  condition programmes at population scale (their Complete Care /
  PHASE patterns). Consequence: Track D outpatient/aftercare and the
  pophealth engine are one discipline — care-gap workqueues, not
  reports.
- **Mayo Clinic:** salaried group practice with physician–
  administrator dyad leadership (a governance pattern, mirrored in our
  approve/attach roles), **Mayo Clinic Platform** treating the data
  platform as a product (validates Tracks F/H/I), and **Advanced Care
  at Home** — a command centre plus in-home teams delivering hospital-
  level care at home (>5,500 patients published). Hospital-at-home is
  added to Track D aftercare.
- **Epic's deployed landscape (our implicit template) was verified
  module by module.** Our build parallels it almost everywhere; three
  genuine gaps surfaced: **infection prevention** operations (Epic's
  Bugsy), **oncology regimen depth** (Beacon), and **payer/care-
  management operations** (Tapestry) beyond our referral/eligibility
  layer. All three join the register/Track C remainder.
- **Support services are operations too** (research §7): supply chain
  (exchange + ERP), EVS integrated with bed management, patient
  transport dispatch, nutrition/diet office, clinical engineering
  (HTM — device maintenance, ties directly to Track H's registry),
  utilization review, patient-experience programmes, interpreter
  services, and at academic centres a CTMS for trials and GME
  operations. Track C gains a support-services block.

## Track A — AI agent foundation (Studio-managed multimodal ADK)

Port the pattern proven in Agentic_App's System Studio into OpenCare's
Studio, as a service beside the FastAPI core:

- ADK service (Google ADK + LiteLLM), **local-first**: Ollama on the
  operator's hardware is the default model route; cloud models are
  opt-in configuration, never a requirement.
- An **agent is a config kind**: model route, system prompt, tool
  allow-list (FHIR reads, module queries, document drafting), input
  modalities (text now; image/audio gated by config and model support).
- Lifecycle = the Studio lifecycle: install/create as draft, separate
  approve → attach → enable; every run writes to the hash-chained audit
  (agent, version, inputs hash, output, human disposition).
- Invocation surfaces: in-chart assistant panel, per-module "draft with
  agent" actions, and scheduled batch agents (nightly worklist prep) —
  all capability-gated by role.
- First agents (each lands with the track that uses it): pharmacy DUR
  reviewer, lab result summariser, radiology impression drafter,
  note/coding assistant, ER boarding forecaster, discharge-readiness
  summariser, aftercare call-prep drafter, schedule optimiser.
- **The competitive bar (2026 landscape check of the other major
  systems):** every major vendor now ships ambient AI documentation —
  natively or through a governed marketplace of third-party scribes —
  and Oracle's clinical agent drafts orders, not just notes; MEDITECH
  adds ambient capture for nursing and an AI search spanning structured
  data, free text, scanned documents, handwriting and faxes. So the
  first wave explicitly adds an **ambient documentation agent**
  (encounter audio → draft note for clinician sign-off; orders drafted,
  never placed) and a **chart-search agent** over all artifact types.
  The ADK tab's pack model is also our answer to the marketplace
  pattern: third-party agent packs install as draft and enable only
  after approval, exactly like clinical packs. No open-source system
  ships any of this today — that gap is OpenCare's opening.

## Track B — System Studio parity with Agentic_App (clinically adapted)

OpenCare's Studio today: packs, config editors, interfaces, audit.
Agentic_App's Studio adds what is missing here:

- Visual designers: pages, forms (extends today's FormRenderer path),
  workflows (state machines with per-transition roles, as data),
  dashboards (see Track E — dashboards are configured, not coded).
- Rules/automation engine: declarative rules over module events
  (extends the workflow-notification pattern from the realignment).
- Pack lifecycle completion: module states, version diff/rollback UI,
  per-site pack overlays.
- ADK tab (Track A's management surface) inside Studio.
- **Deliberate difference from Agentic_App:** no arbitrary code
  execution (scripts/SQL/terminal) inside the clinical runtime.
  Equivalent flexibility via sandboxed expressions and governed rules.
  Clinical safety sets the blast radius.

## Track C — Department-grade depth (pharmacy first, as exemplar)

Each department gets an operations build: the queues, stock, exceptions,
and end-of-day work a real department runs. Pharmacy (named by Min):

- Inventory & procurement: stock by location, lots/expiry, PAR levels,
  receiving, returns, reorder proposals, stock-take.
- Controlled substances: vault counts, witness flows, discrepancy
  workqueue, perpetual log (audited, hash-chained).
- Sterile compounding / IV room: batch records, beyond-use dating,
  double-check sign-off, label print (Track G).
- Dispensing operations: prioritised verification queues (STAT/now/
  routine), workload + turnaround dashboards, ADC/cabinet restock lists,
  refill and med-rec queues.
- Clinical pharmacy: renal/anticoagulation dosing support lists,
  antimicrobial stewardship worklist, TPN review queue.
- Then the same pattern: laboratory (builds on 6B), radiology,
  HIM/revenue (builds on Phase 7), materials/supply chain.
- **Service-line organisation (Cleveland Clinic lesson):** the
  specialty packs from Phase 9 are reframed as institutes/service
  lines with their own operational dashboards and accountability
  (Track E consumes this); a department build is scoped as "the
  service line's operations", not a screen for a room.
- **Support services block** (research §7 — the operations EHR plans
  commonly miss): supply chain & materials (exchange ordering,
  preference cards for OR/cath, PAR replenishment with Track G
  scanning), EVS workflows integrated with bed turnaround,
  patient-transport dispatch, nutrition/diet office (orders, allergies
  from the chart, tray tracking), clinical engineering/HTM (device
  work orders and maintenance — the operational twin of Track H's
  device registry), utilization review queues, patient-experience
  programme data (survey instruments as governed forms), interpreter
  services booking, and at academic sites CTMS trial operations
  (with the Track J GCP pack) and GME scheduling.

## Track D — Care-continuum operations

ER → inpatient → discharge → aftercare as one journey; outpatient as
the parallel front door:

- **ER ops:** ESI triage + reassessment timers, split-flow/fast-track,
  waiting-room and boarding queues, door-to-provider and LOS clocks,
  trauma/stroke/STEMI activation timers, ambulance intake/diversion,
  LWBS tracking, surge levels, results-callback queue for discharged
  patients.
- **Inpatient ops:** acuity-based nurse assignments, rounds lists,
  structured handoffs, rapid-response/code documentation, falls and
  pressure-injury workqueues, discharge planning from admission
  (expected date, barrier list, case-management tasks), EVS bed
  turnaround, transport requests, isolation cohorting, a live
  discharge-readiness board.
- **Outpatient ops:** session templates + overbooking rules, arrival/
  rooming/vitals queues, no-show management + backfill, registry-driven
  recall campaigns, referral loop-closure tracking, async inbox
  (messages/refills/results) with service-level timers.
- **Aftercare:** discharge instructions with teach-back sign-off,
  follow-ups booked before departure, 48–72h post-discharge call queue,
  portal wound/device check-ins, home-health order tracking,
  readmission-risk worklist, wired into the specialty pathways
  (post-surgical, oncology survivorship, rehab). Plus **hospital-at-
  home** (Mayo's Advanced Care at Home pattern): eligibility screening,
  a virtual command roster for the home census, in-home visit/paramedic
  tasks, remote-monitoring feeds (Track H), and escalation-to-admit
  workflows — aftercare at hospital intensity, not just phone calls.
- **Capacity Command Center (the capstone, Johns Hopkins pattern):**
  a staffed-operations surface over everything above — real-time
  census and predicted census, bed-request queue with 24/7 bed-manager
  workflow, a **transfer center** (incoming transfer requests, accept/
  decline with reason, ~the single biggest flow lever at the reference
  centres), transport dispatch status, ED boarding pressure, discharge-
  before-noon tracking, and surge/diversion state. The command centre
  consumes Track E staffing and Track I pipeline freshness; it is a
  Phase 12 deliverable in its own right, not a dashboard theme.
- **Safety & quality operations (Armstrong Institute pattern):** an
  event-reporting system — confidential reporting, ML-triaged intake
  (Track A agent as triage assistant, human-owned), reviewer↔reporter
  two-way follow-up, harm classification, root-cause-analysis
  workqueues, and unit-based safety team dashboards (CUSP). Quality
  measures (Phase 9 engine) say how the system performs; this is how
  it learns. Feeds Track J evidence directly.

## Track E — People: staff, units, and management

- Workforce core: staff directory (doctors, nurses, allied health,
  support, management) as governed data — roles, credentials/licence
  expiries, unit membership, shift rosters, on-call schedules.
- **Dashboards per audience, configured in Studio (Track B), not coded:**
  clinician (my patients, my results, my day), nurse (unit census,
  tasks, MAR due), unit manager (staffing vs acuity, throughput,
  quality flags), department head, executive/management (cross-unit
  KPIs, capacity, finance rollups).
- Unit/group analytics: every unit and group gets its own dashboard +
  analytics view; measures reuse the quality/pophealth engine; small
  numbers suppressed as today.

## Track F — Data platform: visualisation, reporting, documents

- Visualisation: chart components over the existing analytics/query
  APIs; dashboard tiles are config (Track B/E).
- Reporting: a report catalogue (clinical, operational, financial),
  each report a governed definition with parameters; run in-browser,
  **print** (server-side PDF, open-source stack), or schedule.
- Documents: printable **invoices, receipts, patient statements**,
  superbills, labels and wristbands (Track G) — templates are Studio
  config; every print is audit-logged.
- Export/import: CSV/XLSX export from any list/report (capability-gated
  and audited — PHI rules apply); CSV/XLSX **import** with a mapping
  profile, validation pass, and an error report before anything commits
  (all-or-nothing, like the 835 poster).

## Track G — Barcode & identification (cross-cutting platform)

- Code generation + print: patient wristbands (at registration/ADT),
  specimen labels, medication/unit-dose labels, blood-unit labels
  (ISBT 128-compatible fields, building on 6B), inventory and asset tags.
- Scanning workflows: wristband scan to open/verify the chart at any
  workstation; **barcode medication administration (BCMA)** at the MAR —
  scan wristband + scan medication, five-rights check against the order,
  hard stop on mismatch, administration recorded; specimen scan at
  collection and lab receiving (feeds the existing chain-of-custody);
  blood-unit scan at issue and transfusion start; inventory scans for
  receiving/stock-take; equipment scans for location tracking.
- Input hardware: USB wedge scanners work with the web app immediately;
  phone-camera scanning arrives with the native mobile apps (their
  first real job in the product).

## Track H — Devices, IoT, acquisition, and the data lake

The system today has a modelled warehouse (dbt + pophealth) but no raw
data lake, no streaming acquisition, and no live device layer — the lab
instrument registry (6B) tracks devices but nothing acquires from them.

- **Device & instrument integration:** a general device registry
  (extends the 6B instrument registry) — analysers, bedside monitors,
  infusion pumps, storage fridges/freezers, RTLS tags. Protocol
  gateways: ASTM and HL7 ORU over the existing MLLP listener (Phase 8),
  FHIR Observation ingestion, MQTT for IoT sensors. A device whose
  calibration/maintenance is overdue cannot release results (extends
  the 6B QC gating). **Honesty rule:** in development every feed is
  simulated/recorded and labelled as such — no gateway claims a live
  device link it does not have.
- **Monitoring & alerting:** vital-sign streams land on flowsheets;
  threshold breaches enter the existing critical-value workflow
  (read-back, audited). Fridge/freezer temperature monitoring with
  excursion alerts for pharmacy, blood bank, and lab storage. Pump
  status and RTLS location events feed the bed/asset boards
  (Tracks D/G) and aftercare remote monitoring (Track D).
- **Data lake (raw zone):** every inbound payload — HL7 messages,
  device streams, files, IoT events — lands immutable and as-received,
  with source, timestamp, and content hash. The warehouse stays the
  curated zone; acquisition jobs (the Phase 4 ETL pattern, generalised)
  build warehouse tables FROM the lake, so any table can be rebuilt by
  replay and every number carries lineage back to a raw payload.
- **Analysis features:** the workbench layer over lake + warehouse —
  ad-hoc query/explore (extends the Phase 4 explore surface and the
  Phase 9 research workbench), time-series analysis for device data,
  anomaly surfacing; feeds Track F reporting and gives Track A agents
  (forecasters, stewardship) something real to read. Store decision for
  high-rate streams (files-in-lake + aggregated FHIR Observations vs a
  time-series extension) is made at phase design time, open-source only.

## Track I — Data processing & pipeline platform

Acquisition (Track H) lands data; this track is everything that
processes it, as governed, observable pipelines rather than scripts:

- **Pipeline definitions are Studio config:** source → validation →
  transform → load steps, with schedules, dependencies (DAG), and
  owners. The Phase 4 dbt warehouse build and the Phase 8 outbox
  dispatcher become citizens of one framework, not separate machinery.
- **Run discipline:** idempotent steps, retry with dead-letter (the
  Phase 8 pattern), backfill and replay from the lake's raw zone,
  all-or-nothing commits where the consumer requires it (the 835
  poster rule), and a run history with durations and row counts.
- **Data quality gates:** schema validation, freshness checks,
  reconciliation totals, duplicate detection — failures quarantine the
  batch and open a workqueue item; bad data never silently lands.
- **Processing services:** terminology normalisation (local codes →
  standard code systems), de-identification for research extracts
  (the Rust de-id role from the original stack design), patient
  matching/deduplication candidates for the MPI workqueue, and the
  aggregation jobs that feed Track E dashboards, Track F reports, and
  Track H analysis.
- **Observability:** pipeline health on an ops dashboard (Track E);
  a failed or stale pipeline is a visible operational event, not a
  silent gap in a report.

## Track J — Regulatory & compliance alignment

Today the system has good raw materials (hash-chained audit, role
gates, QC programmes) but no compliance layer: nothing maps a workflow
to the regulation that governs it, and nothing can produce evidence
for a surveyor. This track builds that layer:

- **Requirements register:** every applicable requirement as data —
  body, citation, the operational control that satisfies it, where it
  is implemented, and the audit artifact that proves it. Register
  entries link to the hash-chained audit, so evidence is a query, not
  a scramble before a survey.
- **Regulatory packs are Studio config** (Principle 6): a HIPAA
  baseline pack (minimum-necessary access patterns, audit review
  cadence, retention periods), an ONC/Cures pack (information-blocking
  rules for data sharing, EHI export, FHIR/US Core API behaviour), a
  CLIA/CAP pack for the laboratory (QC/EQA gates from 6B become
  compliance controls), an EMTALA pack for the ER (screening-exam
  documentation elements, stabilisation and transfer rules driving
  Track D timers), a pharmacy pack (DEA/state-board controlled-
  substance rules, USP <797>/<800> compounding checks with Track C,
  DSCSA traceability with Track G), an FDA pack for blood bank
  traceability and for CDS/AI boundaries (agents in Track A must stay
  on the non-device side of FDA's CDS line, or be labelled otherwise),
  a 42 CFR Part 2 consent pack (substance-use records segmented and
  consent-gated — touches the psychiatry pack; the 2024 rule's single
  TPO consent model, compliance date Feb 16 2026), and CMS/Joint
  Commission packs (Conditions of Participation — including the ADT
  event-notification duty to community providers, which the Phase 8
  outbox already implements mechanically and the pack makes a named,
  auditable obligation — National Patient Safety Goals, and
  Promoting Interoperability measures).
- **GxP packs** (research and laboratory practice frameworks):
  a **GCP pack** (ICH E6) for clinical-trial operations — protocol
  adherence documentation, eConsent, eligibility verification,
  AE/SAE reporting clocks, source-data verification, essential
  documents — landing with the research workbench's depth; a **GLP
  pack** (FDA 21 CFR Part 58 / OECD GLP) for laboratory studies —
  study plans, an independent QA-unit inspection workflow, SOP
  governance (Studio versioned config is a natural SOP registry),
  raw-data integrity and long-term archiving; and **21 CFR Part 11**
  requirements (electronic records/signatures, audit-trail form)
  folded into the FDA pack, since they underpin both.
- **Compliance operations:** survey-readiness dashboards (Track E),
  evidence export (Track F), tracer-style self-audits (walk a synthetic
  patient's record end-to-end and check every control fired), and a
  change watch: when a pack's requirements change, affected workflows
  are listed for review before the new version can be enabled.
- **Jurisdiction packs (deployments outside the US):** GDPR is a
  first-class pack, not an afterthought — Article 9 health-data lawful
  bases, data-subject rights operations (access via the portal/FHIR
  surface, rectification, portability export), the 72-hour breach
  notification clock as a tracked workflow, DPIA and records-of-
  processing artifacts, and data-residency configuration (which region
  the lake/warehouse may live in). Ontario's PHIPA gets the same
  treatment (UHN is in the reference set). A site enables its
  jurisdiction pack(s) alongside the clinical ones; where rules
  conflict — GDPR erasure vs statutory medical-record retention —
  the pack resolves it explicitly (retention law wins for the record
  itself; erasure applies to everything the law lets go) and the
  resolution is itself auditable configuration, never a code branch.
- **Sequencing:** the framework and HIPAA/ONC baseline land in the
  foundation phase; each body's pack lands with its track (EMTALA
  with ER ops, compounding with pharmacy, CLIA formalised with the
  laboratory depth).

## Track K — Security, privacy & confidentiality platform

Track J maps what the law demands; this track implements the controls.
Honest current state: the dev system runs plain HTTP locally, has role
gates and a strong audit chain, but no encryption-at-rest story, no
key management, no break-the-glass, no snooping detection, and auth
has never been proof-run against a real IdP.

- **Encryption:** TLS everywhere beyond a developer's laptop (config
  for termination, HSTS); encryption at rest for the database and the
  lake (storage-level plus field-level encryption for the defined
  most-sensitive fields); **key management as a first-class concern** —
  KMS-backed keys, rotation, separation of duties, no keys in config
  files; encrypted backups (a backup is a copy of the PHI — it gets
  the same controls); encrypted storage on the mobile apps.
- **Access security:** OIDC against a real IdP with MFA (Keycloak
  proof-run is in the register); idle session lock tuned for shared
  clinical workstations (fade/lock/grace timers — the hospital
  tap-and-go pattern — as configuration, not constants); badge
  tap-and-go as a configured option when the IdP supports it;
  **break-the-glass** — emergency chart access that always works in
  the moment, always demands a reason, alerts in real time, and is
  **100% post-hoc reviewed** by privacy (the reference institutions'
  rule, not sampling), with justification due inside a configured
  window (24–48h pattern).
- **Privacy & confidentiality operations:** minimum-necessary views by
  role (the permissions matrix gains data-scope, not just page-scope);
  consent and segmentation enforcement (Part 2 pack consumes this);
  **snooping detection** — access-pattern auditing with anomaly alerts
  (VIP/flagged charts, bulk reads, off-hours access) feeding a privacy
  officer's workqueue; PII/PHI classification inventory across the
  lake, warehouse, exports and **logs** (log redaction — PHI in logs
  is a classic breach); masking on every non-production surface
  (already policy: synthetic only — now enforced by tooling);
  print confidentiality for Track F documents (queued release, no
  unattended PHI on shared printers).
- **Security operations:** vulnerability and dependency scanning in
  CI, penetration test before any non-local deployment, security event
  feed out of the audit chain (SIEM-ready), and an incident-response
  runbook that pairs with the Track J breach-notification workflows
  (HIPAA and the GDPR 72-hour clock).

## Shortfall register — everything known, with a disposition

_Found across the phase SQA reports, the standing NOT RUN list, and
Min's 2026-10-17 review. Nothing on this list is claimed done until
its phase SQA passes._

| Shortfall | Disposition |
|---|---|
| No regulatory/compliance layer — no requirement-to-control mapping, no survey evidence | Track J: framework + HIPAA/ONC baseline in Phase 10; per-body packs land with their tracks |
| Security/privacy engineering thin: no at-rest encryption or key management story, no break-the-glass, no snooping detection, dev runs plain HTTP | Track K: TLS + at-rest + key management + break-the-glass in Phase 10; detection/print/log controls with Tracks E/F/I |
| Offline-first operation is only a solo-profile note today; the landscape check (Bahmni, DHIS2, CGM) shows store-and-forward offline capture + sync is a feature class of its own | Deployment mode in Track H/mobile: offline capture with queued sync and conflict surfacing, designed with the device work |
| Operated-services model (athenahealth/TruBridge run the revenue cycle for customers) | Scoping note, not a build: OpenCare is operator-run software; the *software* obligations are the workqueues and automation those services use, already in Tracks C/F |
| Drug-interaction knowledge base (Phase 5 recorded it not implemented — no interaction checking exists) | Clinical safety item: license-free/open drug knowledge source evaluated at Phase 11 design; interaction checking lands with the pharmacy build, inert until a real knowledge source is attached |
| Data processing & pipelines ad hoc today | Track I (Phase 14) |
| No live device/instrument feeds | Track H (Phase 14), simulated-and-labelled until real links exist |
| Vite proxy + nginx missing `/messages`, `/engagement`, `/quality` prefixes | Foundation phase fix (Phase 10), staging patch + re-sync |
| Pack-enablement route gating (documented design gap, Phase 9) | Track B, Phase 10 |
| Browser click-through E2E absent (the gap that hid the 2026-10-17 fetch bug) | Standing SQA gate from Phase 10 on (Principle 4) |
| Postgres/Docker runtime never proof-run (compose validated only) | Phase 10 SQA adds a containerised smoke on a Docker-capable machine |
| Live Keycloak OIDC never run (dev auth only) | Phase 10 SQA item; config exists, needs a live IdP pass |
| Live Epic SMART launch never run (needs Min's fhir.epic.com client_id) | Blocked on Min's client_id; test harness ready since Phase 0 |
| Mobile apps never compiled; mobile doesn't consume Studio config | CI permission (Min) or local toolchain; config consumption lands with the Phase 13 dashboard work |
| Root `.github/workflows/mobile.yml` blocked (GitHub App lacks Workflows permission) | Min grants permission or copies the file via web UI |
| DICOM is metadata-only (no real pixel pipeline) | Rust DICOM service from the original stack design; scheduled with the radiology depth in Phase 15 |
| X12 clearinghouse submission + Surescripts are format/local only | Network integrations; documented boundaries, built when a real clearinghouse/network account exists |
| Live payer eligibility (EDI 270/271) absent — local-config check only | Same boundary as above; the honest `basis: local-config` label stays until then |
| Backup/restore never proof-tested; no DR runbook | Phase 10 platform hardening: scheduled backup + a tested restore drill + downtime read-only viewer (hospitals run on paper fallbacks; the system must degrade honestly) |
| Platform observability (metrics/logs/alerts for the EHR itself) thin | Track I observability + Phase 10 hardening |
| Terminology content is seed-level (two local codes by design: COAG-PANEL, BB-XM) | Terminology expansion via Track I normalisation pipelines as real code-system content is loaded |
| Accessibility (WCAG) and localisation never audited | Phase 13 audit item with the dashboard work (staff/patient surfaces) |
| Performance/load profile unknown (all SQA is functional) | Load pass added to Phase 12 SQA (continuum ops at census scale) |

## Proposed sequence

1. **Phase 10 — Foundation:** Track A (ADK) + Track B core (designers,
   rules, ADK tab) + the proxy fix + platform hardening from the
   register (backup/restore drill, containerised Postgres smoke, live
   Keycloak pass). Ends with the first two agents live as draft-only
   assistants (note/coding, lab summariser).
2. **Phase 11 — Pharmacy department build** (Track C) + BCMA slice of
   Track G + pharmacy agents (DUR reviewer, inventory forecaster).
3. **Phase 12 — Continuum ops** (Track D): ER, then inpatient,
   outpatient, aftercare; setting agents land with each.
4. **Phase 13 — People & data platform** (Tracks E + F): workforce
   core, configured dashboards per audience, reporting/documents,
   import/export.
5. **Phase 14 — Devices, IoT, lake & pipelines** (Tracks H + I):
   device gateways and monitoring, raw-zone lake, and the governed
   pipeline platform that processes it all.
6. **Phase 15 — Remaining departments + full barcode roll-out**
   (Track C remainder + Track G completion).

Alternative Min raised implicitly: pharmacy depth first, foundation
second. The foundation-first order is recommended because Tracks C–G
all consume it (agents, configured dashboards, print) — building any
department first means building its smart parts twice.
