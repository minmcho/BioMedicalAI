# Studio Parity Gap Map — OpenCare System Studio vs Agentic_App System Studio

**Date:** 2026-10-18
**Directive:** Min, 2026-10-18 — "System Studio need fully implementation
like Agentic_App System Studio." This map is Deliverable 1 of Platform
Pass Workstream 10 (Studio Full Parity); the builds it schedules are
Deliverable 2, gated in `docs/sqa/PLATFORM_SQA_REPORT.md`.

**Reference inventory:** Agentic_App's Studio is the `SystemStudio.tsx`
shell plus 39 `Studio*.tsx` components under
`Agentic_App/src/components/` (~30,400 lines). OpenCare's Studio today
is `web/src/pages/StudioPage.tsx` + `web/src/pages/studio/` (14 tabs:
Overview, Module packs, Forms designer, Workflows, Terminology,
Navigation, Branding, Permissions, Integration, Agents (ADK),
Compliance, Dashboards, Advanced (JSON), Audit), backed by
`app/studio.py` (pack lifecycle), `app/studio_config.py` (generic
draft→publish governance for every registered config kind) and
`app/configstore.py` (kind registry + validators; 23 kinds after the
platform pass).

**Standing adaptation rules (Min's governance, applied to every row):**

1. **No arbitrary code execution in the clinical runtime.** Script,
   terminal, notebook and server-code surfaces become governed
   rules/transformations or design-time-only equivalents — never a
   live code runner.
2. **SaaS billing/licence surfaces are not applicable** to an EHR
   Studio — EXCLUDE with that reason.
3. **Business-specific modules map to clinical equivalents**
   (indicator library, analytics, report engine) — ADAPT, don't copy.

**Status key:** HAVE = an OpenCare surface covers the function today
(possibly in clinically adapted form) · PARTIAL = some of the function
exists; a named gap remains · MISSING = no OpenCare surface.
**Verdict key:** BUILD = build in this pass · ADAPT = the clinical
adaptation is the answer (built or building) · EXCLUDE = not
applicable to an EHR Studio, with reason · DEFER = named into the
Studio-completion block (before Phase 15) or Phase 16, with reason —
never silently dropped.

| # | Agentic_App surface | What it does there | OpenCare status | Verdict | Reason / OpenCare equivalent |
|---|---|---|---|---|---|
| 1 | SystemStudio (shell) | Tabbed Studio shell hosting all surfaces | HAVE | ADAPT | StudioPage shell with grouped tabs (Configure / Intelligence / Trust) consuming published config; parity work adds tabs to the same shell |
| 2 | StudioADK | Agents as governed config (draft→publish→disable), model + tools + multimodal flags | HAVE | ADAPT | AdkTab + kind `agent` + separate ADK service, honest-off with no model (Phase 10) |
| 3 | StudioActions | Server-side JS action snippets invoked by pages/forms at runtime | PARTIAL | ADAPT | Rule 1: actions become governed `rule` definitions (rules engine) + pipeline steps; no JS runner exists or will be built |
| 4 | StudioAiProviders | AI provider registry (OpenAI-compatible/Ollama), sealed keys, per-department defaults | MISSING | **BUILD** | Priority item: governed kind `ai-provider` + `/ai-providers` API + Studio tab; keys referenced by env-var NAME only, never stored (the dbplatform credential law); Phase 16's ADK wiring depends on it |
| 5 | StudioApprovals | Inbox for automation approval steps (approve/reject resumes a run) | PARTIAL | **BUILD** | Approvals exist scattered (pack lifecycle, break-glass review, DQ quarantine, dedup review); BUILD one inbox aggregating the real pending items (`/studio-ops/inbox` + tab), each item deep-linking to its owning surface |
| 6 | StudioAssistant | AI chat/agent docked in the designer (ask + act on designs) | MISSING | DEFER | No live model endpoint exists anywhere; a chat panel would be a dead control. The ADK draft-proposal path (agents draft, humans publish) is the governed equivalent; the panel itself lands with Phase 16's live-model wiring |
| 7 | StudioAutomationCanvas | SVG flow canvas over the same automation steps model (list ⇄ canvas, no fork) | PARTIAL | **BUILD** | Workflow canvas view over the existing `workflow` kind: renders stages/transitions from the same draft the list editor edits (view + selection sync; structural edits stay in the list editor, stated in the UI) |
| 8 | StudioAutomations | Event-driven rules: trigger + ordered steps (script/tool/webhook/condition/delay/notify/ai/loop) | HAVE | ADAPT | Rules engine (kind `rule`, governed events/actions) + pipelines + transactional outbox are the clinical adaptation; script steps excluded by Rule 1 |
| 9 | StudioBehaviors | No-code behaviours for custom tables: state machines, validation, auto-numbering, computed fields, field permissions | PARTIAL | ADAPT | The primitives exist as governed clinical config: workflow transitions + per-transition roles (state machines + field-level role gates), form validation rules, registration MRN auto-numbering. No generic custom-table behaviour surface — OpenCare has no custom tables; clinical resources are FHIR-shaped by design |
| 10 | StudioBilling | SaaS subscription plans/checkout (Stripe) | MISSING | EXCLUDE | Rule 2: SaaS billing is not applicable to an EHR Studio. (OpenCare's revenue cycle is a clinical-billing module, not a Studio surface) |
| 11 | StudioBlocks | Block renderer for composed custom pages (widget/heading/form/tabs/kanban/…) | PARTIAL | DEFER | OpenCare pages are coded React under the governed design system (docs/DESIGN_SYSTEM.md); free block composition of clinical pages is deferred to the Studio-completion block — layout standards govern clinical screens |
| 12 | StudioBusinessIntel | BI configuration: competitors, scoring models, CRM/ERP mappings | MISSING | ADAPT | Rule 3: the clinical equivalents are the indicator library, analytics and the report engine (Platform WS7/8) — governed KPI definitions, not competitor profiles |
| 13 | StudioBusinessModule | Shell joining BusinessIntel + MarketEntry config | MISSING | EXCLUDE | Rule 3 / Rule 2: a business-module shell has no EHR analogue; its pattern (one module, config in Studio, work in nav) is already OpenCare's operating model |
| 14 | StudioCanvasEditor | Free-form absolute-position page canvas (drag/resize/layers) | MISSING | DEFER | Conflicts with the governed design system (one calm layout language, panel patterns); named in the Studio-completion block for re-evaluation as a design-time-only tool |
| 15 | StudioDataViews | Join designer + raw-SQL views as first-class data sources | PARTIAL | **BUILD** | Raw SQL is refused by design in OpenCare (report-engine hard rule). BUILD the adapted surface: a read-only Data Views registry over governed sources (report-engine datasets, warehouse tables, lake zones) with live counts — no SQL editor |
| 16 | StudioDesignEditors | Authoring editors (widget editor modal, page/form editors) shared by the designer | PARTIAL | **BUILD** | The widget-designer completion (row 39) + form-designer completion (row 19) are this row's OpenCare builds |
| 17 | StudioDesigner | Designer host: CRUD over pages/widgets/forms with ownership | PARTIAL | ADAPT | OpenCare's designer host is the tab set itself (per-kind designers under one shell + Advanced JSON for everything); ownership is role governance + hash-chained audit rather than per-object owners |
| 18 | StudioEndpoints | Assistant-built API endpoints, created DISABLED, human enable, dry-run test | MISSING | **BUILD** | Priority item, adapted: kind `endpoint` — governed endpoint DECLARATIONS only (method, path, capability gate, backing), born disabled, no code execution, no dynamically created routes; the tab states plainly that enablement is a governance marker that does not create a live route (mirroring the documented pack-enablement gap). Runtime endpoint creation is EXCLUDED by Rule 1 |
| 19 | StudioForms | Form designer (field palette, types, roles, sanitised defs) | HAVE | **BUILD** (completion) | FormsTab exists over the `form` kind (FHIR Questionnaire shape); completion = align the designer's field palette/options exactly with the backend validator + FormRenderer vocabulary (nothing offered that the validator refuses) |
| 20 | StudioInstruments | Instrument Hub: fleet, devices, connection wizard, JS drivers, readings, alerts | PARTIAL | ADAPT | Phase 14 device registry + lake ingestion + threshold rules are the clinical Instrument Hub (Devices module, not a Studio tab); JS drivers excluded by Rule 1 — OpenCare adapters are HL7/MLLP, MQTT, FHIR in code |
| 21 | StudioIntegrations | Integration registries incl. MCP servers (client probing, tools/list) | PARTIAL | **BUILD** (surface) | Integration tab + kind `integration` exist (HAVE for integrations generally); the MCP server registry view lands with the tools surface (row 37) as governed declarations — live MCP probing/execution is Phase 16, per Min's placement directive |
| 22 | StudioLicense | Commercial licence status + key entry | MISSING | EXCLUDE | Rule 2: licence-key commerce is not applicable to an open-source EHR Studio |
| 23 | StudioMLOps | ML lifecycle: datasets → training runs → adapter registry → eval-gated promotion → monitoring | MISSING | DEFER | No model training exists in the clinical runtime by design (agents are governed config, not trained in-product); model lifecycle belongs to Phase 16 research scope — named in the completion block |
| 24 | StudioMarketEntry | Market-entry cost/pricing intelligence | MISSING | EXCLUDE | Rule 3: business-specific; no clinical equivalent is needed in an EHR Studio |
| 25 | StudioMetrics | Semantic metrics layer: define once (stored SQL), resolve by name everywhere | HAVE | ADAPT | The indicator library (Platform WS8) is the semantic layer, clinically adapted: governed sources (measure/cohort/named) instead of stored SQL — same "one number everywhere" contract |
| 26 | StudioMigrations | EF-style migration journal: versioned up/down SQL, reverse-order rollback | PARTIAL | **BUILD** | Adapted with WS4: the Database tab surfaces the canonical schema read model, governed schema-extension drafts (`dbplatform-schema`) and per-engine DDL export. Executing auto-migrations (up/down SQL) against a live clinical DB from the Studio is EXCLUDED by safety design — DBAs apply exported DDL under change control |
| 27 | StudioMlopsPlatforms | Connections to MLflow/Grafana/Prometheus/MinIO (health-check-gated enable) | MISSING | DEFER | External ML-platform connections have no clinical-runtime role today; deferred with row 23 (Phase 16 research scope) |
| 28 | StudioModules | Custom module registry: create modules, membership of pages/forms/widgets/tables, roles, enable | PARTIAL | **BUILD** (completion) | OpenCare modules are clinical modules + governed packs; the pack lifecycle exists (install→approve→attach→enable). Completion = the Pack Builder: compose a pack (manifest, endpoint declarations born disabled, artefact picker over existing governed config) in the UI and create it as a draft via the existing `POST /studio/packs` — no file editing |
| 29 | StudioNotebooks | Jupyter-style notebooks on script runners | MISSING | EXCLUDE | Rule 1: notebook cells are arbitrary code execution; research notebooks belong outside the clinical runtime (Phase 16 research ops) |
| 30 | StudioOrganization | Departments (nestable), org units, membership with primary department | MISSING | **BUILD** | Priority item: Organisation tab over a read model aggregating the real org structure (published locations tree — facilities/units/beds, staff directory, accounts) with counts; edits stay in the owning governed surfaces (locations draft, people, accounts), linked not duplicated |
| 31 | StudioPackages | PyPI package installs (browser + server) from Studio | MISSING | EXCLUDE | Rule 1: installing packages on the clinical server from a Studio UI is arbitrary code introduction; dependencies are build-time, reviewed, and pinned in requirements |
| 32 | StudioPlugins | Plugin packs via upload / paste / URL / samples | PARTIAL | ADAPT | Packs are OpenCare's plugin system (governed manifests + lifecycle + the Pack Builder, row 28). URL-fetch installs are EXCLUDED (supply-chain risk); upload/paste of a manifest is the pack-create path |
| 33 | StudioScripts | Monaco API IDE + server scripts project editor | MISSING | EXCLUDE | Rule 1: a server-script IDE is arbitrary code execution in the clinical runtime. The governed equivalents — rules, mapping transformations, pipeline steps from a fixed vocabulary — already exist |
| 34 | StudioServerDatabase | Guided Postgres setup for the app server (writes .env, test connection) | PARTIAL | ADAPT | WS4's DB platform is the adaptation (engine matrix, credential-free profiles, DDL export, live-verified engines) — deliberately WITHOUT .env writing or password handling in Studio; surfaced in the new Database tab (row 26) |
| 35 | StudioStreamsJobs | Streams (API-key ingestion endpoints, ring buffer, SSE) + analytic jobs | PARTIAL | ADAPT | Clinical adaptation exists in the Data Platform: device/FHIR/HL7 ingestion into the lake (Phase 14) and pipeline/ETL jobs with run history; ingestion keys are device-registry credentials, not Studio-minted stream keys |
| 36 | StudioTerminal | Server console (run shell commands from Studio) | MISSING | EXCLUDE | Rule 1: a server shell in the clinical runtime is the clearest arbitrary-execution surface; no equivalent will be built |
| 37 | StudioTools | Versioned tool registry (javascript/python/webhook/sql/ai/pipeline/rest/graphql/mcp), test runs, I/O schemas | PARTIAL | **BUILD** (surface) | Priority item, adapted: kind `tool` — governed tool DECLARATIONS (kind ∈ rest/webhook/mcp/pipeline/rule; executable javascript/python/sql kinds EXCLUDED by Rule 1) in a registry tab. MCP tool definitions land in Phase 16 (Min's placement); this pass ships the registry surface listing governed declarations, honestly labelled PARTIAL until Phase 16 wires execution |
| 38 | StudioTransformations | Table-to-table transformations with column maps across connections | HAVE | ADAPT | Phase 13 `mapping` kind + import centre, generalised by WS4's transformer workbench (`dbplatform-transform` profiles + preview) |
| 39 | StudioWidgets | Widget rendering + data loading for dashboards | PARTIAL | **BUILD** (completion) | Rendering exists (DashboardView, hand-rolled SVG per the design system); the gap is authoring — DashboardsTab only renders. Completion = a structured widget designer (widget rows: type/title/endpoint/field) saving governed dashboard drafts through the existing config API |
| 40 | StudioWorkflows | Workflow definitions (stage pipelines) + kanban operation board | HAVE | ADAPT | `workflow` kind + WorkflowsTab + module boards (Phase 4B) cover definitions and operations; the canvas view is row 7 |

## Build list for this pass (Deliverable 2)

Backend (new modules, existing governance, registrations by the
coordinator):

- kind `ai-provider` + `app/ai_providers.py` (router `/ai-providers`):
  provider registry; payload carries provider type, base URL, model,
  department default, and an API-key **reference** (env-var name) —
  any secret-shaped field is refused 422; `/ai-providers/effective`
  reports published providers + whether the referenced env var is
  present (`keyConfigured`), never its value.
- `app/studio_ops.py` (router `/studio-ops`): approvals inbox over
  real pending items (pack lifecycle steps, config drafts awaiting
  publish, break-glass reviews, DQ quarantine, dedup candidates);
  organisation read model; data-views registry; validators for
  kinds `endpoint` and `tool` (declarations only, born disabled).

Web (new tabs in the existing StudioPage shell + designer
completions): AI Providers, Approvals, Organisation, Data views,
Database (engines/profiles/schema/DDL over the WS4 APIs), Registry
(endpoint + tool declarations), widget designer in Dashboards,
workflow canvas view, form-designer completion, Pack Builder.

## Studio-completion block (scheduled before Phase 15, named here so nothing is silently dropped)

- StudioAssistant designer chat panel — with Phase 16's live-model
  wiring (honest-off until a model endpoint exists).
- MCP server registry with live probing + the tools execution path —
  Phase 16 (Min's placement directive for MCP/A2A).
- MLOps + MLOps Platforms (model lifecycle) — Phase 16 research
  scope.
- StudioBlocks page composition + StudioCanvasEditor free-form
  canvas — re-evaluate against the design system in the completion
  block; clinical pages remain coded/governed meanwhile.
- Pack-enablement route gating — **CLOSED in this pass (RTM
  STU-04)** after this map first carried it as a gap: the mechanism
  is built (`app/pack_gate.py`, opt-in via published
  `integration/pack-gating`, default OFF; ANY-enabled rule;
  /studio + /health exempt; 6 tests in tests/test_pack_gate.py).
  What remains is the default-ON rollout posture and release/DR
  proof, owned by Phase 17 (production readiness). Pack endpoint
  declarations still do not CREATE routes — the Registry surface
  states that honestly; the gate governs declared routes that
  exist.
