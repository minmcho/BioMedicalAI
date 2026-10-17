# Realignment — SQA Report (2026-10-16)

**Scope.** Implementation of the realignment specified in
`docs/sqa/REALIGNMENT_AUDIT.md` (backlog groups A, B, C, E; group D
partially — see §5), under Min's standing rule (roadmap §6):
*configure, don't fork*. The audit's findings were: the backend clinical
core is genuinely config-driven, but the web shell consumed zero config
at runtime, the permissions config governed only some writes, and pack
enablement gated nothing.

**Method.** Defaults preserve the pre-realignment behaviour in effect
everywhere: every new config field is seeded with today's constant, and
every consumer falls back to the built-in default when config is absent
or unreadable. The full pre-existing suites were run before and after;
new tests prove each behaviour flip happens by *publishing config*, not
by code paths the old system lacked.

## 1. Results

| Suite | Before | After |
|---|---|---|
| Backend (`pytest backend/tests`) | 146 passed | **165 passed** (146 existing + 19 new) |
| Web (`vitest run`) | 112 passed / 18 files | **135 passed / 21 files** (112 existing + 23 new) |
| Web build (`tsc --noEmit && vite build`) | pass | **pass** |

No existing assertion was weakened. Three existing tests were updated
where they asserted the old hardcoded *mechanism* rather than behaviour
(§4), and one fixture gained a required additive field.

## 2. What was built

**A — Runtime consumption.**
- `navigation/main` and `branding/default` are seeded as published v1 at
  startup (idempotent), payload-for-payload mirrors of the shell's
  built-in defaults (same 12 items, labels, order, per-item roles;
  orgName "OpenCare", portalTitle "My results", accent `#0b6e4f`).
- The web shell fetches the published navigation, branding and
  permissions configs at sign-in (`RuntimeConfigProvider`); each fetch
  is guarded independently and falls back to the built-in default on any
  failure (offline API, 404, or the patient role's by-design 403), so
  the shell can never break on config. Nav renders from the published
  payload (unknown modules ignored, `visible: false` honoured, order +
  labels from config); the brand and login render the published org
  name; the accent is written to the `--accent` CSS variable; the
  portal title comes from branding.
- Page access (`canAccessWith`) and every per-page action flag
  (lab.work/order/verify, pharmacy.dispense/administer, ed.work,
  radiology.work/verify, surgery.work, billing.manage, pophealth.run)
  resolve from the published permissions matrix merged over defaults,
  so UI and server agree after an override publish. Page→capability
  mapping follows the server's gates (Studio page access follows
  `studio.manage`, not `studio.read`).
- Scheduling, Lab, Billing, ED and Surgery pages derive their offered
  transitions from the published workflow payloads (lab's comes from the
  `/lab/catalog` response it already fetched; the others use the
  effective-config endpoint), with the previous maps kept as fallback.
- StudioPage header copy corrected: drafts change nothing until
  published; once published, the API reads config at request time and
  the shell at sign-in. (The old text — "Nothing here takes effect until
  a human publishes it" — was false for the API surfaces.)

**B — Permissions completion.**
- Six capabilities added to `CAPABILITY_DEFAULTS` with defaults copied
  tuple-for-tuple from the old hardcoded gates: `studio.read`,
  `lab.read`, `pharmacy.read`, `boards.read` (admin, configurator,
  clinician, viewer), `pophealth.view` (admin, clinician),
  `pophealth.run` (admin).
- Every hardcoded gate converted: `STUDIO_ROLES`/`READ_ROLES`
  (studio.py, studio_config.py — Studio's own APIs now enforce
  `studio.manage`/`studio.read`), `STAFF_READ` (lab.py ×6, pharmacy.py
  ×4, boards.py ×8), `POPHEALTH_ROLES` + admin-only ETL (pophealth.py).
- The permissions validator now **accepts new capability names** in a
  published matrix (defining capabilities is configuration; structural
  problems — unknown roles, non-boolean cells — still 422). See §6 for
  the enforcement-point distinction.

**C — Workflow/threshold extraction** (all default-preserving).
- Per-transition `roles` are honoured by all five state machines (ADT,
  lab, claims, ED, surgery): a restricted transition returns 403 naming
  the transition and the allowed roles; absent `roles` = any authorised
  role, as before. Enforcement order is uniform: unknown state 422 →
  illegal transition 409 → role 403.
- Lab governed/specimen state sets, ED active/governed state sets moved
  into their workflow payloads (`governedStates`, `specimenStates`,
  `activeStates`; validated as declared states).
- Claim `exportableStates` and `maxAppeals` moved into the
  claim-lifecycle payload (with maxAppeals = 1 the appeal-refusal
  message is byte-identical to before).
- ESI range is terminology: `terminology/ed-triage` seeded with
  `min`/`max` + level concepts; triage validation and the ED page's
  acuity options read it.
- BCMA enforced rights are the formulary payload's `bcmaRights`
  (default: the original three, in the original order; the dispense
  lookup itself is not optional — a nonexistent dispense still 422s).
- QC policy parameters are the lab catalog's `qc` object:
  `blockOnFailedLatest` (default true), `blockOnMissingQc` (default
  false), `freshnessHours` (default none). Blockers now carry a
  `reason` (`failed-latest` | `missing-qc` | `stale-qc`) and a nullable
  `qc_run`.
- Cohort age bands are per-cohort config (`ageBands` on the cohort
  payload; both seeded registries carry them; the built-in bands remain
  the fallback).
- The ADT transition map now has one backend source:
  `fhir.APPOINTMENT_TRANSITIONS` is an alias of
  `configstore.DEFAULT_ADT_TRANSITIONS` (backlog #17).

**E — Critical-value notification.**
- New `lab_critical_notifications` table (kept out of the FHIR report
  body: who was phoned is never part of what a patient reads — the same
  separation as QC runs).
- `POST /lab/reports/{id}/notify` {`notified_to`, `read_back`} and
  `POST /lab/reports/{id}/acknowledge` (`lab.verify` capability):
  pending → notified → acknowledged with actor + timestamps, hash-chain
  audited. Guards: non-critical report → 400; unreleased → 409;
  read-back missing/false → 422; notify twice → 409; acknowledge before
  notify → 409.
- `GET /lab/critical-reports` (lab.read) lists released critical reports
  with notification state; the verification queue/report summaries carry
  the same `notification` object. The Lab page shows the state in the
  queue and a Critical results section with the notify form (recipient +
  read-back checkbox) and acknowledge action for permitted roles.

## 3. New tests (flip proofs)

Backend `backend/tests/test_realignment.py` (19 tests): seeded
nav/branding shape + idempotency; published nav without a module is the
effective nav; lab.read revocation 403s a viewer (others unaffected);
pophealth.view/run grants flip 403→200; studio.manage revocation blocks
config writes *and* pack installs; per-transition roles flip in all five
machines (403 for the unlisted role, 200 for the listed one);
governed-states flip (bare transition into `resulted` succeeds once
un-governed); ED activeStates flip shrinks the board groups;
exportableStates flip exports a draft; maxAppeals=2 allows a second
appeal and refuses the third; ESI max=3 makes acuity 4 a 422; BCMA
subset (`["right-patient"]`) lets a wrong medication code pass while a
wrong wristband still 422s; QC missing/freshness policies block verify
with `missing-qc`/`stale-qc` reasons; the full notification lifecycle
including every guard, plus patient clean-view (403 on the worklist, no
notification internals in the patient's FHIR report).

Web (23 tests): `runtimeConfig.test.tsx` — default matrix + no/seeded
payload reproduce the legacy nav for **every** role; published nav
removal/order/labels/visibility enforced; `canAccessWith` equals legacy
`canAccess` for all 65 role/page pairs under defaults and flips with a
published matrix (including the studio.manage-not-studio.read rule);
branding parse fallbacks; provider applies published branding and
survives total fetch failure. `LabPageCritical.test.tsx` — notification
state rendering, notify gating on recipient + read-back, acknowledge
flow, viewer read-only. `workflowDerivation.test.ts` — the five pages'
payload→buttons derivations reproduce today's buttons under seeded
payloads. `client.test.ts` — effective-config + notify/acknowledge
request shapes.

## 4. Existing-test changes (mechanism, not behaviour)

1. `backend/tests/test_studio_config.py` — the branding lifecycle tests
   (draft/publish/supersede/rollback/disable/immutability/audit/diff)
   used the key `branding/default`, which the realignment now *seeds*
   (backlog A5); their version arithmetic assumed an empty key. They now
   use a fresh key (`branding/clinic`) with every lifecycle assertion
   unchanged; the "effective list is empty" assertion became "contains
   only the seeded default" (the draft's absence is still asserted), and
   one list assertion now expects the seed alongside the new key.
2. `backend/tests/test_studio_config.py::test_invalid_payloads_are_422_with_details`
   — asserted an unknown capability in a permissions payload is a 422
   ("Unknown capability"). Backlog B explicitly removes that mechanism;
   the test now asserts the payload is accepted (201) and that
   structural problems (unknown role, non-boolean cell) remain 422.
3. `backend/tests/test_studio_audit.py` — created `branding/default` and
   asserted the audit entity `…@v1`; with the seed, that draft is v2.
   The test now creates `branding/audit-clinic` and asserts `@v1`; all
   ordering/hash assertions unchanged.
4. `web/src/pages/studio/models.test.ts` — asserted the serialized
   matrix has exactly 14 capabilities; the catalogue legitimately grew
   to 20 (the six capabilities backlog B added). Count updated with a
   comment naming them.
5. `web/src/test/fixtures.ts` — `fixtureVerificationItem` gained the
   `notification` field the API type now requires (additive).

## 5. Group D — decisions

- **Concept-map → Studio migration (#14): NOT DONE** (explicitly
  deferred by the tasking; high risk — the ETL reads a second database).
- **Warehouse router move (#16): SKIPPED, not zero-risk.** The warehouse
  router's ETL endpoint calls `etl.run_etl`, while `etl.py` imports the
  warehouse module for its table definitions; moving the router into
  `warehouse.py` creates a circular import. Routes, payloads and gates
  are unchanged.
- **Cohort tab (#15): already existed** in the Studio Advanced (JSON)
  surface; skipped per tasking.
- **Pack enablement (#D): documented known gap** — see §7. No gating
  mechanism was improvised.

Divergence note: audit item #7 proposed *deleting* the TypeScript
transition mirrors. They are retained as fallback maps (the published
payload wins whenever it is fetchable), per the tasking's fallback
discipline ("never a broken shell"); the mirrors are now defaults, not
the source of truth.

## 6. Configuration-coverage list (roadmap §6)

**Now configuration-driven (publish flips behaviour, proven by tests):**
shell navigation (items/labels/order/roles/visibility); branding (org
name, portal title, accent); web page access + per-page action flags;
every backend role gate incl. Studio's own APIs and all module reads;
pophealth view/run; per-transition roles in all five workflows; lab
governed + specimen state sets; ED active + governed state sets; claim
exportable states; claim appeal limit; ESI range + level labels; BCMA
enforced-rights set and order; lab QC policy parameters; cohort age
bands; workflow-driven buttons on the Scheduling/Lab/Billing/ED/Surgery
pages.

**Still coded, with justification:**
- *Enforcement points* — which endpoint checks which capability is
  code. Config defines the matrix and may define new capability
  *names*, but a capability no endpoint enforces is inert; this
  distinction is deliberate (a config typo must never silently create
  or remove a gate).
- *Patient self-scoping and unreleased-result invisibility* — safety
  invariants; not site-configurable by design.
- *QC pass/fail computation* (measured vs expected range) — a
  measurement verdict, not policy; the policy around it is config (§2C).
- *Duplicate-administration refusal, dispense-stock arithmetic, the
  dispense lookup in BCMA* — integrity invariants under any policy.
- *Protocol shapes* (837 EDI structure, HL7 v2, DICOMweb) and OMOP
  output shapes — external standards, not site preference (do-not-touch
  list).
- *The ADT/lab/claim/ED/surgery transition graphs' seeded defaults* —
  they are config (above); the code copy is only the fallback for a
  disabled/unpublished config, which is the documented resilience rule.
- *ESI clinical semantics* — the ESI scale is a clinical standard; its
  range endpoints and labels are config, its meaning is not a site's to
  redefine.
- *Small-cell fallback (5)* — the per-cohort threshold is config; the
  constant is only the fallback for cohorts that don't set one.
- *Navigation modules-only rule, audit hash chain, draft→publish
  governance* — engine invariants (do-not-touch list).
- *Pack enablement gating* — known gap, §7.
- *OMOP concept map* — still the warehouse `concept_map` table;
  migration deferred (§5).

## 7. Known gap: pack enablement gates nothing (design note)

**State today.** Pack lifecycle (`installed_draft → approved →
attached → enabled`, plus `disabled`) is fully implemented as governed,
audited bookkeeping, and manifests declare per-endpoint
`"enabled": false`. No code path reads either: all module routes are
live from first boot regardless of pack state.

**Why not retrofitted here.** Gating clinical routes on pack state is a
fail-closed change to a live system: a wrong default or a missing
route→pack mapping silently 404s clinical surfaces. It needs a product
decision (below), not an improvisation.

**Proposed design (for a future phase, with its own SQA).**
1. Give every router an explicit `module` identity and build the
   route→module table at startup from the pack manifests (no
   hand-maintained map).
2. Seed installations for the shipped packs in `enabled` state, so the
   default deployment is byte-identical to today.
3. Enforce in one dependency: a request to a module whose pack is not
   `enabled` → 404 (indistinguishable from absent). Auth, health,
   Studio and the config endpoints are never gated (a site must always
   be able to re-enable).
4. Decide, with Min, the clinical-safety semantics of disabling a
   module with in-flight work (e.g. lab orders mid-lifecycle): refuse
   disable while open work exists, or allow + drain. Recommendation:
   refuse with a 409 listing the open-work count.
5. Ship behind the existing suites plus new tests: disabled pack →
   its routes 404; re-enable restores; ungated surfaces unaffected.

## 8. NOT RUN

- Live browser session against a running backend (this pass is
  API-level + component tests, as with prior phases).
- Mobile consumption of navigation/branding config — `mobile/` was out
  of scope and untouched; mobile config consumption is unverified.
- Postgres runtime (suites run on SQLite, as in all prior phases),
  Docker/nginx deployment, live Keycloak/OIDC sign-in (dev tokens).
- The `agents/` ADK service (parked draft; untouched, out of scope).
- GitHub publish and Mac sync — not requested for this pass.

## 9. Files

Backend: `configstore.py` (new capabilities, nav/branding payload
builders, shared workflow-machine helpers, validator extensions),
`studio_config.py`, `studio.py`, `fhir.py`, `lab.py` (+ notification
endpoints and the `LabCriticalNotification` model in `models.py`),
`billing.py`, `pharmacy.py`, `boards.py`, `pophealth.py`;
tests: `test_realignment.py` (new), `test_studio_config.py`,
`test_studio_audit.py` (§4).
Web: `auth/runtimeConfig.tsx` (new), `auth/session.ts`,
`auth/AuthContext.tsx`, `App.tsx`, `api/client.ts`, `api/types.ts`,
`api/adt.ts`, `pages/studio/models.ts`, and the Lab/Billing/ED/Surgery/
Scheduling/Pharmacy/Radiology/PopHealth/Login/Portal/Studio pages;
tests: `auth/runtimeConfig.test.tsx`, `pages/LabPageCritical.test.tsx`,
`pages/workflowDerivation.test.ts` (new), `api/client.test.ts`,
`pages/studio/models.test.ts`, `test/fixtures.ts` (§4).
Docs: `PHASES.md`, `STUDIO_CUSTOMIZATION.md`, `FULL_SYSTEM_ROADMAP.md`
(drift corrections), this report.
Untouched per the do-not-touch list: config engine core semantics,
audit chain, protocol code, OMOP shapes, seed data, `mobile/`,
`smart-app/`, `warehouse/`, `agents/`.
