# Workforce, Teams & Panels — SQA Report

**Date:** 2026-10-19
**Scope:** Min directive 2026-10-19 — "create registration of medical
doctors/nurses and staffs and all employee to system" + "be realistic
how these organization operate" + "assignment and team panel and cross
team work". Modelled on the institutional operating-model research
(`~/workspace/research_notes/healthcare-institution-operating-models-20261018-0514/report.md`):
HR-fed workforce directory, medical-staff-office credentialing &
privileging, team-based assignment, Kaiser-style empanelment.
**Staging only** — not published to GitHub, not synced to any Mac.

## Gate results (coordinator-run on the final integrated tree)

| Suite | Result |
|---|---|
| Backend (96 test files, 10 sequential chunks) | **1,106 passed, 5 skipped, 0 failed** |
| Web (vitest, 62 files) | **417 passed** |
| Web production build | **exit 0** |
| ADK service | **8 passed** |
| Go services | **ok (exit 0)** |
| Rust services (cargo test) | **43 passed** |
| Backup/restore drill | **PASS** (incl. the 8 new workforce tables) |

Baseline movement: backend 1,068 + 5 skipped (Phase 16 parent gate) →
**1,106 + 5 skipped**; the +38 is exactly the two new suites
(`test_workforce_teams.py` 22 + `test_workforce_onboarding.py` 16).
Web 410 → 417 (+4 TeamsPage, +3 OnboardingPanel). Capabilities
77 → 80, nav modules 29 → 30, compliance register 33 → 34.
The 5 skips are the environment-gated live-database legs (unchanged).

**Gate incident (recorded honestly):** the first gate shell was
killed by a VM runtime restart after chunk 03; chunks 00–03 stand as
run (487 passed), and a second shell re-ran chunks 04–09 + all other
suites to completion on the unchanged tree. The initial full-gate
run also surfaced the two `test_realignment.py` nav-snapshot
failures — fixed at the test layer (see Adjusted tests) and the file
re-run green (19 passed); chunk 09's remaining 122 tests passed in
the gate run itself.

Method: backend as 10 sequential alphabetical chunks with per-chunk
private `/tmp` basetemps deleted between chunks (the chunked method is
the gate of record — a single run fills the 512 MB /tmp at 96 test
files), then web, build, ADK, Go, Cargo and the backup/restore drill,
strictly sequential. Artefacts: `~/workspace/wfgate/`.

## Delivered scope

### A. Employee registration / onboarding (`backend/app/onboarding.py`, router `/onboarding`)
One onboarding record drives the whole institutional chain, in order:
HR profile (employee id, job role from the governed job catalogue,
unit/service line, FTE, supervisor, start date) → credentialing file
(written into the EXISTING `StaffCredential` store — no second store —
so the Phase 13 expiry worklist and rules-engine scan govern
onboarding credentials identically) → privileging (the job role's
privilege template granted through `app.privileging`, each grant a
row + audit) → linked login account provisioned through the EXISTING
accounts machinery semantics (role from the job catalogue, dev token
returned once, only its SHA-256 stored) → orientation checklist (the
governed published form `orientation-checklist`, per-item completion
tracked). Stages `hr-profile → credentialing → privileging → account
→ orientation → active` (+ terminal `terminated`) are visible with
computed blockers on every read; the credentialing and orientation
gates refuse advancement (409, refusal hash-audited) naming each
missing/expired credential or incomplete item — an employee cannot
reach `active` with an expired or missing required credential.
Staff status is `inactive` until the final advance flips it.
Termination disables the linked account (its token stops
authenticating), ends team memberships and ends the clinician's
empanelments with the reason recorded — all audited.
Tests: `tests/test_workforce_onboarding.py` (16) — full physician
happy path incl. the provisioned token authenticating, non-clinical
path, expired-credential block, terminate mid-onboarding.

### B. Care teams (`backend/app/teams.py`, router `/teams`)
Team definitions are governed config (`terminology/teams`: team
types unit / service-line / consult-service / primary-care; team
roles attending / resident / rn / pharmacist / case-manager /
care-coordinator / pcp / member). Team objects carry membership by
staff + role-in-team; membership changes are audited, and an
`attending` membership is privilege-gated (below). A staffer's teams
ride on their directory record (`GET /people/staff/{id}` gains
`teams`) and on Home via `GET /teams/my-teams` (memberships, own
panel summary, coverage giving/receiving). Seeded: 5 teams
(Med-Surg A, ICU, ED, Clinic Primary Care, Cardiology Consult) with
realistic memberships across the seeded directory.

### C. Assignment (privilege-checked)
Patients ↔ teams: one PRIMARY team + any number of CONSULTING teams
per patient/encounter, each assignment carrying an accepting-team
state (requested → active / declined / ended). A second primary
request is a HANDOFF (see F). Staff ↔ patients inside a team ride the
existing machinery (attending extension, Phase 12 `NurseAssignment`)
and are surfaced, not duplicated, on the team panel.
**Privilege gate:** the shared `app/privileging.check_privileges`
enforces role eligibility + ACTIVE grant + required-credential
currency. It is wired into (1) team `attending` membership, (2) PCP
empanelment, and (3) the EXISTING attending-of-record write paths —
admission with `attending_staff_id` and
`POST /inpatient/encounters/{id}/attending` (coordinator integration
in `app/inpatient.py`): the admission's service line (bed → unit →
workforce service line) adds its config-required privileges (seeded:
critical care requires `icu-practice`). Refusals are 409 naming the
missing privilege or the expired/missing credential, and the refusal
itself is hash-audited (`inpatient.attending.refused`,
`teams.membership.refused`, `teams.empanelment.refused`) — the
BD-31 pattern. Unassigned-patient queue per unit/service line
(`GET /teams/unassigned`): live admissions with no active primary
team.
Tests: `tests/test_workforce_teams.py` (22) — incl. grant-less
attending refusal → grant → success, expired-credential refusal
naming the date, ICU service-line refusal, revoke → refusal again.

### D. Empanelment (Kaiser pattern)
Every primary-care patient can be empanelled to a named PCP +
their primary-care team (other team types refuse 422). Re-empanelment
supersedes — the old row is ended with its reason, never edited
(the BD-24 pattern). Panel size and WEIGHTED panel size are computed
deterministically from published config (`panelWeights`: age bands,
per-condition weight, frailty extra — basis returned per patient and
labelled as configuration). Panel manager view (`GET /teams/panels`):
per-PCP size/weighted/capacity vs published thresholds,
empanelment coverage (% of clinic patients empanelled),
unempanelled list; `GET /teams/panels/rebalance` returns
suggestions only (candidate patients + receiving PCPs for
over-capacity panels) — a move is an explicit audited empanelment
action. The existing care-gap panels are wired in:
`GET /teams/panels/{staff_id}` returns the PCP's patients with
weights plus their care gaps filtered from the Phase 12 continuum
computation (outreach unchanged).

### E. Team panel (UI)
New web module **Teams** (nav key `teams`, Operations group, gated
`teams.read`) — `web/src/pages/TeamsPage.tsx` with shared Tabs:
Team panel (MasterDetail team list → members, patients with
Primary/Consulting StatusBadge, bed/attending/primary-nurse,
pending inbound with Accept as the one primary action, the team's
unassigned queue with assign action, coverage in/out), Assignments
(house-wide unassigned queue + create form + pending requests),
Panels (coverage numbers, PCP capacity table, empanel form), My
teams (honest empty state when the sign-in has no directory entry).
Workforce page gains an **Onboarding tab**
(`web/src/pages/OnboardingPanel.tsx`): stage Stepper, blockers,
credentials, privileges, account state (one-time dev token shown
once with a copy-now warning), orientation checklist, Advance /
Terminate. Design standard held: shared Tabs/Stepper/StatusBadge,
workflow state as text + colour, one primary action per panel,
server error detail surfaced inline, forms `noValidate` with
client+server validation.

### F. Cross-team work
Consults: a consulting-team assignment is the bedside consult
(request → accept/decline with reason); advice-only stays the
Phase 15 eConsult — two distinct objects, neither faked into the
other. Co-management = primary + consulting assignments coexisting
with explicit roles (order ownership follows the primary team).
Handoff of primary responsibility: requesting a new primary while
one is active creates a handoff in `requested` state, visible as
pending inbound on the receiving team's panel and pending on the
sender's; acceptance ends the old primary and activates the new one
in ONE transaction (note records the supersession); decline leaves
the incumbent untouched — no silent transfers. Coverage:
time-boxed delegation records (person→person, person→team,
team→team coverage directions validated XOR), audited; a covered
team's patients appear on the covering team's panel flagged
`via_coverage`, and the covered team's own panel flags the patients
as covered.

## Configuration-coverage list (standing rule: configure, don't fork)

Every site difference in this pass is published Studio configuration;
republishing changes behaviour with no code change (each flip
test-proven where marked *):

- `terminology/job-catalogue` (NEW governed document) — per job role:
  clinical flag, required credentials, default account role,
  privilege template. Drives onboarding gates and account
  provisioning.
- `terminology/privileges` (NEW) — privilege catalogue (code,
  eligible roles, required credentials) + `serviceRequirements`
  per service line (seeded: critical-care → `icu-practice`).
- `terminology/teams` (NEW) — team types, team roles, panel weights
  (age bands / per-condition / frailty) and panel capacity
  thresholds* (threshold republish flips a panel to "over" in tests).
- `form/orientation-checklist` (NEW governed form) — the orientation
  items onboarding tracks; a republish changes the checklist.
- `terminology/workforce` (existing) — role/unit/service-line value
  sets and credential windows continue to govern validation and
  credential status everywhere incl. onboarding.
- Navigation: module `teams` added to the seeded navigation
  (Operations group); capabilities `teams.read` / `teams.work` /
  `workforce.onboard` added to the permissions defaults + web mirror
  in lockstep (77 → 80).

## Compliance-coverage list

- **CMS 42 CFR 482.22 (Medical staff)** — NEW register entry
  (register 33 → 34): credentialing/privileging grounded in verified
  credentials; privilege grants are individual, auditable records;
  assignment gates refuse unprivileged or lapsed-credential
  clinicians with the reason named and the refusal in the hash chain.
- **Joint Commission HR.01.02.05 / MS.06.01.03** (existing entries) —
  their evidence now also covers the onboarding credentialing stage
  and the privilege gate: the same credential store feeds the
  worklist, the scan/rule alerts and every assignment check.
- **HIPAA 45 CFR 164.312(a)(2)(i)** (existing) — onboarding
  provisions exactly one account per employee through the managed
  account system; termination disables it immediately.
- **Minimum necessary / role gating** — onboarding writes are
  `workforce.onboard` (admin/configurator only); team writes are
  `teams.work`; all reads are capability-gated; patients hold none
  of the new capabilities.

## Adjusted tests register (no assertion weakened)

All adjustments are legitimate-growth mechanism snapshots extended by
the precedent every prior phase followed; no assertion was weakened
and no behaviour gate was relaxed.

1. `tests/test_frontdoor.py` — compliance-register total 33 → 34
   (the teams seed adds the CMS 42 CFR 482.22 medical-staff entry).
2. `tests/test_phase13_reports.py` — same register total 33 → 34.
3. `tests/test_depth_nav.py` — `EXPECTED_GROUPS` gains
   `"teams": "Operations"` (the new nav module's group).
4. `tests/test_realignment.py` — (a) the seeded-nav module snapshot
   gains `"teams"` after `"workforce"` with the phase comment
   extended; (b) the module-count assertion 28 → 29
   (30 seeded modules − lab).
5. `tests/test_nav_seed_consistency.py` — the expectation TABLES
   gained `MODULE_GATES["teams"] = "teams.read"` and a `teams`
   ENTRY (title/capabilities/roles mirroring the permissions
   defaults exactly) so the seed-vs-config consistency test covers
   the new module — new coverage, not an adjustment of existing
   expectations.

## Named partials / honest scope notes

- **Panel care-gap wiring** is a filtered read of the Phase 12
  continuum computation per PCP panel; outreach task creation remains
  the continuum endpoint, unchanged.
- **Primary-nurse assignment writes** stay in Phase 12 continuum
  (the team panel displays them); this pass adds no second writer.
- **Empanelment seed** lands only for patients already in the store
  (demo seed pat-1…pat-6 → staff-015) — app boot never invents
  patients, so a bare boot shows coverage 0 until patients exist.
- **Onboarding for the pre-existing seeded directory**: two records
  are seeded (staff-001 complete/active, staff-019 mid-orientation)
  as the legacy-directory representation; the other seeded staff
  predate onboarding and carry privilege grants directly (the MSO
  "already privileged" state), which is exactly what the privilege
  gate reads.
- `app/onboarding.py` carries a defensive fallback for its
  privileging import (written while the sibling module was in
  flight); in the integrated tree the real `app.privileging` binds
  and the fallback never executes. Recorded, not hidden.
- Roster/on-call rows for a terminated employee are schedule history
  and are not rewritten; termination ends memberships,
  empanelments and the account (the live surfaces).

## NOT RUN (standing list, unchanged — not claimed)

- Real-browser click-through E2E — VM browser limitation; Min's Air
  click-through remains the browser gate.
- Live LLM run, Postgres/Docker runtime (beyond the Phase 16
  phase-local evidence), live Keycloak OIDC, live TLS serving,
  mobile compiled builds, live external network legs (TEFCA,
  clearinghouse, payers) — unchanged from Phase 16.
- Pack-enablement route gating default-ON remains Phase 17 scope.

---

## Parent re-verification addendum (2026-10-19, parent-run)

The parent re-ran the full gate independently on the final tree: backend as 10 sequential alphabetical chunks with per-chunk private basetemps, then web, build, ADK, Go and Cargo, strictly sequential. Artefacts: `~/workspace/wfverify/`.

| Suite | Parent result |
|---|---|
| Backend `pytest tests/` (10 chunks) | **1,106 passed, 5 skipped, 0 failed** — identical to the coordinator's totals |
| Web vitest | **417 passed** |
| Web `npm run build` | **exit 0** |
| ADK | **8 passed** |
| Go `go test ./...` | **ok (exit 0)** |
| Cargo `cargo test` | **43 passed** |

The 5 skips are the standing environment-gated live legs (4 in `test_platform_db_live.py`, 1 live pgvector) — the same set as the Phase 16 parent gate; the parent's own live-engine run remains in Phase 17's scope. No discrepancies, no watch items this pass.

**Workforce, Teams & Panels is parent-verified.**
