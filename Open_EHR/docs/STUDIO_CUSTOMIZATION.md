# OpenCare Studio — customization engine (like Agentic_App's System Studio)

Direction from Min, 2026-10-15: **Open_EHR must be customizable like Agentic_App** — the system is configured through its Studio, with maximum flexibility, instead of being hardcoded per site.

## Principle
Everything a deploying organisation needs to change — forms, fields, workflows, navigation, terminology, branding, module mix, agent behaviour — is **Studio configuration data** with a draft → publish lifecycle, versioning, role gates, and audit. Code ships capabilities; Studio ships the site's system. This mirrors Agentic_App's System Studio (configuration IDE: module packs, designer, navigation manager, data mapping, custom tables, workflows) adapted to healthcare standards.

## Already in place (Phase 0/1 kernel)
- Versioned module packs; install = draft only; approve → attach → enable as separate audited human steps; pack endpoints disabled until enabled.
- Role gates (admin / configurator / clinician / viewer / patient) and a hash-chained audit log on every Studio and clinical write.
- Web Studio page (packs) in the Phase 1 web app.

## Visual System Studio module (built 2026-10-16)

The web Studio is now a real tabbed **System Studio** module (nav label "System Studio", SQA: `docs/sqa/STUDIO_UI_SQA_REPORT.md`), not a single packs + raw-JSON page:

- **Overview** — org profile from the published branding config, published-config inventory per kind with versions, pack counts by state, recent audit activity.
- **Module packs** — the pack lifecycle UI (unchanged).
- **Visual editors** for the governed kinds, all saving drafts and publishing through the same `/studio/config` endpoints: **Forms designer** (FHIR Questionnaire items + live preview), **Workflows** (states/transitions), **Terminology** (concept tables in the lab catalog shape), **Navigation** (modules-only menu rows), **Branding** (org profile + header preview), **Permissions** (capability × role matrix over `permissions/role-matrix`), **Integration** (key/value connection data).
- **Advanced (JSON)** — the raw-JSON editor at full capability for any kind/key (incl. cohort), versions and rollback.
- **Audit** — read-only view of the hash-chained log via the new `GET /studio/audit` (admin/configurator).

Known consumption gaps (editor side is complete; wiring is later-phase work): the app shell does not yet render its own menu/branding from the published navigation/branding config.

## The customization engine (builds next, before Lab LIS — see sequencing note)
1. **Form & field designer** — custom forms stored as FHIR `Questionnaire`; responses as `QuestionnaireResponse` (optionally projected to Observations). Field types, order, required rules, value-set bindings — all config. Designer drafts never render for clinicians until published (the Agentic_App rule: only approved + attached items appear in navigation/use).
2. **Custom fields** — organisation-defined extensions on Patient/Encounter/Observation etc., declared in Studio, validated at write, queryable in search.
3. **Workflow configuration** — state machines as data: ADT transitions, lab order→result flow, referral flow. The Phase 1 ADT machine becomes the first *config-driven* workflow (same behaviour, loaded from published config).
4. **Navigation & workspace config** — menu items, ordering, per-role visibility for web + mobile (modules only as menu entries; forms/pages live inside modules — the rule Min set in Agentic_App carries over).
5. **Terminology & value sets** — local code lists, lab panels, reference ranges, departments, locations — managed as Studio data (FHIR CodeSystem/ValueSet shaped), versioned.
6. **Branding & org profile** — organisation name, logo, colours, portal wording — one published config consumed by web, mobile, and the patient portal.
7. **Role & permission matrix** — per-role capability toggles beyond the built-in defaults, stored as config, enforced by the API (never by hiding UI alone).
8. **Integration config** — endpoints, HL7/FHIR connections, and (in Agentic_App, already built) ADK agent configs are Studio data in the same pattern: draft, test, publish, disable, audit.
9. **Config governance** — every config object: version history, diff, draft/publish/disable, publish = audited action, rollback to a prior published version. Nothing config-shaped is edited in place once published.

## Sequencing note
Scheduled **immediately after Phase 1 closes and before Phase 2 (Lab LIS)**: the Lab module's forms, panels, ranges, and order→result workflow will then be built *as Studio configuration + a pack*, proving the engine on a real module instead of retrofitting it later. Phases 3–4 inherit the same rule: new capability = code where needed + Studio configuration for everything site-specific.

## Non-negotiables (carried from Agentic_App)
- Assistant/AI-created config is a designer draft only; a human approves and attaches before anything appears or activates.
- No config change bypasses audit; no published config mutates silently.
- A customization that cannot be expressed as config does not get hardcoded quietly — it is flagged as an engine gap and the engine is extended.
