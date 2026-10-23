# Phase 11 SQA Report — Pharmacy department depth + BCMA slice

Date: 2026-10-18. Scope: PHASE10_ROADMAP Track C (pharmacy exemplar),
Track G (barcode slice), Track J pharmacy packs. Staging only;
synthetic data only.

## Gate results

- **Backend: 393 passed** (362 Phase 10 baseline + 31 new Phase 11
  tests: `tests/test_phase11_pharmacy_ops.py`,
  `tests/test_phase11_bcma.py`). Build agent's final full run logged
  at `~/workspace/phase11_final_suite.log`.
- **ADK runtime service: 8 passed** (own suite, own venv).
- **Web: 227 passed** (211 baseline + 16 new), `npm run build` clean.
- Parent re-verification: see the addendum at the end of this report.

## Delivered scope

1. **Inventory & procurement.** Lot-level stock by location
   (`PharmacyLot`) kept equal to the Phase 4A aggregate counter by
   construction — receiving, FEFO dispense allocation, adjustments and
   witnessed waste move both in one transaction. Purchase orders with
   cumulative receiving that cannot over-receive. Receiving captures
   DSCSA transaction data (lot, serials, expiry per line) into an
   append-only traceability ledger (`DscsaLedgerEntry`). Low-stock and
   expiry worklists are computed from lots + PAR, never stored.
2. **ADC restock.** Top-up-to-PAR lists per cabinet location with the
   recent-dispense count (window from config) as context.
3. **Verification + DUR work centre.** Verification queue derived from
   signed/active orders without a verification row, ordered by clinical
   priority then wait; SLA per priority from config, breaches flagged.
   Verify is a clinical sign-off (`pharmacy.verify`): formulary-
   restricted items require an explicit restriction acknowledgement;
   non-formulary items cannot be verified. Verification emits the
   rules-engine event `pharmacy.dur.screened` carrying the patient's
   Cockcroft-Gault creatinine clearance; the seeded `renal-dose-review`
   rule (Phase 10 engine, threshold in rule config) turns a low CrCl
   into a pharmacist Task. DUR reviewer agent definition seeded as a
   **draft** agent config (runs remain honest-off without a model).
4. **Controlled substances.** Two-person shift counts (expected vs
   counted, witness ≠ counter), witnessed waste, discrepancy records
   that must be resolved with a reason before the count book for that
   medication + location balances. Every movement is in the
   hash-chained audit.
5. **Sterile compounding / IV room.** Batch records on a queue separate
   from verification; ingredients capture lots; beyond-use datetime
   computed from the published USP <797> category config (never a code
   constant); release requires a verifier who is not the preparer.
6. **BCMA (Track G slice).** Barcode payload standards are config data
   (prefixes, separator); payloads are `PREFIX|field|field|check`
   strings a USB wedge scanner types like a keyboard.
   `POST /pharmacy/bcma/administer` runs the five-rights check —
   patient, medication, dose, route, time — against the order behind
   the scanned dispense. Identity failures (wrong patient, wrong
   package/lot, wrong drug) are absolute hard stops; dose/route/time
   deviations require a recorded override reason and are audited. The
   MAR web flow gains a scan panel (labelled simulator input for
   development; the same field accepts wedge-scanner input).
7. **Regulatory packs.** Draft regulatory packs + register entries for
   DEA/state controlled-substance rules, USP <797>/<800>, and DSCSA
   traceability, each citing the operational controls above.

## Drug-interaction knowledge source — evaluation outcome

**Decision: checking is INERT.** No interaction knowledge source was
found that is openly licensed for redistribution in software AND
structured AND clinically governed:

- NLM RxNav interaction API — discontinued 2024-01-02.
- DrugBank severity data — commercially licensed.
- DDInter — academic dataset; no redistribution licence, no RxNorm keys.
- openFDA — adverse-event reports, not pairwise interactions.

The check endpoint exists, names its config slot
(`interactionKnowledgeSource` in `terminology/pharmacy-ops`, currently
null), and returns an explicit no-source status with zero findings. No
interaction pair or severity is invented anywhere in the system. An
operator with a licensed source can attach it at that slot when a
loader exists.

## Configuration-coverage list

All Phase 11 behaviour differences are published Studio config
(`terminology/pharmacy-ops` unless noted): pharmacy locations and the
default dispense location; PAR level + reorder point per medication
per location; expiry warning window (30 d); verification SLA minutes
per priority (stat 30 / asap 60 / urgent 120 / routine 480); ADC usage
window (7 d); USP <797> beyond-use hours per category per storage
condition (Category 1 carries the chapter's values; Category 2 values
are site examples, labelled as such in the config); barcode payload
standards (wristband prefix `OCWB1`, med-label prefix `OCML1`,
separator); the interaction knowledge-source slot; the formulary's
restricted status per medication (Phase 4A formulary config); the
`renal-dose-review` rule's CrCl threshold (rules engine config).

## Compliance-coverage list

- DEA / state controlled-substance rules → two-person counts,
  witnessed waste, discrepancy resolution before the book balances,
  full movement audit (register entries + draft pack).
- USP <797> → BUD from published category config, preparer/verifier
  separation at release, lot capture per ingredient.
- USP <800> → register entry + draft pack (hazardous-drug handling
  controls are configuration for the compounding queue).
- DSCSA → lot/serial/expiry capture at receiving into an append-only
  traceability ledger; saleable-return style lookups run against the
  ledger.
- Joint Commission NPSG (two identifiers, medication safety) → BCMA
  five-rights hard stops with audited overrides at the MAR.

## NOT RUN / gaps — honest list

- **Live LLM run: NOT RUN** (unchanged; no model endpoint). The DUR
  reviewer exists as a draft agent definition only.
- **Drug-interaction checking: INERT** (see evaluation above) — by
  design, not by omission.
- **Browser click-through E2E: NOT RUN** (unchanged VM limitation —
  Chromium local-network-access blocks loopback automation; see the
  Phase 10 addendum).
- **Postgres/Docker runtime, live Keycloak OIDC: NOT RUN** (unchanged).
- **Pack-enablement route gating:** unchanged documented design gap.
- Physical scanner hardware: not applicable in this environment; the
  scan field is a standard keyboard-wedge input by design.

---

## Parent verification addendum (2026-10-18, independently re-run)

- Backend suite re-run by the parent agent (`pytest tests/` from
  `backend/`): **393 passed**, exit 0 — confirms the build run above.
  Runtime was 5 min 23 s on a quieter machine; the build run's
  65 minutes was VM contention that night, not suite growth.
- Web suite re-run by the parent: **227 passed**; `npm run build`
  re-run clean. ADK suite re-run: **8 passed**.
- Spot checks: `backend/app/pharmacy_ops.py` implements the scope
  above on the Phase 10 platform (ops config, rules-engine DUR event,
  DSCSA ledger, CS count book, compounding, BCMA five rights); the
  interaction evaluation is recorded in the module itself and the
  config slot ships null.
