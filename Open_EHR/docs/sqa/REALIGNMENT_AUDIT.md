# Realignment Audit — existing OpenCare EHR vs the full-system roadmap + "configure, don't fork"

- **Date:** 2026-10-16
- **Scope:** shipped Phases 0–4B code in `~/workspace/Open_EHR` — `backend/app/*.py`,
  `backend/packs/`, `backend/seed/`, `web/src/**`. Mobile, SMART app and the dbt
  project are placed in the domain map only, per the audit brief.
- **Yardsticks:** (A) `docs/FULL_SYSTEM_ROADMAP.md` §2 domain inventory and §4–§5
  specialty/profile plan; (B) roadmap §6, the standing rule: *"configure, don't
  fork — every difference between settings is System Studio configuration data."*
- **Method:** read-only code audit. Every verdict cites file:line evidence that
  was actually read. No test suites were run. `NOT CHECKED` is used where a
  claim could not be grounded.

## Executive summary

The backend's *clinical* configuration story is genuinely strong: the lab
catalogue, fee schedule, formulary, modalities, OR rooms, cohort definitions,
claim/lab/ED/surgery/ADT state machines (server-side) and the DICOM viewer
link are all read from published Studio config at request time, most with
seeded published v1 payloads. The realignment problem is concentrated in
three places:

1. **The web shell consumes no configuration at runtime.** Navigation,
   branding and the UI's capability checks are hardcoded TypeScript that
   *mirrors* the config defaults. A configurator can publish new navigation
   or branding and nothing anywhere changes. (§2a, §2b, §2c-iv)
2. **The permissions config governs only half the backend gates.** The
   capability-checked write paths honour it; every read gate
   (`STAFF_READ`, `POPHEALTH_ROLES`, Studio's own `STUDIO_ROLES`/`READ_ROLES`)
   is a hardcoded role tuple — including the `studio.manage` capability,
   which is defined in the matrix and enforced *nowhere*. (§2c)
3. **Pack enablement gates nothing.** The pack lifecycle (install → approve →
   attach → enable) is real bookkeeping with a hash-chained audit, but no
   router, endpoint or page ever reads installation status: all clinical
   routes are live from first boot whether or not any pack was installed.
   The docs claim otherwise. (§2h, §4.3)

Everything else is duplication and drift: each state machine exists up to
three times (config seed, Python fallback, TypeScript mirror), and several
safety/policy rules the roadmap counts as "configuration" (QC gating rule,
BCMA rights, ESI range, exportable claim states, governed states) are code
constants sitting next to the config they accompany.

---

## 1. Feature-to-domain map

Domains are roadmap §2 rows. "Surface" = backend router (registered in
`backend/app/main.py:79–94`) and/or web page (`web/src/pages/`).

| # | Feature (surface) | Roadmap §2 domain | Placement verdict |
|---|---|---|---|
| 1 | Auth: dev tokens + OIDC/JWKS verification (`backend/app/security.py`), `LoginPage` | Identity & access | ✅ aligned |
| 2 | Hash-chained audit log (`backend/app/audit.py`; read via `GET /studio/audit`, `studio.py:138–155`) | Identity & access (§2 row lists audit) / Platform | ✅ aligned; the only audit *read* surface lives under the Studio router |
| 3 | Generic FHIR store + search + patient scoping (`/fhir`, `fhir.py`) | Clinical documentation (the record itself) | ⚠️ flag F1 |
| 4 | Appointment ADT transitions (`POST /fhir/Appointment/{id}/transition`, `fhir.py:459–465`) + `SchedulingPage` | Registration & scheduling **and** ADT & bed management | ⚠️ spans two §2 domains; the §2 "ADT" row is otherwise only ED-admit linkage — acceptable, but the split should be named |
| 5 | Results inbox (`GET /fhir/inbox`, `fhir.py:468–475`; `InboxPage`) | Laboratory (results review) / clinical documentation | ✅ aligned (cross-module read over the shared results bundle, `fhir.py:371–387`) |
| 6 | QuestionnaireResponse create/read/search (`fhir.py:519–540`) | Clinical documentation (forms) | ⚠️ server-side only — **no web page ever submits or renders one** (grep: zero `QuestionnaireResponse` uses in `web/src` outside Studio/tests) |
| 7 | Patient portal reads (`/portal/my-results`, `fhir.py:543–551`; `PortalPage`) | Patient engagement | ⚠️ portal router lives inside `fhir.py`; portal page reads results/conditions/meds only (`PortalPage.tsx:23–25`) — patient *bills* are read on the Billing page, not the portal (see §4.6) |
| 8 | Patient list + chart viewer (`PatientsPage`, `ChartPage`) | Registration & scheduling (index) + Clinical documentation (read-only chart) | ✅ aligned; chart is a `PageKey` with no nav entry of its own (`session.ts:64–77`) — reached from Patients, consistent with the modules-only menu rule |
| 9 | Lab LIS (`/lab`, `lab.py`; `LabPage`) | Laboratory | ✅ aligned |
| 10 | HL7 v2 ORU + CSV ingest (`POST /lab/ingest`, in `lab.py`) | Laboratory **and** Interoperability | ⚠️ dual-domain; §2 Interoperability counts it — implemented inside the lab module, not an integration surface |
| 11 | Warehouse: outbox → ETL → OMOP (`outbox.py`, `etl.py`, `warehouse.py`; dbt in `warehouse/dbt/`) | Analytics & quality | ✅ aligned |
| 12 | Population health registries + explorer (`/pophealth`, `pophealth.py`; `PopHealthPage`) | Analytics & quality | ✅ aligned |
| 13 | Warehouse status + ETL trigger (`/warehouse`, router defined **inside `pophealth.py:33`**, trigger at `pophealth.py:337`) | Analytics & quality (operations) | ⚠️ wrong file: warehouse ops routes live in the pophealth module (backlog #16) |
| 14 | Billing / claims + 837P export (`/billing`, `billing.py`; `BillingPage`) | Revenue cycle | ✅ aligned |
| 15 | Pharmacy: formulary, dispense, BCMA-lite (`/pharmacy`, `pharmacy.py`; `PharmacyPage`) | Medication closed loop | ✅ aligned (partial per §2: no pharmacy-verify step, no MAR — code agrees) |
| 16 | ED board (`/ed`, `boards.py`; `EdBoardPage`) | ED | ✅ aligned |
| 17 | Radiology + DICOMweb (`/radiology`, `/dicomweb`, `boards.py`; `RadiologyPage`) | Radiology / imaging (+ Interoperability for DICOMweb) | ✅ aligned |
| 18 | Surgery board (`/surgery`, `boards.py`; `SurgeryBoardPage`) | Surgery & anaesthesia | ✅ aligned (no anaesthesia record — §2 says PARTIAL, code agrees) |
| 19 | Module packs lifecycle (`/studio`, `studio.py`; manifests in `backend/packs/`) | Platform / System Studio | ⚠️ lifecycle exists but gates no runtime surface (§2h) |
| 20 | Studio config engine (`/studio/config`, `studio_config.py` + `configstore.py`) | System Studio | ✅ aligned |
| 21 | System Studio visual module (`StudioPage` + `web/src/pages/studio/*`, 11 tabs, `StudioPage.tsx:413–472`) | System Studio | ✅ aligned (and newer than the roadmap §2 row claims — §4.2) |
| 22 | Seed / synthetic data (`backend/seed/seed.py`, `synthea_bundle.json`, `lab_demo.py`) | — (test fixture, no §2 domain) | ✅ correctly outside the product surface |
| 23 | SMART on FHIR companion (`smart-app/`) | Interoperability | ✅ placement only, per brief |
| 24 | Native mobile sources (`mobile/`) | Patient engagement + clinical access | ✅ placement only, per brief; **config consumption NOT CHECKED** |
| 25 | Parked ADK draft (`agents/`) | — (explicitly not part of the system, `PHASES.md`) | ✅ correctly excluded |

### Flags

- **F1 — the FHIR router is a cross-domain monolith.** `fhir.py` alone serves
  the chart (documentation), scheduling/ADT, the results inbox, form-response
  validation and the patient portal. Defensible for a generic resource store,
  but the §2 domain boundaries are invisible in the code layout; later phases
  will keep accreting here unless new domains get their own modules the way
  lab/billing/boards did.
- **F2 — naming.** The nav label is now correct: `session.ts:148` reads
  `"System Studio"`. The stale name survives in the docs (`PHASES.md:43`,
  "Studio packs UI" — §4.4) and in code identifiers (`StudioPacksSection`,
  `StudioPage.tsx:458`). No user-facing surface still says "Studio packs".
- **F3 — duplicated capability logic (three copies).** The role/capability
  model exists as (i) Python defaults `configstore.py:33–60`, (ii) a
  TypeScript mirror for the Studio matrix editor
  `web/src/pages/studio/models.ts:438–453` (verified identical to the Python
  defaults, cell for cell), and (iii) a *different, page-granularity* model
  in `web/src/auth/session.ts:80–130` plus per-page role literals (§2c-iv).
  Three copies of one policy is the largest drift risk in the codebase.
- **F4 — duplicated state machines (up to three copies each).** ADT map:
  seed source `configstore.py:70–79`, fallback `fhir.py:76–85`, TS mirror
  `web/src/api/adt.ts:5–14`. Lab/claim/ED/surgery machines: Python fallback +
  TS mirror each (§2d). The copies agree today; nothing but discipline keeps
  them agreeing.
- **F5 — no feature found doing another feature's job**, with one near-miss:
  patient results are reachable via both the portal (`/portal/my-results`)
  and generic FHIR search with patient scoping (`fhir.py:329–348`). Both
  funnel through the same scoping + visibility code — one job, two doors,
  not duplicated logic.

---

## 2. Configuration-coverage audit

Verdicts: **CONFIG-DRIVEN** (published Studio config is read at runtime and
changes behaviour), **PARTIAL** (config drives part; the rest is hardcoded —
the split is stated exactly), **HARDCODED** (whatever config exists, runtime
never reads it).

### (a) Web navigation — **HARDCODED**

- The shell's menu is a TypeScript constant: `NAV` at
  `web/src/auth/session.ts:137–150`, filtered by the hardcoded `canAccess`
  (`session.ts:80–130`) in `navItemsFor` (`session.ts:152–154`) and rendered
  by `App.tsx:33–58`. The route guard uses the same hardcoded function
  (`App.tsx:27–31`).
- Nothing outside the Studio editor fetches navigation config: a tree-wide
  grep for `navigation` in `web/src` finds only `api/types.ts` (the kind
  union), `StudioPage.tsx` (the tab) and `pages/studio/*`. `AuthContext.tsx`
  creates the API client and never loads config.
- The backend validates navigation payloads (modules-only rule →
  HTTP 400, `configstore.py:291–314`) and can store/publish them
  (`studio_config.py`), but **no default navigation config is ever seeded**:
  `ensure_default_configs` (`studio_config.py:305–331`) seeds only
  `workflow/appointment-adt`. A fresh deployment has no published navigation
  config at all, and nothing would read it if it did.
- Even the Studio editor duplicates the module list by hand
  (`KNOWN_MODULES`, `web/src/pages/studio/NavigationTab.tsx:15–18`).

**Net effect:** the Navigation tab is a write-only surface. Publishing a new
menu changes no menu anywhere. This directly contradicts roadmap §6's claim
(§4.1) and the Studio page's own copy (§4.8).

### (b) Branding — **HARDCODED**

- Org name is a literal in two shells: `App.tsx:37`
  (`<h1 className="brand">OpenCare</h1>`) and `LoginPage.tsx:9`
  (`<h1>OpenCare</h1>`).
- Primary colour is a CSS literal: `--accent: #0b6e4f` at
  `web/src/styles.css:6`; the config's `primaryColor` is never written to
  any CSS variable or style.
- The branding validator exists (`configstore.py:317–326`) and, like
  navigation, **no branding config is seeded** by
  `studio_config.ensure_default_configs`.
- The only readers of published branding are inside Studio itself: the
  Overview tab's org-profile summary (`OverviewTab.tsx:31–44`) and the
  Branding tab's own live preview (test-asserted at `StudioPage.test.tsx:220`).
  `portalTitle` appears nowhere outside `api/types.ts`, the Studio tabs and
  test fixtures (grep evidence) — the patient portal never displays it.

**Net effect:** rebranding a deployment today means editing TSX/CSS and
rebuilding — precisely the fork §6 forbids.

### (c) Permissions — **PARTIAL** (backend writes: config-driven; backend reads + Studio's own gates + all web UI: hardcoded)

- **(i) The merge mechanism is real.** `role_allowed`
  (`configstore.py:148–156`) reads the published `permissions/role-matrix`
  config and lets it override individual (capability, role) cells over
  `CAPABILITY_DEFAULTS` (`configstore.py:33–60`). `require_capability`
  (`security.py:192–210`) enforces it in the API dependency.
- **(ii) Capability-checked paths honour it:** FHIR writes + inbox
  (`fhir.py:442, 465, 475`), lab order/work/verify (`lab.py:395, 488, 747,
  856, 966, 995, 1054`), billing manage (`billing.py:343, 515`) and
  billing.view via a direct `role_allowed` call (`billing.py:464–477`),
  pharmacy dispense/administer (`pharmacy.py:247, 366`), boards work/verify
  (`boards.py:386, 462, 525, 575, 660, 752, 809, 1010, 1171, 1209`).
- **(iii) But the defaults are the ceiling.** `CAPABILITY_DEFAULTS` is a
  hardcoded dict, and the validator *rejects* any capability not already in
  it (`configstore.py:333–334`). Config can flip cells; it cannot express a
  new capability. Site variation is served; capability invention still
  requires a code release (defensible — but it is a code-defined surface,
  not pure configuration).
- **(iv) `studio.manage` is defined and enforced nowhere.** It sits in the
  defaults (`configstore.py:36`) and in the Studio matrix UI, but no
  endpoint depends on it (grep: zero `require_capability("studio.manage")`).
  Studio's own APIs gate on hardcoded tuples instead: `STUDIO_ROLES` +
  `require_roles` throughout `studio.py` (9 uses) and `studio_config.py`
  (12 uses), with `READ_ROLES` hardcoded at `studio_config.py:30`.
  Publishing a `studio.manage` override changes nothing.
- **(v) Every read gate is a hardcoded tuple.** Lab/pharmacy/boards queue
  reads use `STAFF_READ = (admin, configurator, clinician, viewer)`
  (`lab.py:60`, `pharmacy.py:45`, `boards.py:65`); pophealth uses
  `POPHEALTH_ROLES = (admin, clinician)` (`pophealth.py:36`) and the ETL
  trigger is admin-only (`pophealth.py:337`); the portal is role-checked
  directly (`fhir.py:543–551`). A site cannot, say, grant the viewer
  pophealth access or revoke configurator lab reads via config.
- **(vi) The web UI never reads the matrix.** Page access is
  `session.ts:80–130`; action buttons are per-page role literals —
  `LabPage.tsx:53–54`, `PharmacyPage.tsx:19–20`, `EdBoardPage.tsx:34`,
  `RadiologyPage.tsx:19–20`, `SurgeryBoardPage.tsx:35`,
  `BillingPage.tsx:45`, `PopHealthPage.tsx:159`. These mirror the *defaults*.
  After a published override the server and the UI disagree: buttons appear
  that will 403, or vanish for users the server would now allow. (The server
  remains the enforcer, so this is a UX-integrity defect, not a security
  hole — the Permissions tab's own comment says exactly this,
  `PermissionsTab.tsx:8–13`.)

### (d) Workflow states

Common pattern, stated once: each machine's server loader reads the
published workflow config and falls back to a hardcoded copy of the same
map; the schema (`configstore.py:230–256`) permits per-transition `roles`
and `action`, but **every loader reads only `from`/`to`** — role-restricted
transitions written in Studio are silently unenforced in all five machines.

| Machine | Server verdict | Evidence | Web verdict |
|---|---|---|---|
| ADT (appointments) | **CONFIG-DRIVEN** w/ hardcoded fallback | loader `fhir.py:404–421`; fallback `fhir.py:76–85`; seed source duplicated again at `configstore.py:70–79`; seeded published v1 `studio_config.py:305–331` | **HARDCODED** mirror `web/src/api/adt.ts:5–14`, consumed `SchedulingPage.tsx:104` |
| Lab order flow | **CONFIG-DRIVEN** w/ fallback | loader `lab.py:212–228`; fallback `lab.py:72–82`; seeded `lab.py:177–185` | **HARDCODED**: transition buttons `LabPage.tsx:23–37`, status list `:39–40`. The page *does* fetch `/lab/catalog`, which returns the published workflow payload (`lab.py:1117–1131`), but uses it only for orderable concepts (`LabPage.tsx:67–68`) and to *display* the workflow version (`:98–100`) — the buttons ignore it |
| Claim lifecycle | **CONFIG-DRIVEN** w/ fallback | loader `billing.py:201–215`; fallback `billing.py:64–72`; seeded `billing.py:145–152` | **HARDCODED** `NEXT_ACTIONS` `BillingPage.tsx:16–24` |
| ED flow | **CONFIG-DRIVEN** transitions w/ fallback; board structure **HARDCODED** | machine `boards.py:225–241`; fallback `boards.py:89–96`; board grouping iterates the constant `ED_ACTIVE_STATES` (`boards.py:80`, used `:443`) — a config-added state never gets a board column; governed exits hardcoded (below) | **HARDCODED** columns + next-state map `EdBoardPage.tsx:19–30` |
| Surgery flow | **CONFIG-DRIVEN** w/ fallback | machine `boards.py:225–241`; fallback `boards.py:97–104` | **HARDCODED** `NEXT_STATES` `SurgeryBoardPage.tsx:25–33` (its comment concedes the server is the enforcer) |

Hardcoded state sets riding alongside the config machines:

- Lab `GOVERNED_STATES = ("resulted", "verified", "released")` (`lab.py:84`)
  and the specimen status mirror set (`lab.py:85–86`) — a site that renames
  a governed state in config desynchronises the dedicated endpoints.
- ED `ED_GOVERNED_STATES = ("departed", "admitted")` (`boards.py:83`,
  enforced `boards.py:470`).
- Billing `EXPORTABLE_STATES` (`billing.py:57`), mirrored in the web at
  `BillingPage.tsx:29`.
- Billing's **one-appeal rule is code**, not lifecycle config: note required
  + `_appeal_count(body) >= 1` → 409 (`billing.py:536–546`). The config can
  add/remove the `denied → appealed` edge; it cannot change "once".

### (e) Terminology

| Item | Verdict | Evidence |
|---|---|---|
| Lab catalogue (panels, analytes, units) | **CONFIG-DRIVEN** | read from published `terminology/lab-catalog` at `lab.py:193–199`; **no fallback** — unpublished catalogue → 503; seeded `lab.py:177–185`. Order placement validates against it (`lab.py:395–425`) |
| Reference ranges + critical thresholds | **CONFIG-DRIVEN** | `interpret_value` consumes the concept's `referenceRange` from config (`lab.py:309–324`) |
| Fee schedule | **CONFIG-DRIVEN** | `terminology/fee-schedule` (`billing.py:167–199`); unpriced code → $0 line flagged, never dropped (rule itself is code, `billing.py:184–199`) |
| Formulary (+ `stockTracked`, `initialStock`) | **CONFIG-DRIVEN** | `pharmacy.py:130–137`; 503 if unpublished; stock seeded from config (`pharmacy.py:103–125`) |
| Imaging modalities, OR rooms | **CONFIG-DRIVEN** | `boards.py:243–256`; consumed `boards.py:653` (modalities) and `:1003` (rooms); 503 if unpublished |
| ESI validation (1–5) | **HARDCODED** | literal range check in the triage transition, `boards.py:489–493`. ESI is a fixed clinical standard, so a constant is defensible — but it is not terminology config and a site cannot relabel or extend acuity levels |
| OMOP `concept_map` | **PARTIAL** | data-shaped and DB-resident, but seeded from a code constant `CONCEPT_MAP_SEED` (`warehouse.py:194`, loaded by `ensure_concept_map`, `warehouse.py:288–298`) in the *warehouse* DB — not Studio terminology config; changing a mapping means a code change, not a publish |

### (f) Integration config (radiology viewer) — **CONFIG-DRIVEN** ✅

The one integration key that exists is consumed end-to-end:
`_viewer_url` reads published `integration/dicom-viewer` → `viewer.baseUrl`
(`boards.py:259–273`), the study list embeds the built `viewer_url`, and the
page renders it or says "viewer not configured" (`RadiologyPage.tsx:73–78`).
Two caveats: the seeded default points at the **public OHIF demo**
(`boards.py:190–204`) — a deployment that never opens Studio sends its users
to a third-party demo viewer; and the Integration tab happily edits any
integration key while `dicom-viewer` is the only key any code reads.

### (g) Thresholds & safety gates

| Gate | Verdict | Evidence |
|---|---|---|
| QC gating | **PARTIAL** — analyte *set* is data; the *rule* is code | Blockers are computed per report from its analytes (`lab.py:347–372`); analytes/ranges come from config (§2e). But the policy — "latest run failed blocks; **no QC history does not block**" — is a code comment + code (`lab.py:351–355`), and there is no config for it: a site cannot require QC freshness (e.g. within 24 h), cannot make missing QC block, cannot exempt an analyte except by removing it from the catalogue |
| Small-cell suppression | **CONFIG-DRIVEN** per cohort, hardcoded default | threshold read from the cohort payload (`pophealth.py:157`), defaulting to `DEFAULT_SMALL_CELL = 5` (`pophealth.py:37`); the display string is correctly derived from the threshold (`pophealth.py:139–142`). Age bands are hardcoded (`pophealth.py:38–39`) |
| BCMA rights | **HARDCODED** | the three checks, their order and their 422 messages are literals in `pharmacy.py:367–448` (checks list literal at `:445–446`). Which rights are enforced is not selectable in config; right-dose/right-time are not implemented at all (admitted in `PHASES.md`, Phase 4A row) |

### (h) Two further surfaces the brief's logic implicates

- **Forms — PARTIAL.** Server-side validation is config-driven: a
  QuestionnaireResponse is accepted only against a *published* form, with
  required items enforced (`fhir.py:488–516`). But no clinician- or
  patient-facing page renders a published form or submits a response (§1 #6),
  so in the shipped product forms are config that validates an API with no
  UI consumer. The Forms Designer tab edits data the chart never uses.
- **Pack enablement — HARDCODED-OFF (governance without enforcement).**
  The lifecycle state machine is real (`studio.py:26–31`, audited), and both
  manifests declare every endpoint `"enabled": false`
  (`backend/packs/clinical-core.pack.json`, `backend/packs/lab.pack.json`).
  But nothing reads it: all routers are registered unconditionally
  (`main.py:79–94`), and a grep for `PackInstallation`/`InstallationStatus`
  outside `studio.py` finds only the model definitions (`models.py:22, 78–83`).
  The lab module serves identically whether the lab pack is uninstalled,
  drafted, or enabled. (For the *shipped* modules this is arguably the right
  product behaviour — but then the docs must stop claiming the opposite; §4.3.)
- **Cohort/registry editing — CONFIG-DRIVEN, editor-poor.** Registries are
  published `cohort` config consumed at `pophealth.py:234–255`, but the
  Studio visual module has no cohort tab (tab list `StudioPage.tsx:413–425`);
  registries are editable only through the Advanced JSON editor. `cohort`
  is in the kind registry (`configstore.py:22`) and the web kind list
  (`web/src/pages/studio/helpers.ts:29–38`) — only the visual editor is missing.
- **Deployment variation that *is* config, but not Studio config:** CORS
  origins via `CORS_ORIGINS` env (`main.py:57–70`), dev tokens via
  `OPENEHR_DEV_TOKENS` env (`security.py:27–47`) — noted for completeness;
  env is a legitimate surface for deployment secrets/origins. The web's dev
  account list, however, is a hardcoded mirror of the server defaults
  (`session.ts:22–28`) that env changes on the server silently desync.

---

## 3. Realignment backlog

Sequenced: (A) runtime config consumption in the shell → (B) backend gate
extraction → (C) hardcoded workflow/threshold extraction → (D) naming/domain
tidy. "Contract" = public API shape/behaviour; "tests" = existing test
expectations that would need to change.

### A. Make the shell consume published config

1. **Shell renders navigation from published config.** Fetch
   `GET /studio/config/navigation/effective` after sign-in (endpoint exists,
   `studio_config.py:96–103`; client wrapper exists, `client.ts:343–345`);
   map items `{module, label, roles, visible, order}` onto `PageKey`s,
   ignore unknown modules, and fall back to today's built-in `NAV` when no
   config is published or the fetch fails. Files: `web/src/App.tsx`,
   `web/src/auth/session.ts`, `web/src/auth/AuthContext.tsx`.
   Risk: **med** (nav is the shell's spine; fallback discipline is the whole
   job). Contract: none. Tests: `session.test.ts` nav expectations become
   fallback-path expectations; new tests for config-driven nav.
2. **Shell applies published branding.** Org name in the sidebar + login,
   portal title on the portal/login, `primaryColor` written to the
   `--accent` CSS variable at runtime. Files: `web/src/App.tsx`,
   `web/src/pages/LoginPage.tsx`, `web/src/pages/PortalPage.tsx`,
   `web/src/styles.css`. Risk: **low**. Contract: none. Tests: add render
   assertions; existing Studio tests untouched. (Pairs with #5 so a default
   exists.)
3. **Web capability checks resolve from the published permissions matrix.**
   Fetch `permissions/effective/role-matrix` at sign-in; merge over defaults
   exactly as `role_allowed` does (reuse `parsePermissions`,
   `web/src/pages/studio/models.ts:458–476`); derive `canAccess` and the
   per-page `canWork`/`canVerify`/`canManage` flags from the resolved
   matrix instead of role literals. Files: `web/src/auth/session.ts`,
   `web/src/pages/{LabPage,PharmacyPage,EdBoardPage,RadiologyPage,SurgeryBoardPage,BillingPage,PopHealthPage}.tsx`.
   Risk: **med-high** (every page's buttons; the server stays the enforcer,
   so failure mode is cosmetic mismatch, not privilege escalation). Caveat:
   `READ_ROLES` excludes `patient` (`studio_config.py:30`), so the patient
   shell must tolerate a 403 and use defaults. Contract: none. Tests:
   page tests that assert button visibility per role need a config fixture.

### B. Extract the remaining backend gates into the capability surface

4. **Replace hardcoded read/studio gates with capabilities.** Add
   `lab.read`, `pharmacy.read`, `boards.read`, `pophealth.view`,
   `pophealth.run`, `studio.read` to `CAPABILITY_DEFAULTS` with defaults
   equal to today's tuples; swap `STAFF_READ` (`lab.py`, `pharmacy.py`,
   `boards.py`), `POPHEALTH_ROLES` + admin-only ETL (`pophealth.py`), and
   `STUDIO_ROLES`/`READ_ROLES` (`studio.py`, `studio_config.py`) for
   `require_capability(...)` — finally enforcing `studio.manage`, which
   already exists. Files: `backend/app/configstore.py`, `lab.py`,
   `pharmacy.py`, `boards.py`, `pophealth.py`, `studio.py`,
   `studio_config.py`, mirror in `web/src/pages/studio/models.ts`.
   Risk: **med** (a wrong default silently changes who gets 403; defaults
   must be copied tuple-for-tuple). Contract: no URL/shape change; behaviour
   changes only where a site publishes overrides — the point of the rule.
   Tests: role-matrix tests in `test_studio_config.py` and the per-module
   403 tests must be re-checked line by line.
5. **Seed default navigation + branding configs at startup**, like every
   other kind: `navigation/main` mirroring today's `NAV` (modules-only) and
   `branding/default` (`orgName "OpenCare"`, portal title, `#0b6e4f`), via
   `studio_config.ensure_default_configs`. Files:
   `backend/app/studio_config.py`, `backend/app/configstore.py` (default
   payload builders). Risk: **low**. Contract: additive only. Tests: seed
   idempotency tests alongside the existing ADT seed tests.

### C. Extract hardcoded workflow/threshold residue

6. **Workflow machines honour transition `roles`** — or narrow the schema.
   Either the five loaders (`fhir.py`, `lab.py`, `billing.py`, `boards.py`)
   return role restrictions and the transition endpoints enforce them
   (403 on a role-blocked transition), or `validate_workflow`
   (`configstore.py:230–256`) drops the `roles`/`action` fields it currently
   accepts and ignores. Pick one; the silent middle is the defect.
   Risk: **med**. Contract: error-code change for role-blocked transitions
   if enforced. Tests: transition tests per module.
7. **Web pages render transitions from the published workflow payloads.**
   Scheduling from `workflow/appointment-adt` (effective endpoint exists);
   LabPage from the workflow payload `/lab/catalog` *already returns*
   (`lab.py:1126–1130`); ED/Surgery/Billing pages via the effective
   endpoints or by embedding the machine in board/claim payloads. Delete
   the TS mirrors (`web/src/api/adt.ts` map, `LabPage.tsx:23–40`,
   `EdBoardPage.tsx:19–30`, `SurgeryBoardPage.tsx:25–33`,
   `BillingPage.tsx:16–29`), keeping label maps as pure UI copy. Fall back
   to built-in defaults when config is unreadable. Files: as listed.
   Risk: **med** (async config load in five pages). Contract: none if the
   effective endpoints are used. Tests: page tests gain config fixtures.
8. **Move governed/active state sets into the workflow payloads.** Optional
   `governedStates` / `activeStates` fields, validated in
   `configstore.py`, seeded into the lab/ED default payloads, read by
   `lab.py` (replacing `lab.py:84–86`) and `boards.py` (replacing
   `boards.py:80, 83`). Risk: **med**. Contract: additive payload fields.
   Tests: governed-endpoint tests (verify/release, admit/depart) re-run
   against config-supplied sets.
9. **Claim exportable states from config.** Derive `EXPORTABLE_STATES`
   (`billing.py:57`) from the claim workflow payload (or an
   `exportableStates` field on it); drop the web mirror
   (`BillingPage.tsx:29`). Files: `backend/app/billing.py`,
   `web/src/pages/BillingPage.tsx`. Risk: **low-med**. Contract: none
   (837 responses unchanged for the default config). Tests: export tests.
10. **ESI as terminology.** Publish `terminology/ed-triage` (levels 1–5 +
    labels) and validate triage against it (replacing `boards.py:489–493`);
    ED page takes labels from the same payload. Files:
    `backend/app/boards.py`, `web/src/pages/EdBoardPage.tsx`.
    Risk: **low**. Contract: none for default config. Tests: triage tests.
    *Acceptable alternative:* keep the constant and record ESI in the §6
    coverage list as a justified clinical-standard constant — but say so.
11. **BCMA enforced-rights as config.** A `pharmacy` section (workflow kind
    or formulary payload field) listing enforced rights, defaulting to
    today's three; `administer` (`pharmacy.py:367–448`) checks the
    configured subset in the configured order. Files:
    `backend/app/pharmacy.py`, `backend/app/configstore.py` (validator
    passthrough already permits extra concept fields — decide the home
    explicitly). Risk: **med** (patient-safety path; defaults must be
    byte-identical in behaviour). Tests: `test_phase4a_pharmacy.py` BCMA
    cases must pass unchanged against the default config.
12. **QC policy knobs.** Optional `qc` object in the lab catalogue payload —
    e.g. `blockOnMissingQc`, `freshnessHours` — read by `qc_blockers`
    (`lab.py:347–361`); defaults reproduce current behaviour exactly
    (failed-latest blocks; missing does not). Files: `backend/app/lab.py`
    (+ validator passthrough note in `configstore.py`). Risk: **med**
    (safety gate). Tests: `test_lab.py` QC cases unchanged under defaults.
13. **Small-cell leftovers.** Move `AGE_BANDS` (`pophealth.py:38–39`) into
    the cohort payload (seeded payloads carry `ageBands`; explorer reads
    payload-first); keep `DEFAULT_SMALL_CELL` as the documented fallback.
    Files: `backend/app/pophealth.py`. Risk: **low**. Contract: response
    shape unchanged under defaults. Tests: `test_phase3_pophealth.py`.

### D. Naming / domain tidy-ups

14. **OMOP concept map onto the Studio surface — last, and flagged hard.**
    Serve `concept_map` from published `terminology/omop-concept-map`,
    seeded from today's `CONCEPT_MAP_SEED` (`warehouse.py:194`). The ETL
    runs against a *second* database and would need a clinical-DB read at
    run start (`etl.py`, `warehouse.py:288–298`). Risk: **high**. Contract:
    none externally; ETL coverage counts must not move under the default
    map. Deferrable: an admin import endpoint for the existing table is an
    acceptable halfway state — but pick one and document it.
15. **Cohort visual editor tab.** Add a Registries tab to the Studio module
    (kind `cohort` already first-class everywhere except the tab list,
    `StudioPage.tsx:413–425`). Files: `web/src/pages/studio/` (new tab),
    `web/src/pages/StudioPage.tsx`. Risk: **low**. Contract: none. Tests:
    serialization tests like the other tabs'.
16. **Move the warehouse router out of `pophealth.py`** into
    `warehouse.py` (or a small `warehouse_ops.py`); routes, payloads and
    gates unchanged; `main.py` import updated. Risk: **low-med** (import
    churn only). Contract: none. Tests: none should change.
17. **One backend copy of the ADT map.** `fhir.APPOINTMENT_TRANSITIONS`
    (`fhir.py:76–85`) should import `configstore.DEFAULT_ADT_TRANSITIONS`
    (`configstore.py:70–79`) instead of restating it. Files:
    `backend/app/fhir.py`. Risk: **low**. (The TS copy disappears in #7.)

### Explicitly NOT to touch

- **The config engine core** — validators, draft→publish lifecycle,
  immutability, rollback, diff (`configstore.py`, `studio_config.py`).
  Aligned with §6 and heavily tested; the gaps are in *consumption*, not
  the engine.
- **Patient scoping + lab-unreleased visibility** (`fhir.py:329–387`,
  `fhir.py:55–57`). Safety invariants, not site variation; making them
  configurable would be a defect, not realignment.
- **The hash-chained audit** (`audit.py`) and the **pack lifecycle state
  machine itself** (`studio.py:26–31`). Governance core; only its
  *enforcement reach* is gapped (backlog #4, and the doc fix in §4.3).
- **`interpret_value` range logic** (`lab.py:309–324`) — already purely
  data-driven.
- **Protocol code:** the X12 837P renderer (`billing.py`), HL7 v2 ORU / CSV
  parsers (`lab.py`), DICOMweb JSON shaping (`boards.py`). Standards
  implementations, not site differences.
- **OMOP warehouse table shapes** (`warehouse.py`) — standard CDM.
- **Seed data content** (`backend/seed/`) — synthetic fixtures.
- **`mobile/`, `smart-app/`, `agents/`** — outside this realignment's
  shipped surface (mobile/smart-app config consumption is a separate audit).

---

## 4. Doc drift

Each item: what the doc says → what the code shows.

1. **Roadmap §6 overclaims navigation.** §6 lists "navigation
   (modules-only)" among behaviours that in Phases 1–4 "flip behaviour by
   config publish, with tests" (`docs/FULL_SYSTEM_ROADMAP.md`, §6,
   "Already proven" paragraph). What flips is only the *write-side
   validation* (400 on page/form entries, `configstore.py:291–314`). No
   runtime menu reads the config (§2a), so publishing navigation changes no
   behaviour anywhere. Same sentence, softer overclaim: "**QC gating**" —
   the gate *rule* is code (§2g); only catalogue data flips.
2. **Roadmap §2 System Studio row is stale on arrival.** It reads "ENGINE
   DONE — visual module being built out (2026-10-16)". The tabbed visual
   module is in the tree (`web/src/pages/studio/*.tsx`,
   `StudioPage.tsx:413–472`) and has its own SQA report
   (`docs/sqa/STUDIO_UI_SQA_REPORT.md`).
3. **PHASES.md claims pack enablement gates routes. It does not.**
   The Studio-reuse table (`docs/PHASES.md`, final table) states "Install
   creates `installed_draft` and nothing else; **no routes go live**" and
   "Backend surface ships disabled until enabled — pack manifests declare
   endpoints `enabled: false`". Code: every router is registered
   unconditionally (`main.py:79–94`) and no code outside `studio.py` reads
   installation status (grep evidence, §2h). Routes are live from first
   boot; `enabled: false` in the manifests is never read by any code path.
   Either the doc is corrected (recommended now) or gating is implemented
   as a deliberate Phase-5 decision (it would change the API contract and
   every module test's setup — do not drift into it).
4. **PHASES.md uses the retired name.** "Studio packs UI"
   (`docs/PHASES.md`, Client deliverables bullet). The shipped label is
   "System Studio" (`session.ts:148`) and the page is the full tabbed
   module. The same bullet-era description in the Studio workstream entry
   ("Web Studio page gained a configuration section") likewise understates
   what shipped.
5. **STUDIO_CUSTOMIZATION.md promises consumption that doesn't exist.**
   §6: branding is "one published config **consumed by web, mobile, and
   the patient portal**" — web consumes it nowhere outside Studio itself
   (§2b); mobile NOT CHECKED. §4: navigation config drives menus "for web
   + mobile" — web does not consume it (§2a). §2: custom fields "declared
   in Studio, validated at write, queryable in search" — **not
   implemented**: grep finds no custom-field logic in `backend/app/*.py`,
   and `fhir.py`'s validator (`_validate`, `fhir.py:110–184`) knows nothing
   of declared extensions. (PHASES.md itself does not claim custom fields —
   the drift is spec-vs-code, and the spec should be annotated, not the
   phase record.)
6. **Roadmap §2 Patient engagement row conflates two surfaces.** "Portal
   reads results/bills": the portal page reads results, conditions and
   medications (`PortalPage.tsx:23–25`); a patient's *bills* are read on
   the Billing page under the patient role (`session.ts:107–111`,
   `billing.py:464–477`). Minor, but the row should name both surfaces.
7. **Roadmap §2 statuses otherwise check out.** ED "DONE (core)", radiology
   PARTIAL (metadata-only, no ingest — `boards.py` docstring + code agree),
   medication loop PARTIAL (no verify step, no MAR — `pharmacy.py` has
   dispense + administer only), ADT PARTIAL, Identity PARTIAL (OIDC built
   in `security.py`; gates hardcoded per §2c) all match the code as read.
   Phase statuses in PHASES.md (0, 1, 2, 3, 4A, 4B DONE) match the shipped
   module inventory; per-phase test counts quoted there were **NOT
   CHECKED** (suites not run, per brief).
8. **In-product copy overclaims.** The Studio page header tells the
   configurator "Nothing here takes effect until a human publishes it"
   (`StudioPage.tsx:435–441`). For navigation and branding, publishing
   takes no effect at all (§2a–b). Until backlog A lands, that sentence is
   false for two of the eight things the page lists.

---

*End of audit. One file written (`docs/sqa/REALIGNMENT_AUDIT.md`); nothing
else in the tree was modified.*
