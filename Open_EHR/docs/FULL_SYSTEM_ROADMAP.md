# Full-system roadmap — how the reference institutions actually run, and what OpenCare still needs

Direction from Min, 2026-10-16: model the system on how Mayo Clinic, Cleveland Clinic,
Johns Hopkins, UCSF Health, Bascom Palmer Eye Institute, Shirley Ryan AbilityLab,
Toronto General (UHN), and Kaiser Permanente operate — "minimum functionality at
this point; I want a fully system."

## 1. How those institutions actually run (the pattern, not the brand)

Almost all of them run **ONE integrated enterprise EHR + revenue-cycle platform**
for the whole organisation — Epic at Mayo, Cleveland Clinic, Johns Hopkins, UCSF,
Bascom Palmer (UHealth), UHN Toronto (Epic, live 2022), and Kaiser Permanente
(HealthConnect, a heavily customised Epic build). The exception in the list is
instructive: **Shirley Ryan AbilityLab runs Cerner (Oracle Health)**, live 2017 —
proof that the operating model, not the vendor, is what matters:

- **One patient, one record, one database** across every site, specialty, and
  care setting (inpatient, ED, OR, clinic, rehab, home). No departmental silos.
- **Everything is an order**: labs, imaging, meds, consults, therapies, blood —
  CPOE with decision support, and **closed loops** (order → fulfil → result →
  acknowledge) everywhere, with barcode verification at the point of care.
- **Documentation is structured and role-specific**: nursing flowsheets, MAR,
  therapy documentation (rehab runs on functional-outcome instruments), anaesthesia
  records, operative notes — all feeding the same chart and the same bill.
- **Patient access is a first-class system**: enterprise scheduling, referrals +
  authorisations, registration/eligibility, estimates, a portal (MyChart-class:
  results, messaging, questionnaires, telehealth, self-scheduling, bill pay).
- **Revenue cycle is integrated, not bolted on**: charge capture at the point of
  care, coding/HIM, claims + remittance + denials, patient financial services.
- **Operations run on the same data**: bed management/census, OR throughput,
  ED throughput, transfer centre, staffing — real-time boards fed by ADT events.
- **Analytics, quality, and research are built on the record**: registries,
  quality measures, cohort tools, de-identified research extracts — plus
  mandatory public-health and registry reporting.
- **Interoperability is continuous**: HL7 v2 feeds to/from ancillaries and other
  organisations, FHIR APIs, document exchange networks, device integration
  (monitors, pumps, analysers streaming into flowsheets).
- **Governance**: a central informatics team owns configuration (order sets,
  documentation templates, rules) with clinical sign-off — the exact role our
  System Studio plays, which is why Studio is a module of its own, not a page.

The differences between the institutions are **specialty depth**, not core
architecture: Bascom Palmer = ophthalmology workflows (imaging-heavy eye exams,
laterality everywhere); Shirley Ryan = rehabilitation (functional assessments,
therapy minutes, interdisciplinary team conferences, long stays); Toronto General
= transplant/cardiac quaternary care inside UHN; Kaiser = integrated payer-provider
(prevention, population care, total-cost view).

## 2. Minimum functional inventory of a "full system" — and where OpenCare stands

| Domain | Minimum functionality | OpenCare today |
|---|---|---|
| Identity & access | SSO/OIDC, MFA, role + context access, break-the-glass, audit | PARTIAL — OIDC mode built, live IdP unverified; no MFA/BTG |
| Registration & scheduling | Enterprise scheduling, registration, eligibility, referrals/authorisations | PARTIAL — appointments + ADT basics; no eligibility/referral workflow |
| ADT & bed management | Admit/transfer/discharge events, census, bed board, transfer centre | PARTIAL — ADT transitions + ED admit linkage; no bed/census system |
| CPOE & orders | Order sets, favourites, decision support (allergy/interaction), consults, order acknowledgement | MISSING — orders exist only inside lab; no general order framework |
| Clinical documentation | Notes (all types), nursing flowsheets, vitals, I&O, assessments, care plans, problem-oriented charting | MISSING — chart is read-mostly resources; no notes/flowsheet authoring |
| Medication closed loop | Formulary, order → pharmacy verify → dispense → BCMA → MAR, infusion | PARTIAL — dispense + BCMA-lite exist; no pharmacy verification step, no MAR |
| Laboratory | Full LIS incl. microbiology/pathology, POCT, blood bank | PARTIAL — core chemistry/haem LIS with QC; no micro/path/blood bank |
| Radiology / imaging | RIS workflow, DICOM archive (PACS), reporting, critical results | PARTIAL — study metadata + DICOMweb reads + report verify; no image ingest/archive |
| Surgery & anaesthesia | OR scheduling, pre-op, anaesthesia record, PACU, implants/supplies | PARTIAL — case board + overlap control; no anaesthesia record |
| ED | Tracking, triage, disposition, fast-track, LWBS metrics | DONE (core) — board + ESI + wait times |
| ICU / acute care | Device data, drips, ventilator, early-warning scores | MISSING |
| Specialties | Ophthalmology, rehab (functional outcomes), cardiology, oncology, transplant, OB, behavioural health, dialysis | DONE (Phase 9) for Min's 11 service lines — each a governed Studio pack (order sets, forms, flowsheets, terminology, registry); deep proof for ophthalmology (laterality enforced), rehabilitation (functional outcomes), psychiatry (lab-gated dispensing). Transplant + dialysis were not in the 11 and remain unbuilt |
| Therapies (PT/OT/ST) | Rehab documentation, functional instruments, therapy scheduling | PARTIAL — rehabilitation pack (Phase 9): FIM-shaped functional instrument + delivered-minutes flowsheets (PT/OT/speech) + therapy evaluations as consult orders; dedicated therapy scheduling not built |
| Patient engagement | Portal: results, messaging, questionnaires, telehealth, self-scheduling, bill pay, proxy access | PARTIAL — portal reads results/conditions/medications; Phase 9 added secure messaging (FHIR Communication threads, patient-scoped), telehealth visit workspace (configured provider link — no video stack), self-scheduling (staff-confirm default / auto-confirm config), telehealth intake form; patient statements exist (Phase 7) but no online bill pay; proxy access not built |
| Revenue cycle | Charge capture, coding/HIM, claims, remittance (835), denials, patient billing, estimates | PARTIAL — claims + 837 export; no 835/coding/denials workqueues |
| HIM & compliance | Chart completion/deficiency, release of information, coding queues | MISSING |
| Interoperability | HL7 v2 live feeds (ADT/ORM/ORU over MLLP), FHIR bulk, document exchange, public-health reporting | PARTIAL — HL7 ORU ingest (paste/API) + SMART; no live MLLP listener, no ADT feeds, no reporting |
| Analytics & quality | Warehouse, registries, quality measures (eCQM-style), operational dashboards | PARTIAL — OMOP warehouse + registries + explorer; Phase 9 added the quality-measure engine (measures as Studio `measure` config over the reused pophealth evaluator, small-cell suppression, 3 seeded measures with exact-math tests); operational dashboards not built |
| Research | Cohort discovery, de-identified extracts, trial screening | PARTIAL — Phase 9 research workbench: saved cohort definitions (Studio `cohort` configs) + snapshot counts over time + de-identified aggregate export (counts/distributions only, capability-gated + audited; row-level export refused by design); trial screening not built |
| Platform | Multi-site/org hierarchy, HA, downtime viewer, config governance (Studio) | PARTIAL — Studio engine real; single-site today |
| System Studio | Visual configuration IDE: packs, forms, workflows, terminology, navigation, branding, permissions, integrations, audit | ENGINE + 11-tab visual module DONE (2026-10-16); runtime consumption DONE (realignment, 2026-10-16 — shell nav/branding/permissions + full API enforcement; docs/sqa/REALIGNMENT_SQA_REPORT.md). Known gap: pack enablement does not gate module routes (design note in the same report) |

## 3. Build-out phases (sequential, SQA-gated, Studio-config-first — same rules as Phases 0–4)

- **Phase 5 — Orders & documentation core**: general order framework (CPOE)
  with order sets as Studio config, allergy/interaction checking against the
  chart, consults; clinical notes (DocumentReference authoring + templates as
  Studio forms); nursing flowsheets (vitals/assessments as Questionnaire-driven
  flowsheet rows); MAR built on Phase 4A administrations. *This is the phase
  that turns the chart from read-mostly into the working record.*
- **Phase 6 — Inpatient operations**: ADT event system + bed management
  (locations/units/beds as Studio data), census + bed board, transfer/discharge
  workflows, case management + discharge planning, anaesthesia record +
  perioperative documentation depth on the Phase 4B surgery board.
- **Phase 7 — Access & full revenue cycle**: referral + authorisation workflow,
  eligibility checks (270/271-shaped), registration workqueues, HIM/coding
  queues, 835 remittance posting, denials workqueue, patient statements +
  portal bill pay.
- **Phase 8 — Interoperability & reporting**: live HL7 v2 over MLLP (ADT A01/A02/A03,
  ORM, ORU) as a Go service (per the system design's Go tier), FHIR bulk export,
  immunisation + syndromic public-health reporting packages, external document
  retrieval ( Carequality-pattern ), device-data ingest into flowsheets.
- **Phase 9 — Specialty operations packs + engagement** (detailed in §4):
  one governed Studio pack per clinical service line, each carrying that
  specialty's operations — order sets, intake/follow-up forms, flowsheets,
  procedure workflows, terminology, registry. Plus patient messaging +
  telehealth visits + self-scheduling in the portal, quality-measure engine
  on the warehouse, research cohort workbench (de-identified, small-cell
  suppressed like population health).

## 4. Specialty operations (Min, 2026-10-16) — what each service line actually does all day

Every pack below is built the OpenCare way: the specialty's operations are
**Studio configuration** (order sets, forms, flowsheets, workflows,
terminology, registry definition) on the Phase 5 orders/documentation core,
plus a focused workspace page — not a hardcoded silo. That is also how the
reference institutions run them (Foundation-model order sets + specialty
documentation templates, locally governed).

- **Cancer (oncology)** — diagnosis/staging charting (TNM), treatment plans
  and chemotherapy regimens as cycle-based order sets (regimen → cycle →
  day orders), infusion scheduling + chair management, toxicity assessments,
  antiemetic/supportive-care bundles, tumour-board documentation, survivorship
  care plans, trial screening flags; registry: cancer registry cohort +
  regimen adherence measures.
- **Cardiology** — chest-pain/ACS pathways with timed troponin orders, cath-lab
  case workflow (pre/post, contrast, radiation dose), echocardiography +
  stress-test orders and structured reports, heart-failure clinic flowsheets
  (weights, GDMT titration), device clinic (pacemaker/ICD checks), cardiac
  rehab referrals; registry: HF + post-MI cohorts, door-to-balloon-style
  timeliness measures on ADT/order timestamps.
- **Diabetes & endocrinology** — A1c-driven diabetes registry with outreach
  queues, insulin titration order sets, CGM/pump device-data ingest into
  flowsheets, hypoglycaemia event tracking, diabetic foot/eye/kidney screening
  bundles (auto-recalls), thyroid/adrenal dynamic-test protocols (timed
  stimulation/suppression test orders), endocrine clinic intake forms.
- **Gastroenterology** — endoscopy workflow (booking → bowel-prep instructions
  → procedure report → pathology linkage), IBD clinic (disease-activity scores,
  biologic infusion linkage), liver clinic (fibrosis scores from labs),
  hepatitis treatment pathways, colonoscopy quality measures (adenoma
  detection, prep quality) on the warehouse.
- **Geriatrics** — frailty + comprehensive geriatric assessment forms
  (cognition: MMSE/MoCA-shaped instruments, ADL/IADL scales, falls risk,
  polypharmacy review with deprescribing flags), delirium screening flowsheets,
  advance-care-planning documentation, caregiver assessments; registry:
  frail-elderly cohort with falls/readmission measures.
- **Pulmonology & lung** — spirometry/PFT orders + structured results
  (FEV1/FVC into flowsheets), COPD/asthma action plans, inhaler technique
  documentation, sleep-study workflow (order → study → CPAP titration),
  lung-nodule tracking registry with guideline-interval recalls, bronchoscopy
  procedure workflow (with Phase 4B boards pattern), oxygen therapy orders.
- **Obstetrics & gynaecology** — prenatal visit flowsheets (fundal height,
  fetal heart, GBS/glucose screening schedule by gestational age), labour &
  delivery board (admission → labour stages → delivery record → postpartum),
  fetal-monitoring data capture, postpartum checks, gynae procedure workflow
  (colposcopy, hysteroscopy), well-woman screening recalls (cervical/breast
  linked to registries).
- **Ophthalmology (Bascom Palmer pattern)** — imaging-centric exam charting
  (visual acuity, IOP, OCT/fundus imaging via the Phase 4B DICOMweb pattern),
  **laterality on every order/result** (OD/OS/OU), cataract surgery pathway
  (biometry → lens selection → OR → post-op drops), intravitreal injection
  clinic workflow (high-volume, bilateral-aware), glaucoma/diabetic-eye
  registries with visit-interval recalls.
- **Orthopedics** — fracture clinic workflow, joint-replacement pathway
  (pre-op optimisation → OR → implant registry record → PT milestones),
  sports-medicine clinic, cast/splint documentation, bone-health/osteoporosis
  screening (DEXA orders, fracture-risk scores), op/post-op PROMs
  (patient-reported outcome questionnaires via portal).
- **Psychiatry** — intake + risk assessment (suicide-risk instruments,
  safety planning), rating-scale battery as recurring forms (PHQ-9, GAD-7,
  MDQ-shaped), medication monitoring protocols (lithium/clozapine labs with
  hard-stop recalls — clozapine ANC gating reuses the Phase 2 QC-gate
  pattern), therapy session documentation, legal-status/involuntary-hold
  tracking with expiry alerts, ECT procedure workflow.
- **Rehabilitation (Shirley Ryan pattern)** — admission functional assessment
  + goal-setting (FIM/CARE-item-shaped instruments), interdisciplinary team
  conference documentation, therapy scheduling + delivered-minutes tracking
  (PT/OT/speech), daily therapy flowsheets, equipment/prosthetics orders,
  discharge functional-outcome scoring + community-reintegration planning;
  registry: outcome-by-diagnosis cohorts (functional gain, length of stay).

Build order inside Phase 9 follows dependency weight, not prestige:
ophthalmology + rehabilitation first (patterns already seeded by Phases 4B
and 2), then cardiology + oncology + diabetes/endo (registry-heavy), then
GI + pulmonology + geriatrics, then orthopedics + OB/GYN + psychiatry.

## 5. The other end of the scale — a single-doctor office (Min, 2026-10-16)

The same institutions' model must also collapse down to a solo practice,
because that is where most care happens. A single-doctor office operates
nothing like a small hospital:

- **Three hats, one or two people each**: front desk (phones, check-in,
  insurance cards, co-pays), a medical assistant (rooming, vitals, vaccines,
  point-of-care tests), and the physician (everything clinical, often their
  own documentation, refills, results review, and the business itself).
- **The day is a schedule, not a census**: 12–25 same-day/advance visits —
  physicals, chronic-care follow-ups (the doctor's own diabetes/hypertension
  panel), acute complaints, procedures the office can do (ECG, spirometry,
  POCT labs, immunisations, minor procedures).
- **No ancillary departments**: labs mostly sent OUT (reference-lab orders +
  results back), imaging referred out, meds = a medication list + refill
  workflow (real e-prescribing runs on the Surescripts network — a network
  integration, out of scope here, same honesty rule as the X12 clearinghouse).
- **Billing is a superbill, not a revenue department**: visit level + codes
  captured at checkout → claim out → payment posted; one person's Friday
  afternoon, not a HIM department.
- **Continuity is the product**: recall lists ("A1c due", "annual physical",
  "flu shots in October"), the doctor knowing their panel — population
  health at 800-patient scale.

**Design answer: a deployment PROFILE, not a separate product.** OpenCare
ships profiles as Studio packs — one install applies the module subset,
navigation, permission defaults, and workflow simplifications for the setting:

- `profile-solo-practice` — core chart, scheduling, Phase 5 notes/orders,
  POCT lab mode (single-step result entry allowed for office tests — a
  workflow-config change, the two-step verify stays for hospital labs),
  medication list + refill queue, billing-lite (superbill → existing Phase 4A
  claims), portal with online intake forms + appointment requests, recall
  registries (the Phase 3 cohort engine at panel scale). No ADT/beds/OR/ED,
  no warehouse requirement — SQLite, one machine, runs offline.
- `profile-clinic` — the same, times a few providers: shared schedules,
  MA/provider task pools, standing orders.

Profiles land with **Phase 5** (the orders/documentation core is what a
clinic needs most), gated by an SQA scenario: one synthetic solo-practice
day end-to-end — 12 fabricated patients, check-in → rooming vitals (Studio
form) → note → POCT result → refill → superbill → claim — all synthetic.

## 6. The standing rule — the system configures to the requirement (Min, 2026-10-16)

**Configure, don't fork.** One codebase serves the academic medical centre,
the specialty service line, and the single-doctor office. Every difference
between settings — module subset, workflows and their states, forms and
fields, terminology and value sets, roles and permissions, navigation,
branding, thresholds and gates, deployment profile — is **System Studio
configuration data**: versioned, draft→publish, role-gated, hash-audited.
New code is written only for a genuinely new capability (a new engine),
never to fit one site's requirement. If a requirement cannot be met by
configuration, that is a defect in the configuration surface, and the fix
extends the surface — it does not hardcode the requirement.

Already proven on this rule in Phases 1–4 (each flips behaviour by config
publish, with tests): ADT transition rules, lab catalogue + QC gating,
claim lifecycle + fee schedule, cohort/registry definitions, radiology
viewer integration, OR room inventory, pack lifecycle governance.
(Correction, 2026-10-16 audit: navigation was at first proven only on
the WRITE side — the modules-only 400 — while the shell menu itself was
hardcoded. The realignment pass, 2026-10-16, made it genuinely
consumed: the shell renders navigation/branding from the published
configs and resolves its capability gating from the published
permissions matrix, and the QC gate's rule parameters — not just the
catalogue — are configuration now too. See
docs/sqa/REALIGNMENT_SQA_REPORT.md.) Enforcement: every phase SQA report from Phase 5 on
must include a **configuration-coverage list** — which behaviours the phase
made configurable, and a justification for anything left coded.

## 7. Clinical laboratory — full workflows & operations (Min, 2026-10-16)

Checked against the shipped Phase 2 code on 2026-10-16. What exists is a
solid CORE LIS; a clinical laboratory is much more than that.

**Existing today (verified in code, `backend/app/lab.py` + lab pack v2.0.0):**
order → specimen → result → verify → release workflow (config-driven
states); LOINC-coded catalogue as terminology config (10 default analytes:
CBC + BMP subsets, reference ranges + critical limits); result entry with
flag interpretation; **two-step verify→release** (unreleased results are
invisible everywhere else); **QC gate** — failed QC per analyte blocks
verify/release; HL7 v2 ORU + CSV ingest (drafts only); critical flagging in
the verification queue; specimen list; LabPage UI (orders, queue, ingest,
specimens, QC status).

**Required to meet the new requirements — the full laboratory:**

- *Pre-analytical*: collection worklists (phlebotomy rounds), specimen
  labelling/accessioning, chain-of-custody timestamps
  (ordered → collected → received → in-analysis), specimen
  rejection criteria + recollection workflow.
- *Discipline benches* (each a workflow + terminology extension, per §6):
  chemistry, haematology, coagulation, urinalysis as full bench catalogues;
  **microbiology** (culture workflow: accession → culture → organism ID →
  susceptibility → staged/prelim reports); **blood bank / transfusion**
  (type & screen, crossmatch, unit inventory, issue/return, transfusion
  reaction workup); **anatomic pathology & cytology** (accession → gross →
  histology → pathologist sign-out, synoptic reports); immunology/serology;
  molecular; **POCT management** (devices, operator lists, POCT QC —
  ties to the solo-practice profile's in-office tests).
- *Post-analytical*: auto-verification rules engine (config), delta checks,
  reflex-testing rules (config), **critical-value notification workflow**
  (call + read-back + acknowledgement timestamps — the tracking core
  landed early in the 2026-10-16 realignment: pending → notified →
  acknowledged records on released critical reports; escalation timers
  and routing remain Phase 6B), amended/corrected reports with reason +
  audit, result-routing rules, reference-lab send-out tracking.
- *Laboratory operations*: QC **programme** (multi-level QC, Levey-Jennings
  data, Westgard-style rule evaluation, EQA/proficiency-testing records),
  instrument/analyser registry + maintenance/calibration log, TAT
  monitoring + dashboards (warehouse), staffing + competency records,
  supply/reagent lot tracking, test-utilisation stewardship (duplicate
  order alerts), compliance evidence views (CLIA/CAP-pattern).

**Phasing**: lands as **Phase 6B — Clinical laboratory completion**,
immediately after inpatient operations (the lab serves the inpatient
workflows), with the discipline benches delivered as Studio configuration
+ benches following the existing order/specimen/result spine. Critical-value
notification and amended reports are pulled FORWARD into the realignment
pass if the audit confirms they are small — patient-safety items first.

Each phase: synthetic data only, SQA report, publish to BioMedicalAI, Air sync —
identical discipline to Phases 0–4. The System Studio visual module lands first
(started 2026-10-16) so Phases 5–9 are configured, not hardcoded.
