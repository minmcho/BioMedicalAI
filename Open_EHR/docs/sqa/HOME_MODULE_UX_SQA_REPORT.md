# Home & Module UX Pass — SQA Report

Pass: Home & Module UX (Min's directives of 2026-10-19: card-menu
Home, "UI look old" full visual redesign, per-module completeness,
test-detail + analysis-detail workflows, virtual plates, shared
rich-text editor, registration of operational times).
Date: 2026-10-20. Environment: staging `~/workspace/Open_EHR` only —
nothing published, nothing synced, release remains HELD.
Synthetic data only. The parked `agents/` draft and
`docs/AGENTS.md` were not read or touched by any workstream.

## Scope vs the pass brief

- **A. Card-menu Home — DONE.** Home is a role dashboard whose main
  surface is a card menu: one card per module, grouped by the
  published navigation groups, role/job-profile filtered by the
  same governed config that filtered the sidebar. Each card shows
  a one-line purpose, live indicators where a real provider exists
  (value + status text + tone + timer text), and opens the module
  in 1 click. The sidebar item list is retired as primary
  navigation; a slim top bar carries Home, global patient search
  and user context; breadcrumbs lead back home.
- **B. Full visual redesign — DONE.** A modern design language as
  tokens (spacing/radius/typography scales, soft shadows, one
  status-colour system) in `styles.css`; the theme rides the
  existing branding-config governance (see configuration list) —
  a payload without a theme renders exactly the modernized
  default look.
- **C. Module layering + operational times — DONE.** Shared
  `ModuleHeader` (one title, one-line purpose, ONE primary action,
  live-indicator slot) adopted across the audited page sets;
  stacked sections converted to in-panel tabs (P-7); multi-step
  flows render as Steppers with visible step state; workflow state
  is text + colour, never colour alone (P-3 tone sweep).
  Operational times (boarding oldest-wait, TAT/SLA breach states,
  timers) surface on Home cards and module overviews from the
  computations the system already performs.
- **D. Shared rich-text editor — DONE.** One editor component +
  one sanitizer (backend-authoritative, web mirror) replace plain
  textareas on every long-input prose surface (sweep table in
  WS2b results: 21 sites classified, 13 converted, 8 kept as
  code/data with reasons recorded).
- **E. Per-module job-completeness audit — DONE.** All 30 modules
  audited job-by-job (WS5a cluster A + WS5b cluster B tables);
  gaps found were built in-pass (see "What was built"). The two
  NAMED audit lines are closed: the **test-detail workflow**
  (order → specimen + aliquot lineage → bench steps incl. the
  plated step → result detail with ranges/delta/critical →
  review/acknowledgement trail) and the **analysis drill-down**
  (reported number → indicator definition → underlying record list
  → individual record / chart).
- **F. UX-spec reconciliation + click budgets — DONE.** Spec
  S-1…S-15 reconciled against the current tree (several Part A
  claims predated fix passes; the tree won, per item). Budgets
  verified by test: own queue ≤2 clicks, chart from any list = 1
  click (MasterDetail row action added this pass), pharmacist
  journey sign-in → verify ≤4 clicks after the landing fix.
- **G. Configuration + compliance coverage — this report** (lists
  below).
- **H. Gate — this report** (WS6, completion coordinator). The
  original coordinator's gate died mid-run (lost background result,
  then two VM restarts during the re-run); all partial output was
  discarded as UNKNOWN and the gate below is a complete fresh run.

## What was built

### Shell, cards, profiles (WS1a/WS1b/WS1c)
- Navigation regrouped to 10 groups of 2–6 modules (spec N-2) in
  the published `navigation/main` seed and its web mirror; every
  item gains a one-line `purpose` + `icon` (validated, length-
  capped; modules-only rule still rejects page/form entries 400).
- Job profiles: published `navigation/job-profiles` (existing
  kind, no new kind) mapping job role → module keys for the 11
  workforce job-catalogue roles. Effective menu = role/permissions
  resolution ∩ profile when the caller's job role resolves from
  their linked staff record; an empty intersection falls back to
  the un-profiled menu — a profile NEVER empties the menu. Patient
  role unaffected. Served by `GET /home/effective-modules`;
  consumed by the web shell through the existing runtime-config
  path (404-safe).
- Home cards: `GET /home/cards` (new `app/home_cards.py`) with 9
  providers (inbox, lab, pharmacy, beds, command, ed, safety,
  workforce, scheduling) that REUSE the indicator NAMED_SOURCES
  computations and the continuum boarding snapshot — no copied
  computations; test-pinned equal to direct source evaluation.
  Modules without a real provider are ABSENT (card shows purpose
  + Open only — no invented numbers). Patient role gets `{}`;
  unauthenticated 401.
- Top bar with global patient search (same API + matching as the
  Patients page; click/Enter opens the chart in 1 action);
  breadcrumb bar; wide-board content treatment for the six board
  pages.
- **Seeded-default upgrade path (WS1c, named item from a live
  defect):** `_seed_published` never upgraded an existing
  deployment's published nav, so Min's Air kept a Phase-15-era menu
  on current code. `upgrade_seeded_default` now recognises an
  untouched seeded default (provenance arm: system-only rows +
  seeded publish audits; fingerprint arm: canonical projection
  matches a known legacy default — eras A–D) and publishes a NEW
  version carrying the current default through the existing
  publish machinery (system actor, audited, `seed_upgrade` flag,
  supersede — never mutate in place). A customised payload is
  never overwritten: one `studio.config.seed_drift` audit per
  distinct payload hash, payload left alone. Wired for
  `navigation/main` + `branding/default`; fresh DBs seed v1 as
  before (upgrade no-ops). 8 tests pin all arms.

### Rich text (WS2a/WS2b)
- `backend/app/richtext.py` (stdlib only): `sanitize_html` —
  whitelist tags `p br h2 h3 strong b em i u ul ol li blockquote
  code a`, only attribute `a[href]` with http/https/mailto scheme
  (tab/CR/LF-obfuscated `javascript:` dropped); script/style
  content dropped entirely; all other tags stripped with text
  kept; output balanced + idempotent. `html_to_text` renders the
  plain-text form for FHIR/print.
- Wiring: documentation notes + discharge summaries sanitize on
  write (form items marked `rich: true`; `text`-type fallback
  covers cross-module governed forms); the FHIR DocumentReference
  Narrative carries tag-free plain text only. Messages: bodies
  keep the plain rendering in the FHIR payload
  (envelope-encrypted as before); the sanitized HTML rides an
  envelope-encrypted extension; markup-free bodies are stored
  byte-identically to before. **Finding: reports have NO narrative
  field** (definitions and runs carry no caller free-text) —
  recorded, nothing wired.
- Web: `RichTextEditor` (toolbar, sanitized paste, visible link
  rejection), `RichTextView` (renders sanitized HTML only),
  `richtext.ts` mirror; FormRenderer renders the editor for
  `rich: true` text items. The web mirror is stricter than the
  backend for iframe/object/embed (drops content; backend keeps
  text) — acceptable direction, server remains authoritative.

### Laboratory: virtual plates + test detail (WS3a/WS3b)
- Plate templates are governed terminology
  (`terminology/lab-plate-templates`): well layouts with
  control/calibrator/blank/patient/replicate roles and expected
  results; 2 templates seeded insert-if-absent. Plate instances
  per run (`app/lab_plates.py`): specimen-to-well assignment by
  barcode/id (409 if already on an open plate), per-well result
  import (single + bulk), and a **plate-level QC gate** — patient
  wells cannot result until every control has resulted and none
  failed; the gate is ALSO enforced in the direct result path
  (409 naming the plate). All actions audited.
- `GET /lab/orders/{id}/test-detail` composes the full chain from
  existing stores only: order summary, steps
  (ordered|collected|received|plated|in_analysis|analysed|
  verified|resulted — plated only when a plate assignment
  exists), specimen, aliquot lineage, results with reference
  ranges / delta flags / critical handling, report summary, the
  acknowledgement trail, custody stamps.
- UI: Plates tab (template picker disabled WITH the reason when
  unpublished; format-aware well grid; QC panel with gate state
  as text + tone; per-well blocked reasons) and a Test detail
  panel (Stepper + per-step stamps, lineage, results table,
  ack trail). LabPage's remaining textarea is analyser HL7/CSV
  ingest — machine data, correctly classified, not prose.

### Analysis drill-down (WS4a/WS4b)
- `GET /indicators/{key}/records` (same `studio.read` capability
  as the value endpoint; draft/unpublished key → 404): definition
  (published payload verbatim), value (the existing evaluation,
  unchanged), and the underlying records with real ids, human
  labels, and `patient_id` where patient-linked. Resolvers reuse
  each counter's own queries — for count-type indicators
  `len(records) == value` by construction (23 of 25 named
  sources). Honest exceptions: `workforce.coverage-pct` returns
  `records_supported=false` with the reason (a capped aggregate
  fill rate has no record set); `workforce.occupancy-pct` returns
  its numerator records with the reason stated; measure/cohort
  (warehouse) sources return `records_supported=false` + reason —
  never fake rows. Registry completeness is test-pinned
  (resolver keys == NAMED_SOURCES keys).
- UI: Analytics gains an Indicators tab (value + definition
  rendered readably, records table, 1-click chart open on
  patient rows); dashboard widgets backed by indicators gain the
  same in-place drill. Governed query/composition/export surface
  preserved byte-for-byte in its own tab.

### Module layering + completeness fixes (WS5a/WS5b + integration)
- `ModuleHeader` adopted on 25 pages across both clusters; the
  P-1 early-return gap (loading/error states rendering no title)
  fixed on Devices, Workforce, Reports, DataPlatform, Import.
- Tab conversions: Scheduling (5), ClinicDay (4), PopHealth (4),
  Portal (5), Billing (6 — Claims/HIM/Remittance/Denials/
  Statements/UR), BedBoard (2), UnitBoard (4), Aftercare (4),
  Devices (3). ChartPage's hand-rolled tab bar replaced by the
  shared `Tabs`.
- Named fixes: **pharmacy landing** (verification-capable roles
  land on Verification & DUR — N-9/F-10); **Command Center
  boarding** defensive render + the real census-key bug
  (`u.unit` → `u.code`) fixed; MasterDetail row action giving
  Patients + Inbox a true 1-click chart open (N-6); Documents
  doc-type select state bug + action-less status note; ImportPage
  commit disabled-reason hints; UnitBoard hard-coded ROUND_UNITS
  replaced by org-hierarchy units; ErOps hard-coded PATHWAYS
  replaced by published `terminology/continuum-ops`.
- Silent-submit fixes (P-6 class): clinic booking empty patient,
  cohort explorer NaN threshold, portal bill-pay amount, portal
  appointment start, inbox empty-text forms — all now surface
  inline errors or disable with the reason shown.
- S-1 front-desk journey re-verified as ALREADY FIXED on the
  current tree (register → book → check-in as the viewer role,
  proven by the pre-existing UxAuditFixes F-1 test, kept green
  through the tab conversions).

## Configuration-coverage list

Standing rule: every difference is Studio configuration data;
each item names its Studio surface.

| Capability | Studio surface (kind / key) | Governance |
|---|---|---|
| Nav groups, order, labels, roles, purpose, icon | `navigation/main` — Studio Navigation tab | draft → publish; published immutable; regrouping is a republish, never code |
| Job-profile module filtering | `navigation/job-profiles` — Studio Navigation tab | same; seeded defaults for 11 job roles; sites may add their own roles |
| Visual theme (density, corners, status colours, font scale) | `branding/default` payload `theme` — Studio Branding tab | server-validated enums/hex; absent theme = modernized defaults |
| Brand identity (org name, portal title, accent) | `branding/default` — Studio Branding tab | unchanged from before the pass |
| Home card tones/thresholds | indicator definitions (`indicators` kind) — Studio Dashboards/Indicators surfaces | cards read the published definition bound to each named source |
| Rich-text availability per form item | form templates (`forms` kind) — `rich: true` on text items | the sanitizer whitelist itself is a code-level security floor, deliberately NOT configurable |
| Plate templates (layouts, control roles, expected results) | `terminology/lab-plate-templates` — Studio Terminology tab | published concepts; instances are operational data |
| ErOps activation pathways | `terminology/continuum-ops` (`activationPathways`) — Studio Terminology tab | published; UI disables with reason when unreadable |
| Unit lists (rounds etc.) | `terminology/locations` org hierarchy — Studio Terminology / Units & Beds manager | the Org Placement hierarchy; no hard-coded unit lists remain in the audited set |
| Seeded-default upgrades | operates on `navigation/main` + `branding/default` via the existing publish machinery | system actor; audit actions `studio.config.create/publish` with `seed_upgrade: true`; drift on customised payloads audited as `studio.config.seed_drift` (once per payload hash) |
| Indicator definitions (what is drillable) | `indicators` kind — Studio | records endpoint serves only published definitions |

No new config kinds were created in this pass.

## Compliance coverage

- **Audit:** every config publish (including seed upgrades),
  plate action, and note/message write rides the existing
  hash-chained audit; seed-upgrade provenance is inspectable in
  the audit trail (`seeded_default` / `seed_upgrade` flags,
  `from_version`).
- **Injection/XSS:** all prose stores sanitize server-side on
  write with a strict whitelist; the FHIR narrative carries
  plain text only (no author markup crosses into FHIR); the web
  renders stored prose through the sanitizing view only.
- **Encryption at rest:** message bodies (plain + rich forms)
  remain envelope-encrypted under the Phase 10/17 KMS machinery,
  including the new rich-HTML extension (covered by the re-wrap
  path, test-pinned).
- **Access control:** home cards require authentication and return
  `{}` to the patient role; the records endpoint is gated by the
  same `studio.read` capability as indicator values; plate and
  test-detail endpoints ride `lab.read`/`lab.work`; the
  job-profile filter can only narrow a caller's role/permissions
  resolution, never widen it, and never empties a menu.
- **Honesty controls:** no invented indicator values on cards
  (absent provider = absent card data); unsupported drill-downs
  return an explicit reason instead of rows; disabled controls
  show their reason (picker, commit, complete-plate cases
  test-pinned).
- **Regulation-as-configuration:** nothing in this pass hardcodes
  a jurisdictional rule; the compliance register is unchanged by
  this pass (no new regulated surface was introduced).

## Page↔API contract pinning (WS6)

`backend/tests/test_page_contracts.py` (17 tests, new) pins REAL
payload keys for the high-traffic pages — the Command Center
lesson (a page consuming a key the API never sent, with a drifted
test fixture hiding it):
command overview `boarding` object; home cards map + tone enum;
effective-modules shape (profiled admin, patient); lab
test-detail top-level keys + step keys + the plated-step rule;
indicator value + records contracts (incl. census drill and the
measure-unsupported honesty contract); imaging worklist keys;
continuum handoffs boarding/total; pack summaries
installed-state counts; the Command Center page source string
pins; disabled-provider honest states; org-hierarchy tree keys;
workforce onboarding states; specimen chain keys; Studio config
version list keys. All 17 pass in the gate (in-chunk and solo).

## Case-collision audit (WS6 named check)

Scan over `web/src` for files in the same directory whose
basenames collide modulo case (both full-filename and
extension-stripped forms — the IdleLock/idleLock class that
white-screened the Mac): **ZERO collisions.** `idleLock.ts` was
not reintroduced; `sessionSecurity.ts` stands.

## Gate (WS6 completion coordinator, own runs, this environment)

Method: suites run SEQUENTIALLY, never overlapped; backend as 16
sequential alphabetical chunks (123 test files, 8 per chunk) with
a per-chunk private basetemp deleted between chunks; every chunk
log + summary line on disk at `~/workspace/uxpass_work/gate/`.
Live stack up for the whole backend gate (PostgreSQL 16.15,
MariaDB 10.11.14, MinIO, Keycloak 26.7.0) with the Org-gate env
mapping (test URLs, OIDC creds, S3 endpoint; ALL proxy variables
stripped, NO_PROXY/no_proxy clean).

- **Backend: 1,392 passed / 0 failed / 0 skipped** (baseline
  1,285 at the Org pass → +107, principally the eight new UX test
  files: test_ux_shell_config, test_home_cards (12),
  test_seed_upgrade (8), test_richtext (23), test_lab_plates +
  test_lab_test_detail (16), test_indicator_records (9),
  test_page_contracts (17)).
- **Live legs run in-sequence and passed:** Keycloak OIDC live
  tests (test_phase17_oidc.py), MinIO S3 tiering drill
  (test_phase17_tiering_s3.py), pgvector RAG
  (test_phase16_rag_pgvector.py), live DB platform legs
  (test_phase17_dbplat_live.py, test_platform_db_live.py).
- **Web: 533 passed / 0 failed (77 test files)** — reproduces
  WS5a's full-suite figure on the settled tree. Production build
  exit 0 (tsc clean; the >500 kB chunk warning is pre-existing).
- **ADK: 8 passed. Go: all packages ok. Cargo: 43 passed.**
- Gate incidents, recorded honestly: the first WS6 attempt and
  one re-run were interrupted by VM restarts (two chunks'
  outcomes discarded as UNKNOWN and re-run — c03, c14); one chunk
  (c10) failed 4 live OIDC tests because the gate harness had
  left sandbox `all_proxy`/lowercase `no_proxy` (IPv6 entries
  httpx cannot parse) in the environment — harness defect, not
  product code; env fixed, chunk re-run green (68 passed).
  A workspace basetemp proved ~10x slower than /tmp on this VM
  (btrfs+zstd) and was replaced before the counted run.

## Adjusted-tests register (consolidated; no assertion weakened)

Backend:
- `test_depth_nav.py` (3 tests) + `test_realignment.py` (order
  list): pre-regroup group/order pins updated to the ledger
  regroup — group/order assertions only, at the same strength.
- `test_home_cards.py`: the "unprofiled admin" test's premise was
  wrong (dev-admin IS staff-linked, job role executive);
  replaced by an executive-profiled test + a genuinely-unlinked
  account test carrying the original assertions.
- `test_richtext.py`: one own-test expectation corrected
  (plainText includes section titles — intended prose rendering).

Web (tab conversions: navigation clicks inserted, or a text
query disambiguated to a heading role — assertions unchanged):
SchedulingAccess, PopHealthPage, PortalPage, PortalTelehealth,
AccessServices (portal describe), BedBoardPage, FrontDoor,
ContinuumPages (UnitBoard/Aftercare), DevicesRfid,
Phase14Pages (Devices), BillingPage7, PharmacyPage,
BarcodePrint.
- `session.test.ts`: per-role key ORDER updated to the regroup
  (membership byte-identical); `runtimeConfig.test.tsx`: seed
  order + theme/job-profile expectations extended.
- `Phase10Tabs.test.tsx` / `Phase16StudioTabs.test.tsx`: driving
  lines only (contentEditable editors are driven by innerHTML +
  input events, not textarea change events).
- `RadiologyPage.test.tsx`: one test ADDED (rich-editor drafting).
- `PharmacyOps.test.tsx`: one test REPLACED — its premise was the
  exact landing behaviour the named N-9 fix removes; replaced by
  three stronger tests (verifier landing, non-verifier landing,
  deep-link wins). Two stub clients gained
  `pharmacyVerificationQueue`/`durQueue` stubs (the new landing
  tab mounts the panel that calls them).

## Design decisions & interpretations (recorded)

- ED board vs ER operations: WS5b's evidence shows two views of
  one department's one queue and recommends MERGE into one ED
  module with two tabs (fallback: rename both). NOT done in-pass
  — nav structure is governed config and the merge is an
  unratified S-15 structural item; carried as DEFERRED for Min's
  ratification. The same N-4 family: PopHealth Warehouse ↔ Data
  Platform overlap and Aftercare ↔ Support Hospital-at-Home
  overlap — left in place, recorded.
- DashboardsTab (Studio preview) has no chart opener: drill works
  there, patient rows render as text — accepted by WS4b and the
  coordinator; App + Home hosts pass the opener.
- DocumentsPage intake bytes now carry sanitized HTML (WS2b
  judgment call; the intake treats them as document text as
  before) — confirmed at integration.
- KnowledgeTab's second textarea is the RAG query, not an article
  body — kept as a textarea (markup would corrupt retrieval).
- FormRenderer reads the `rich` flag through a narrow cast
  (`FormItem` in api/types.ts has no `rich` field yet) — cosmetic
  typing debt, no behaviour gap.

## NOT RUN / named partials

- **Real-browser click-through E2E: NOT RUN** (VM limitation —
  hosted tooling refuses loopback; standing hazard). The Air is
  the acting browser gate; Min's click-through of the new Home is
  the outstanding evidence. Toolbar formatting commands
  (execCommand) are jsdom-untestable and rest on the same
  browser pass.
- Existing deployments with a CUSTOMISED published navigation do
  not auto-upgrade (by design): they receive a drift audit note;
  the new menu arrives on their next republication.
- Inbox Messages still takes a free-text patient id (a search
  picker needs a shared component — recorded, not built).
- Scheduling referral-service/slot-type selects remain static
  mirrors of terminology (server-validated) — drift risk
  recorded.
- Form-level (not field-level) validation persists on several
  create forms (Surgery, Research, Support) — a product-wide
  pattern; the pass fixed the silent-submit cases it found.
- Live LLM, Docker/Kubernetes runtime, mobile compiled builds,
  live external legs: NOT RUN (standing list, unchanged by this
  pass).

## Parent verification

Pending — the parent re-runs the full gate after this report;
nothing in this pass counts as banked until then.

## PARENT ADDENDUM (2026-10-20, parent re-verification)

Parent gate run independently against the live stack (PostgreSQL 16.15,
MariaDB 10.11.14, MinIO, Keycloak 26.7.0), artefacts in
~/workspace/uxverify/ (run_gate.sh, summary.txt, per-chunk logs).
Totals reproduce the coordinator's gate EXACTLY:

- Backend: **1,392 passed / 0 failed / 0 skipped** — 123 test files in
  16 sequential alphabetical chunks, every chunk exit=0; the sum was
  re-added from the per-chunk lines (117+77+106+109+79+91+70+69+58+65+
  53+88+105+91+113+101 = 1,392). Live legs (Keycloak OIDC, MinIO S3
  tiering, pgvector RAG, DB-platform) ran in-sequence inside the
  chunks with the stack up.
- test_page_contracts.py solo re-run: **17 passed**, exit=0.
- Web: **533 passed (77 files)**, production build exit 0.
- ADK: **8 passed**. Go: **5 packages ok**. Cargo: **43 passed**.
- Case-collision audit over web/src: **ZERO** (the idleLock class is
  now a standing gate check).

Two VM restarts killed parent gate runs mid-chunk (after chunks 00–02
and 00–10 respectively); killed chunks were treated as UNKNOWN and
re-run under the driver's banked-chunk resume — no result was carried
forward unverified.

Disposition: Home & Module UX pass **DONE + PARENT-VERIFIED**
(2026-10-20). Outstanding evidence remains the Air click-through of
the new card Home (browser E2E is NOT RUN on this VM by standing
hazard); the ED/ER merge stays DEFERRED for Min's ratification.
Next in programme: Air sync of this verified tree, then the
Inventory & Materials pass.
