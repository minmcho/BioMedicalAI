# SQA Report — System Studio visual module (web)

**Date:** 2026-10-16 · **Scope:** the tabbed System Studio module in the OpenCare web app (`web/src/pages/StudioPage.tsx` + `web/src/pages/studio/`), plus one backend addition (`GET /studio/audit`). Built at Min's direction (2026-10-16): the web Studio was a single packs + raw-JSON page labelled "Studio packs" — "system studio module is missing."

## What was built

The nav label is now **System Studio** (page key `studio`, role gating unchanged: admin/configurator). The page is a tabbed module:

| Tab | What it does |
|---|---|
| Overview | Org profile from the published branding config (name, portal title, colour chip, support contact); published-config inventory per kind with version numbers; pack counts by installation state; 8 most recent audit entries. |
| Module packs | The pre-existing packs UI, unchanged: install-as-draft, approve → attach → enable / disable lifecycle. |
| Forms designer | Visual FHIR Questionnaire editor: pick an existing form key or create one; add/remove/reorder items; linkId, text, type (string/decimal/integer/boolean/date/choice), required toggle; choice options (answerOption valueCodings); live read-only preview; save draft + publish; version history with load/publish/disable/rollback. |
| Workflows | State-machine editor: states with initial/terminal marks, transitions (from/to selects, action label, roles); save draft/publish. |
| Terminology | Concept-table editor in the lab catalog's actual shape (code, display, unit, reference low/high, critical low/high, active); unmodelled concept data (panel `components`) is preserved through round-trips. |
| Navigation | Menu rows {module, label, roles[], visible} with reorder; row order is the stored `order`. The modules-only rule stays server-side — a 400 from the API is surfaced verbatim in the UI. |
| Branding | Org name, portal title, tagline, primary colour (colour input + hex), logo URL, support contact; live preview chip rendering the org header as the app shell does. |
| Permissions | Capability × role matrix (14 capabilities × admin/configurator/clinician/patient/viewer) over `permissions/role-matrix`, starting from the server defaults merged with the saved config. Copy states plainly that enforcement is server-side. |
| Integration | Key/value rows over the config's scalar leaves (dotted paths, e.g. `viewer.baseUrl` for the DICOM viewer); non-scalar values preserved untouched. |
| Advanced (JSON) | The pre-existing raw-JSON editor at full capability — every kind (now incl. cohort + integration), versions, payload view, publish/disable/rollback. |
| Audit | Read-only table (time, actor, action, subject, hash prefix) over the new endpoint, with a 25/50/100/200 limit selector. |

**Governance is unchanged and entirely server-side:** every visual tab saves DRAFT versions through the existing `/studio/config` endpoints and publishes through them; published versions are never edited in place (opening one bases a new draft). No new write paths were invented.

**Shared shell:** all kind editors sit on one `ConfigEditorShell` (key picker / fixed key, working-base tracking, save-draft, save-then-publish, version history with load/publish/disable/rollback, API error surfacing). Structured state ⇄ payload conversion lives in pure, unit-tested model functions per kind (`web/src/pages/studio/models.ts`) that preserve unmodelled fields verbatim in both directions.

## Backend addition

`GET /studio/audit` (in `backend/app/studio.py`) — no audit read endpoint existed. Returns the most recent entries of the single hash-chained log (pack + config + clinical writes share the chain), newest first; `limit` query param (default 50, 1–200, out-of-range = 422); roles **admin/configurator only** (401 unauthenticated, 403 for clinician/viewer/patient). Response per entry: `id, created_at, actor, action, entity, entry_hash, prev_hash`.

## Verification (run 2026-10-16, this machine)

| Suite | Before | After |
|---|---|---|
| Backend (`pytest tests`) | 142 passed | **146 passed** (+4 audit-endpoint tests: auth, role gate, newest-first + hash fields, limit bounds) |
| Web (`npm test`) | 81 passed / 16 files | **112 passed / 18 files** (+19 model serialization tests both directions for form/workflow/terminology/navigation/branding/permissions/integration; +11 StudioPage component tests covering every tab incl. a save-draft serialization assertion per editor and the navigation 400 surfacing; +1 client URL test for `listAudit`) |
| Web build (`npm run build`, tsc strict + vite) | clean | **clean** |

Test design notes: component tests use a stub `ApiClient` and assert the exact serialized payload each editor sends to `createConfigDraft` — i.e. the visual editors are verified against the payload contracts, not just rendered.

## NOT RUN / honest gaps

- **Browser click-through E2E: NOT RUN** (no browser run against a live backend was performed; coverage is unit + component level, consistent with the standing browser-E2E gap noted in earlier phase reports).
- Live Keycloak/OIDC session against the Studio: NOT RUN (unchanged from Phase 1 status).
- The published **navigation** config is not yet consumed by the app shell's own menu (the shell still uses `session.ts` nav); the tab governs the config record. Same for **branding**: the app header does not yet re-skin from the published config — the tab's preview shows the header as it will render. Both are consumption wiring for a later phase, recorded here rather than implied done.
- Permissions tab edits the governed config record; runtime enforcement is the server's existing `require_capability` merge over defaults (already tested in the backend suite).
