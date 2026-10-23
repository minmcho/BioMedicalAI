# Inventory Depth — SQA Report

**Pass:** Gap Programme §3b Inventory Depth (Min-approved 2026-10-20),
built to `~/workspace/gapprogramme_work/INVENTORY_DEPTH_MODEL.md`.
**Build date:** 2026-10-21. **Gate closed by parent:** 2026-10-21.
**State:** staging only — not synced, not published. Release remains HELD.
**Baseline (C-AI, parent-verified, as corrected):** backend 1,550 / web 582.
**This pass:** backend **1,611 / 0 / 0** (134 files, 23 batches),
web **589 / 589** (84 files), production build exit 0,
case-collision audit ZERO. Net: +61 backend tests, +7 web tests.
`backend/requirements.txt` unchanged (md5 recorded in the build ledger).

Build facts are from the coordinator ledger
(`~/workspace/invdepth_work/PROGRESS.md`), spot-verified by the parent;
the gate totals below are the parent's strict recount (see "Counting
discipline").

## What was built

- **Unit ladder:** items carry `base_uom` + a governed `uom_ladder`
  (per-level `containsNext`, optional per-level barcode); stock is held
  in base units and movements record their `unit_level`, converting
  exactly. PAR levels are expressible per ladder level
  (`par_unit_level` / `par_qty`).
- **Kits & components:** items are SIMPLE or KIT (`kit_components`,
  `kit_mode`: track-whole vs break-on-receipt). Kit lots carry a
  composition snapshot (component sku + lot); kit expiry is the
  earliest component expiry from that snapshot. `kit_open`, assemble
  and disassemble are first-class append-only ledger movements.
  Instruments (asset-linked items) are never consumed, cannot be kit
  components (422), and cannot be container-tracked stock.
- **Containers below the lot:** per-container identity with location,
  `opened_at`, and in-use expiry that overrides shelf expiry in FEFO
  once opened; container open / consume / dispose / transfer endpoints
  (transfer moves the whole container with exact source/destination
  lot quantities; a disposed container refuses transfer, 409; lot-level
  transfer of a container-tracked item refuses 409 naming the rule and
  the container alternative). Chemicals carry `hazard_class` + `sds_ref`
  and a disposal state — cradle-to-grave.
- **Stage workflow with lot-verification gate:** lots move
  RECEIVED → QUARANTINE → RELEASED → IN_USE → terminal; only RELEASED
  stock is allocatable and FEFO is stage-aware. Reagent release
  requires a current passing lot-verification record (lot-to-lot
  verification: method + result + actor + UTC date, stamped by the
  verify endpoint, audited, snapshotted into the release movement's
  detail); a missing/expired verification refuses 409 naming it, and a
  failed verification blocks release until a later pass supersedes it.
  **No emergency bypass exists in this pass** — JIT pressure is an
  operations problem, not a safety-gate exception (explicit position,
  carried from the lab report §3.4 refinements).
- **Recall propagation:** recalling a lot cascades to its live
  containers; any kit whose composition references a recalled lot is
  recall-blocked — excluded from FEFO and named in a `recall_blocked`
  field wherever kit stock is serialised. Traceability runs
  lot → kits and kit → component lots from the composition snapshot.
  Control materials are ordinary items under the same lot machinery
  (§3.4 refinement honoured). **Lot → RESULT linkage is NOT built**
  (Lab Depth D9); lot identity (sku, lot_number, serial, expiry,
  stage) is exposed on every stock serialisation for D9 to join.
- **Governed configuration (System Studio surface):** the inventory
  configstore kind gained the depth validator + defaults + capability
  (`validate_inventory`); taxonomy subcategories carry class policy;
  quarantine / recall / disposal reason codes are governed taxonomy
  data. Web: InventoryPage gained the item ladder/kit editors, the
  lots & containers panel, per-level PAR editing, and client methods
  for lot verify/stage, container operations, and kit
  open/assemble/disassemble — all calling the server, refusals surfaced.

## Scope position (recorded at design time, restated honestly)

Preference-card lines carry no draw-as configuration today: **kits
draw as whole kit units** through the stage-aware FEFO plan
(inherited, not rebuilt). **Component-level draw per card line is NOT
built**; `kit_open` at the case location is the operator path for
components. This was a coordinator design position recorded in the
build ledger, not a silent cut — it stands as the pass's one named
scope position for Min's ratification.

## Gate history + counting discipline

The coordinator's gate took four runs; three were killed by runtime
restarts that also took down the local live stack (restarted each
time). Run 1 died ~1 minute in with nothing banked. The coordinator
ended before its final report; the parent owns the reconciliation.

**Counting discipline (lesson applied):** totals here are a STRICT
recount — pytest summary lines only — because the parent's earlier
loose-grep tally of narrative-bearing logs double-counted a batch in
the C-AI gate (that report and the PHASES row are corrected; the same
loose method had also overstated this gate's banked subtotal, 1,209
claimed vs 1,169 strict, and masked the batch-18 gap below).

- Runs 2–4 banked: 683 + 486 + 365 = 1,534 passed / 0 failed across
  22 batch summaries.
- **Batch 18 gap:** run 3's log carried 9 batch headers but only 8
  summaries — batch 18 (test_phase6b_lab, test_phase7_access,
  test_phase7_revenue, test_phase8_interop, test_phase8_mllp,
  test_phase9_engagement) was in flight when run 3 died, and run 4
  resumed after it. The parent re-ran batch 18 itself:
  **77 passed / 0 failed.**
- **Final backend total: 1,611 passed / 0 failed / 0 errors**
  (1,534 + 77), all 23 batches evidenced. Web (run 4): **589/589**,
  build exit 0, collisions 0. Workers' own pre-gate spot checks
  (test_inventory_depth.py 29 passed incl. the 5 container-transfer
  tests; inventory suites 51 passed) are consistent with the gate.

## NOT RUN / standing limitations

- **Browser / rendered check:** not possible from this VM (standing
  limitation). Min's Air click-through after Run 15 is the acting
  browser gate.
- No new dependencies; `requirements.txt` unchanged.

## Carry-forwards

- **Sync:** the 6 Inventory-Depth files excluded from Run 14
  (inventory.py, configstore.py, test_inventory_depth.py, client.ts,
  InventoryPage.tsx, InventoryDepth.test.tsx) + this report + the
  corrected C_AI_SQA_REPORT.md + updated PHASES.md go in **Run 15**.
- Lab Depth D9 joins lab results to this pass's lot identity — the
  join surface is ready.


## Addendum — 2026-10-21 evening (post-Run-15 defect, fixed in Run 16)

Min's Air click-through — the acting browser gate named above —
caught a defect this gate could not: the Depth pass added columns
to four PRE-EXISTING tables (inventory_items, inventory_stock_lots,
inventory_movements, inventory_par_levels; 18 columns) without the
codebase's startup upgrade step, and `create_all` never alters an
existing table. Every gate database is born fresh, so the suites
stayed green while Min's older Air database 500'd on /home/cards,
/inventory/items and /inventory/overview ("no such column:
inventory_items.structure", "…inventory_par_levels.par_unit_level").

Verified by diffing the Air's live database schema against the
current model metadata: all 202 tables present, exactly those 18
columns missing, nothing else in any module.

Fix (parent-built, data-preserving): `inventory.ensure_inventory_columns`
— inspector-guarded ALTER TABLE ADD COLUMN for exactly the 18,
DDL defaults matching the model defaults (existing lots backfill to
stage "released", their pre-Depth allocatable behaviour) — wired in
`app.main.create_app` next to the Wave A pharmacy hook, following
the org_hierarchy / pharmacy_ops precedent. Regression tests added
to tests/test_inventory_depth.py: the ensure itself (adds + idempotent)
and an end-to-end boot of create_app on a pre-Depth-schema database
serving /home/cards, /inventory/items and /inventory/overview with
pre-Depth rows intact. Affected suites re-run: 128/128
(test_inventory_depth, test_inventory, test_inventory_platform,
test_home_cards, test_org_hierarchy, test_wave_a_pharmacy).

Standing rule from this defect: any pass that adds a column to an
existing table ships (a) the guarded ensure hook and (b) an
old-schema regression test in the SAME pass; the final gate's
checklist gains an old-database upgrade run.
