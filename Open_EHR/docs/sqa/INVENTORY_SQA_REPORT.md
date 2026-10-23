# Inventory & Materials Pass — SQA Report (2026-10-20)

Coordinator-run gate; staging only. Release HELD — nothing pushed,
nothing synced to any Mac. Synthetic data only.

## Scope delivered

A dedicated hospital **Inventory & Materials** module — not a fork of
Pharmacy, not the device asset registry — with a governed taxonomy,
an item master with stock by governed placement location, an
append-only movement ledger, PAR-driven and expiry worklists,
requisitions, cycle counts, OR preference-card case draw with
implant lot traceability, barcode/RFID identification, and a full
web page in the Rich Design Elevation design system.

## What was built

### Governed taxonomy (configure, don't fork)

- New Studio config kind **`inventory`**, key **`taxonomy`**, with a
  dedicated strict validator (`configstore.validate_inventory`):
  `{categories: [{code, label, subcategories: [{code, label}]}],
  reasonCodes: [{code, label}], expiryWarningDays: int 1..365}`.
  Category codes unique; subcategory codes unique within their
  category; slug/shape rules follow the codebase's config-validator
  style. Draft → publish governance is the platform's ordinary flow;
  published versions are immutable.
- Seeded published v1 at startup via
  `configstore.ensure_inventory_defaults` (insert-if-absent, system
  actor, create+publish audit rows — the
  `ensure_job_profiles_default` pattern): categories **instruments,
  medical-appliances, surgical-supplies, implants, consumables,
  ppe, lab-reagents, linen**, each with detailed subcategories;
  reason codes damaged, breakage, lost, expired-disposal, recall,
  contamination, recount-correction, other; `expiryWarningDays: 90`.
- Items validate category/subcategory against the **published**
  taxonomy at write time; an unpublished pair is a 422 naming it.

### Item master, stock, costing

- `backend/app/inventory.py` (2,094 lines, 31 routes, own SQLAlchemy
  models on the shared Base): `InventoryItem` (SKU unique, name,
  category/subcategory, unit of measure, lot-/serial-/expiry-tracked
  flags, active), `InventoryStockLot` (item × location × lot ×
  serial, unique tuple), `InventoryParLevel` (item × location).
- **Costing (Min directive 2026-10-20 — surgery will price
  operations estimate-vs-actual):** the item master carries
  `unit_cost_cents` (integer cents ≥ 0, nullable = not yet valued);
  receiving captures an optional lot-level cost override (effective
  lot cost = override ?? item default); every movement stamps the
  effective `unit_cost_cents` and a signed `value_cents`, so a case's
  consumed-materials value is a plain sum over its `case_issue`
  movements. OR-minute / staff-role rates are explicitly NOT
  inventory's concern (governed config on the surgery side).
- **Locations are the governed placement hierarchy** (REGION →
  LOCATION → DEPARTMENT → UNIT, published `terminology/locations`)
  resolved through `app.org_hierarchy` — no parallel location model.
  Stock sits at facility/department/unit codes; a region or unknown
  code is a 422 naming it.

### Movements — append-only ledger (DSCSA pattern)

- `InventoryMovement` records receive / issue / transfer / adjust /
  count_adjust / case_issue / return with actor, timestamp,
  lot/serial/expiry where tracked, reference, governed reason code,
  patient reference, balance-after, and cost/value stamps.
- Issue / transfer / case-issue allocation is **FEFO**; expired lots
  never allocate; validate-all-then-apply — a shortfall is a 409
  naming every shortfall and **nothing moves**. Stock cannot go
  negative silently: hard validation with inline errors on both the
  API (`{detail}` 409/422) and the web forms.
- Tracking-flag law: lot-tracked requires a lot number,
  serial-tracked requires a serial and quantity 1, expiry-tracked
  requires an expiry date.
- Adjustments require a **published** reason code; reason `other`
  requires a note (server law, mirrored client-side).
- Requisitions (requested → partially_issued → issued / cancelled)
  and cycle counts (expected snapshot → counted → variance applied
  as `count_adjust` movements; a balanced count writes none).

### Worklists

- Low-stock: item × location rows with a PAR level whose on-hand is
  below PAR (deficit computed) — items without a PAR row never
  appear. Expiry: lots expired or expiring within the **governed
  window read from the published taxonomy at request time** (a test
  republishes the window 90 → 30 days and watches a 60-day lot fall
  out). Both filterable by location and category.

### Clinical draw + implant traceability

- `POST /inventory/case-issues` reads the surgery module's
  `PreferenceCard` and FEFO-issues **every** card line from real
  stock at the case location (card item code = inventory SKU),
  writing `case_issue` movements carrying the case reference
  (`Procedure/<case_id>`), the patient reference, and cost/value.
  Sterile-processing draws ride the same machinery via requisitions
  carrying references.
- `GET /inventory/implant-trace` answers **both directions** off the
  ledger for implant-category items: lot → patients/cases, and
  patient → lots.

### Identification

- `POST /inventory/scan` resolves, in order: the barcode registry
  (`app.barcodes`, entity refs `InventoryItem/<sku>` /
  `InventoryLot/<id>`), the RFID registry (`app.rfid` tags bound to
  an inventory entity), exact SKU, exact lot/serial, and the pipe
  payload `SKU|LOT[|EXPIRY]`. `POST /inventory/rfid-events` records
  a simulated RFID sighting resolved through the registry. Every
  scan appends an `InventoryScanEvent` audit row (hit or miss);
  unresolvable input is an honest 404.

### Boundaries

- Medications stay in Pharmacy: no drug items exist here; an item
  may carry a `formulary_code` **link**, validated against the
  published `terminology/formulary` (unknown → 422). Equipment
  assets stay in the device registry: an item may carry an
  `asset_key` **link**, validated against `Device.device_key`
  (unknown → 422). Assets are never stock.

### Surfaces

- Role gating through the permissions matrix: `inventory.read`
  (admin/configurator/clinician/viewer), `inventory.manage` +
  `inventory.move` (admin/configurator/clinician), seeded in
  `CAPABILITY_DEFAULTS`; the web page gate is `inventory.read`
  (`session.ts` PAGE_CAPABILITY; `test_nav_seed_consistency`
  transcribes and pins the agreement).
- Navigation: module key `inventory`, "Inventory & Materials",
  group **Safety & Support**, icon `boxes` (new ModuleIcon glyph),
  via the governed nav seed AND the seeded-default upgrade path —
  existing databases gain the card with no site action (pinned by a
  dedicated upgrade test; the legacy-era fingerprint projections in
  `studio_config` exclude the post-era module so fingerprint-only
  legacy databases keep matching their era and still upgrade).
  Placement decision: Operations was already at the regroup pin's
  6-module maximum; Phase 15 put supply chain under Support
  services, so the module sits third in Safety & Support.
- Web: `InventoryPage.tsx` (2,354 lines) — ModuleHeader (group
  icon/tile), overview stat cards from real aggregates (including
  stock value), Tabs (Items, Stock, Movements, Receiving & Issue,
  Counts, Worklists), Steppers for receive / issue-transfer-adjust /
  case issue / counts, scan prefill, `.table` system, StatusBadge,
  inline `{detail}` errors, every select fed by the published
  taxonomy or `/org-hierarchy` or disabled with the reason shown.
- Home-card indicator: **added** — a real count source exists.
  `home_cards._PROVIDERS["inventory"]` reuses the worklist
  computations (below-PAR line count; expiring/expired lot count in
  the governed window), tone neutral per the documented
  no-thresholds fallback.

## Configuration-coverage list (Studio surfaces)

| Surface | What a site governs without code |
|---|---|
| `inventory` config kind, key `taxonomy` | Category/subcategory taxonomy, adjustment reason codes, expiry-warning window — draft → publish, published immutable |
| `navigation/main` seed + seeded-default upgrade | The Inventory & Materials card (label, group, roles, purpose, icon) and its arrival in existing databases |
| Permissions matrix (`CAPABILITY_DEFAULTS`) | `inventory.read` / `inventory.manage` / `inventory.move` role grants |
| `terminology/locations` (Org Placement) | The stock-location code set (facility/department/unit) |
| `terminology/formulary` (Pharmacy) | The link targets for `formulary_code` |
| Studio Branding (`theme.groupColors`) | The Safety & Support group hue the module renders under |

## Compliance-coverage list

| Concern | Coverage |
|---|---|
| Audit | Append-only movement ledger (actor, timestamp, balance-after, reason, references); scan events audited; config seed/publish writes the platform audit chain |
| Traceability | Implant lot ↔ patient both directions off the ledger; DSCSA-pattern lot/serial/expiry capture on every movement (cf. Pharmacy DSCSA ledger) |
| Access control | Capability gating on every route (read/manage/move); viewer read-only; patient role has no inventory access (tested) |
| Data integrity | Validate-all-then-apply movements; stock-never-negative hard validation; unique lot tuple; governed reason codes; published-taxonomy validation |
| Data scope | Synthetic data only; no PHI (patient references are synthetic case refs) |
| Governance | Taxonomy changes only via Studio draft → publish; published config immutable; nav arrives via the governed seed/upgrade, never a page in navigation |

## Gate (coordinator-run, personally)

- **Backend: 1,448 passed / 0 failed / 0 skipped** — 125 test files
  in 20 chunks with private basetemps (pruned between chunks) plus
  live legs in-sequence against the local stack (PostgreSQL 16 +
  pgvector, MariaDB 10.11, Keycloak OIDC, MinIO S3): pgvector RAG,
  live DB-platform, OIDC and S3-tiering legs all green. Baseline:
  1,392 (UX gate) + 51 new inventory tests (32 module + 19 platform)
  + 5 operator/design-era growth = 1,448.
- **Web: 558 passed / 0 failed (80 files)** — baseline 551 + 7 new
  InventoryPage tests. **Production build exit 0.** `tsc --noEmit`
  clean (WS-C).
- **Case-collision audit: ZERO** — 209 files in `web/src`, 485 in
  `backend/app` + `backend/tests` (same-directory basenames modulo
  case, extension ignored).
- Gate incidents, disclosed: the first m–p attempt ran as a single
  100-file process whose basetemp filled the 512 MB `/tmp` tmpfs and
  cascaded 904 sqlite disk-I/O errors — **that chunk's numbers were
  voided**, root-caused to chunk size, and the range was re-run in
  ≤ 8-file chunks (green). Two VM restarts killed gate processes
  mid-run; completed chunks were banked from the on-disk logs and
  only the interrupted ranges re-run. `test_phase17_realtime`'s SSE
  test fails under this shell's ambient proxy env (the bracketed
  IPv6 `no_proxy` entries break this httpx version's URL parser) and
  passes 5/5 with all proxy vars stripped — an environment artefact,
  no code change; live legs were run with proxies stripped and
  `NO_PROXY=127.0.0.1,localhost`.

## Adjusted-tests register (pins updated for the new module — none weakened)

- `test_ux_shell_config.py`, `test_home_cards.py`,
  `test_depth_nav.py`, `test_realignment.py`,
  `web/src/auth/session.test.ts`,
  `web/src/auth/runtimeConfig.test.tsx`: module counts 30 → 31 and
  exact module/group lists gained `inventory` in Safety & Support,
  in seed order (seeded-payload orders after `support` shift by one).
- `test_nav_seed_consistency.py`: `MODULE_GATES` gained
  `inventory → inventory.read` (the test's own registry pattern).
- `test_seed_upgrade.py`: the legacy-payload builder skips modules
  that did not exist in the legacy eras.
- `test_page_contracts.py`: the `/home/cards` exact key set gained
  `inventory` (a real provider was added).
- `test_phase17_packgate_default.py`: `SMOKE_ROUTES` gained
  `inventory → GET /inventory/overview` (every seeded module must
  smoke under the default-on pack gate; the route serves).
- `test_inventory.py`: the unpublished-taxonomy fixture deletes the
  seeded taxonomy rows (startup seeding is now wired) to restore
  the genuine unpublished state the test pins.

## NOT RUN

- **Browser / rendered check** — the VM cannot run loopback browser
  E2E (standing limitation); Min's Air click-through after the next
  sync is the acting browser gate.
- Standing NOT RUN list otherwise unchanged: Docker runtime on this
  VM, Helm cluster install, live LLM run, live external legs
  (TEFCA/QHIN, clearinghouse, payer EDI, Surescripts, public-health
  gateway, EQA provider, fax), physical RFID readers / real devices
  (RFID here is the simulated registry event path, as in the lab
  module's pattern), mobile compilation.
- Not a gap: no `pip install` is needed for the Air sync of this
  pass — `backend/requirements.txt` is unchanged (no new imports
  beyond the existing stack).

---

## Parent addendum (2026-10-20, parent re-verification)

The coordinator's gate was re-run IN FULL by the parent and reproduced
exactly:

- Backend: 1,448 passed / 0 failed / 0 errors across 125 files in 21
  batches of <=6 files (private basetemps, /tmp pruned between
  batches, proxy vars stripped, live-stack env sourced so the live
  legs — PostgreSQL/pgvector, MariaDB, Keycloak OIDC, MinIO S3 — ran
  in sequence). Batches 1–10 (724 passed) banked from the on-disk log
  after a runtime restart killed the first parent run at batch 11;
  batches 11–21 (724 passed) re-run after the live stack was
  restarted. 724 + 724 = 1,448 — the coordinator's total, matched.
- Web: 558 passed / 80 files; production build exit 0.
- Case-collision audit over web/src: ZERO.
- Artefacts verified on disk by the parent: backend/app/inventory.py
  (2,094 lines), web/src/pages/InventoryPage.tsx (2,354 lines), the
  governed `inventory/taxonomy` config kind, `unit_cost_cents` on the
  item master with lot-level override at receiving and value-stamped
  movements (the Wave E costing prerequisite — LANDED).
- requirements.txt unchanged: the Air sync needs no pip install.

NOT RUN (unchanged): browser/rendered check on this VM — Min's Air
click-through after Air Sync Run 12 is the acting browser gate.
Release remains HELD; nothing pushed to GitHub.
