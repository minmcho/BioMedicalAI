# C-AI — Clinical AI Governance — SQA Report

**Pass:** Gap Programme Wave C workstream C-AI (framework items AI-G1…AI-G10),
built to `~/workspace/gapprogramme_work/AI_GOVERNANCE_FRAMEWORK.md`.
**Build date:** 2026-10-21. **Gate closed by parent:** 2026-10-21.
**State:** staging only — not synced, not published. Release remains HELD.
**Baseline (Wave A, parent-verified):** backend 1,511 / web 573, build 0,
collisions 0. **This pass:** backend **1,550 / 0 / 0**, web **582 / 582**
(83 files), production build exit 0, case-collision audit ZERO.
Net: +39 backend tests, +9 web tests.

**CORRECTION (2026-10-21, parent):** this report originally stated
1,601 backend passes (+90 net). A strict recount of the three gate
logs (pytest summary lines only) proved the total is **1,550**: the
parent's original tally used a loose grep that also counted the
"51 passed" figure inside the run-2 resume script's narrative
header, double-counting batch 1. The pass/fail substance is
unchanged — every batch summary is green after the two fixes below
— only the arithmetic was wrong. The corrected figures appear
throughout this report; the Inventory Depth SQA carries the same
recount note. The corrected report re-syncs to the Air in Run 15
(Run 14 carried the pre-correction copy).

The coordinator agent ended without filing this report; the gate,
defect resolution, and this SQA are the parent's. Build facts below are
from the coordinator ledger (`~/workspace/cai_work/PROGRESS.md`),
spot-verified by the parent against staging code.

## What was built

- **Validation-statement register** (new `backend/app/ai_governance.py`,
  ~1,890 lines): statements are versioned documents — draft editable,
  supersede-never-edit, prior approved version superseded on new
  approval. Submit enforces the framework §4 completeness machine
  (23 fields; evidence runs must exist and match agent + published
  version + route; thresholds may tighten tier defaults, never loosen
  them without a recorded governance reason; the five §4.5
  prohibited-use codes are mandatory: `no-autonomous-diagnosis`,
  `no-unreviewed-order`, `no-unreviewed-patient-communication`,
  `no-excluded-population`, `no-unlisted-route-tool-modality`).
  Recorded-mode evidence is labelled, never upgraded.
- **Risk tiers:** seeded agents stamped (note-coding-assistant T2,
  lab-result-summariser T2, pharmacy-dur-reviewer T2, rag-answerer T1,
  radiology-vision-assistant T3). An absent tier is treated as T2 —
  T1 must be positively classified.
- **Enable gate** (`agents.enable_agent`): existing BD-41 validation-
  record gate + NEW statement gate (governed
  `requireStatementForEnable`) + four-eyes for T2/T3 (enabler may be
  neither the published-version author nor the statement author).
  Enable with a record but no statement is a 409 naming the statement
  requirement (asserted directly by `test_cai_enforcement.py`).
- **Four-eyes as a shared service** (new `backend/app/four_eyes.py`):
  refusals are audited (`governance.four_eyes.refused`) then 403.
  Wave C item C-1 generalises this service to controlled documents.
  Also applied to `ai-provider` publish on both paths (the G8 finding:
  the kind was already registered in configstore; the missing work was
  four-eyes + tests). Change-control approver is now the authenticated
  user only — the request body can no longer name an approver.
- **Runtime gate** (G4): shared `runtime_gate()` consulted by agent
  runs, workflow steps, the doc-intake classifier, the innovation run
  path, and MCP/A2A sends. Refusal idiom stays per-surface; the reason
  names expiry, suspension, or the kill switch.
- **Kill switch:** `AiGovernanceState` singleton; engaging it flips ALL
  agent enablements off (audited per agent). Release clears only the
  state — re-enablement is per-agent through the normal gate.
  Auto-suspension on breach thresholds via `AiBreachTracker`.
- **Safety linkage:** `ai-agent` added to the governed safety-event
  taxonomy defaults; the raise helper validates against the published
  taxonomy and falls back to `other` if a site removed the category.
- **Governed configuration (System Studio surface):** the
  `integration/agent-validation` payload is extended with
  `tierRequirements` (framework §3.2 matrix), `requireStatementForEnable`,
  and the drift/breach policy; published configs merge over the new
  defaults, so the keys take effect without republishing. Studio web
  surfaces: ValidationTab gained the statement register + governance
  state; AdkTab extended; all controls call the server and surface gate
  refusals. **Agents keep EMPTY model routes — nothing became
  runnable.** No agent can run until the Intelligence Evidence Run
  produces a live statement against a real model route.
- **Test choreography:** the shared `tests/conftest.governance_for_enable`
  helper performs the genuine flow (recorded-mode run minting a current
  record + statement authored, submitted, approved with the tier's
  approval kinds, four-eyes respected). Existing suites that publish +
  enable agents were migrated onto it; assertions were not weakened.

## Gate history (honest record)

The full gate (133 backend files in 23 batches + web + build +
collision audit) took three runs: two were killed mid-run by runtime
restarts that also took down the local live stack (PostgreSQL /
MariaDB / Keycloak / MinIO, restarted by the parent each time).
Banked batch results from the on-disk logs are the evidence; no batch
result was ever assumed.

- Run 1: batch 1 banked — 51 passed (incl. all four new `test_cai_*`
  files). Killed during batch 2.
- Run 2: batches 2–15 banked — 931 passed, **3 failed** (batch 12).
  Killed during batch 16 (result lost, re-run in run 3).
- Run 3: batches 16–23 — 565 passed. Backend total across runs:
  **1,550 passed / 0 failed / 0 errors** on the reconciled basis below
  (51 + 931 + 565 = 1,547 passed in the gate runs, with batch 12's
  3 failures superseded by the 12/12 re-run of the only affected
  file — 1,547 + 3 = 1,550).

## Defects found by the gate — both test-side, both fixed

1. **`tests/test_phase16_rag.py` — missed test migration (3 failures).**
   The suite's `_validate_for_enable` minted only the BD-41 record;
   under the new statement gate the enable was refused (409, by
   design) and the `/rag/answer` tests failed with "published but not
   enabled". The C-AI migration had moved the phase-10 and workflows
   suites onto the shared governance flow but missed this file.
   **Fix (parent, test file only):** the helper now completes
   `governance_for_enable` after the dataset run, with the reason
   recorded in its docstring. Re-run: **12/12 passed.** No production
   code changed; no assertion weakened.
2. **`web/src/pages/studio/CaiGovernance.test.tsx` — stale code string
   (1 failure).** The test expected the draft editor to seed
   `unnamed-route-tool-or-modality`; the backend's canonical code —
   and the component's actual seed — is
   `no-unlisted-route-tool-modality`. The component was aligned to the
   backend at integration; the test's own expectation was not.
   **Fix (parent, one line):** expectation corrected to the canonical
   code. Re-run: file 9/9; full web suite **582/582**.

Parent's own verification (2026-10-21, after both fixes): web suite
582/582 (83 files), web build exit 0, `test_phase16_rag.py` 12/12,
backend totals reconciled from the three gate logs by strict
summary-line recount (see the correction note at the head of this
report): 1,550 passed / 0 failed / 0 errors.

## NOT RUN / standing limitations

- **Browser / rendered check:** not possible from this VM (standing
  limitation). Min's Air click-through after Run 14 is the acting
  browser gate.
- **No live model run:** agents have empty model routes by design; no
  LLM behaviour is verified or claimed. The first live statements come
  from the Intelligence Evidence Run.
- **Live Postgres/Keycloak/MinIO legs** ran as part of the batched
  suites against the local live stack, as in prior passes.

## Carry-forwards

- **Framework §15 final-gate criteria** (AI_GOVERNANCE_FRAMEWORK.md
  §15) are to be added to the programme final-gate checklist at
  **Wave C close**, per PROGRAMME_PLAN.md — tracked, not yet due;
  C-AI itself does not close Wave C (QC core remains).
- **Sync:** C-AI files + the 26 files deferred from Run 13 + the
  migrated `test_phase16_rag.py` and fixed `CaiGovernance.test.tsx`
  go in **Run 14**, after Run 13's remaining 70 read-only verifications
  close in the next stable Air window.
- `backend/requirements.txt` unchanged — no new dependencies.
