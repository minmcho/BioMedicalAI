# Rich Design Elevation pass — SQA report (2026-10-20)

Min's verdict on the Home & Module UX pass rendering: "It is not rich
UI design." This pass elevates the visual design to a finished product
standard — design language **"Calm Clinical, Richly Finished"**
(`~/workspace/designpass_work/DESIGN_SPEC.md`) — under a hard law:
**presentation only.** No behaviour, workflow, API, click-budget, or
UX-pass structure changes. The single functional change is the one the
spec called for: server-side validation of a new optional branding
theme field, `theme.groupColors`.

Staging only — NOT published to GitHub, NOT synced to any Mac (the
parent syncs after re-verifying this gate). Release remains HELD.

## Scope vs spec

| Spec area | Delivered |
|---|---|
| §3 Ten-hue group identity palette (measured AA) | ✅ All ten hues as `--group-<slug>` CSS vars + `[data-group]` map; literal 10% tints as fallbacks under color-mix |
| §4 Token retune (surfaces, borders, type, motion, shadows) | ✅ styles.css `:root` retune + foundation section; body line-height 1.5; reduced-motion block |
| §5 Icon tiles + `ModuleIcon` extraction | ✅ `components/ModuleIcon.tsx` extracted verbatim (ModuleCard re-exports); additive `size` prop |
| §6 Skeletons | ✅ `components/Skeleton.tsx` (SkeletonCards/SkeletonRows, role=status + AT labels); Home loading branch |
| §7 Status system: close the `--status-*-bg` gap | ✅ Theme effect derives 8% color-mix tints; backend validation unchanged; warn-ink defect fixed (`#9A6700` 4.42:1 → `--status-warn-ink #8A5C00`) |
| §8.1–8.2 Shell (TopBar, breadcrumbs) | ✅ Brand block, ghost-pill Home/sign-out, sunken breadcrumb bar carrying the current module's `data-group` + crumb dot |
| §8.3 Sign-in | ✅ Brand block (56px tile), display-size heading, accent wash (the one allowed radial), account rows with role badge + chevron |
| §8.4–8.6 Home hero + rich ModuleCards | ✅ Hero strip from REAL already-loaded payloads only (org name, long-form date, humanised audience, caller's own my-patients/my-unit counts — no new endpoint, nothing invented); group dots/headers; cards with icon tiles, 3px group rule, hover lift, group focus ring, timer pills |
| §8.7 Converged tables | ✅ `.data-table` + `.table-sticky` + foundations; legacy families (`lab-table`, `studio-table`, `dashboard-table`) converged by token; `.table` family converged at the gate (see Findings) |
| §8.8–8.10 Module headers, tabs, steppers, forms, buttons | ✅ ModuleHeader icon/group props adopted on 29 of 30 module pages (1 justified non-adoption, below); tabs = ink + group underline; stepper numerals; codified button tiers incl. legacy selectors; form convergence + `[aria-invalid]` treatment |
| §8.11 Rich-text stylesheet | ✅ The FULL `.rte-*` stylesheet — the editor previously had ZERO CSS; shared prose rules for `.rich-text-view` |
| §8.12 Studio polish + group-colour editor | ✅ BrandingTab "Group colours" fieldset (picker + hex per group, draft preview strip + sample card) over the ordinary governed publish flow |
| §8.13 Empty / loading / lock states | ✅ Designed Empty panel (icon + optional primary action); `button.primary` defined (was unstyled everywhere); lock card icon + backdrop blur |
| §9 `theme.groupColors` contract extension | ✅ Optional/partial map, exact group labels → `#RRGGBB`; a payload WITHOUT it renders the full design (the built-in defaults ARE the design) |

One non-adoption, justified: **ChartPage**'s header stays
patient-specific (its title is the patient's name; group identity
arrives via the breadcrumb's `data-group`). All other module pages —
including DocumentsPage (`folder` / Data Platform) and StudioPage
(`sliders` / System, adopted at the gate) — carry the group header.
**MasterDetail `loading` prop:** built and unit-tested; wired on
WorkforcePage and TeamsPage, where it is currently pre-empted by
those pages' top-level early-return Loading states (correctly wired,
no visible change today); PatientsPage follows the same early-return
pattern and was left unwired; Radiology does not use MasterDetail
(its own two-pane pattern by design).

## Per-workstream results

- **WS-D1 foundation:** backend `validate_branding` groupColors block
  (keys must be canonical `NAV_GROUP_ORDER` labels, values `#RRGGBB`)
  + 2 tests; `runtimeConfig` `DEFAULT_GROUP_COLORS` / `groupColorSlug`
  / parser; App theme effect (`--group-*` vars + derived status
  backgrounds); ModuleIcon extraction; Skeleton; `:root` retune +
  foundation CSS. Backend 48 passed (touched files), web D1 suites 42.
- **WS-D2 shell + Home:** TopBar, BreadcrumbBar, LoginPage, HomePage
  hero + group wrappers, ModuleCard, PatientList `.data-table`,
  skeleton loading branch. 37 tests.
- **WS-D3 components + page clusters:** shared-component upgrades
  (ModuleHeader, StatusViews, MasterDetail, FormRenderer, IdleLock) +
  29 module-header adoptions and table convergence across four
  disjoint page clusters. Detail + verification notes:
  `~/workspace/designpass_work/ws-d3/RESULTS.md`.
- **WS-D4 rich text + Studio:** full `.rte-*` stylesheet; BrandingTab
  group-colour editor + models round-trip (an untouched payload
  serializes identically — the exact round-trip test stands). 50 + 34
  tests across the touched suites.
- **WS-D5 gate + reconciliation:** see Gate below and
  `~/workspace/designpass_work/ws-d5/RESULTS.md`.

## Configuration-coverage list (standing rule)

- **Studio surface: System Studio → Branding tab** — the group-colour
  editor edits the published `branding/default` config's
  `theme.groupColors` (new optional field, spec §9) through the
  ordinary draft → publish governed lifecycle. No new config kind, no
  new endpoint.
- **Backend:** `configstore.validate_branding` validates
  `theme.groupColors` (unknown group label or malformed colour →
  rejected like every other invalid theme field); the seed payload
  (`default_branding_payload()`) is UNCHANGED — sites that never set
  an override get the full design from the built-in measured palette.
- Everything else in the pass is static presentation (CSS + component
  markup): no other configuration surface is created, read, or altered.
  Navigation, job profiles, permissions, and all clinical config are
  untouched.

## Measured contrast (WCAG 2.x relative luminance, re-run at the gate)

Group palette — accent on white / accent on its own 10% tint (all ≥ AA
4.5:1 for text and for the identity uses specified):

| Group | Hex | On white | On own tint |
|---|---|---|---|
| Patient Care | #0B6E4F | 6.25 | 5.39 |
| Access & Revenue | #8A5A13 | 5.91 | 5.12 |
| Clinical | #1D4E7E | 8.60 | 7.30 |
| Diagnostics & Pharmacy | #5F4B9E | 7.03 | 6.06 |
| Operations | #0E6B7A | 6.17 | 5.33 |
| Safety & Support | #8E2A4E | 8.10 | 6.88 |
| People & Teams | #4A4E9C | 7.34 | 6.30 |
| Insights | #5C6618 | 6.23 | 5.43 |
| Data Platform | #41586B | 7.41 | 6.37 |
| System | #5D4037 | 9.32 | 7.93 |

Status (derived 8% tints as shipped by the theme effect): ok ink 5.55,
warn ink 5.25 (the tightest pairing in the system), bad ink 5.75, info
ink 7.57 — all ≥ 4.5:1. Base text: ink 16.15, muted (retuned) 6.38,
ink-soft 10.34 on white. Full re-run output:
`~/workspace/designpass_work/ws-d1/contrast-rerun.txt` — it reproduces
the spec table exactly; no hue was re-picked during the build.

## Adjusted-tests register (3 entries, no assertion weakened)

1. `runtimeConfig.test.tsx` (×2 expectations): the exact-match theme
   objects gained `groupColors: DEFAULT_THEME.groupColors` — the
   spec §9 contract extension adds the field to the effective theme.
2. `HomePage.test.tsx` (×1 lookup): the card-click test located the
   "My patients" section by its bare text; the new hero chip
   legitimately renders the same words, so the lookup is scoped to
   the full section title "My patients — I am the attending". The
   assertion (section present, ordered after the cards) is unchanged.

## Findings recorded during the build

- **Cluster-report verification:** interim cluster messages diverged
  from the final state in both directions (adoptions claimed early
  that were later reasoned out, and adoptions — DocumentsPage's
  header, Workforce/Teams MasterDetail `loading` — that landed after
  an interim "skipped" note). The coordinator therefore verified
  every adoption claim against disk at the gate; the four clusters'
  FINAL reports agree with the on-disk state throughout (e.g. Lab's
  21 tables already carry `lab-table`; Patients/Inbox contain no
  tables; Radiology does not use MasterDetail). All figures in this
  report are the disk-verified ones. (This finding was itself
  corrected post-gate when the final cluster reports arrived.)
- **`.table` had no CSS at all:** 28 tables across 9 files rendered
  with raw UA styling. Converged at the foundation level in WS-D5.
- **`.button-secondary` / `button.primary` were dead classes** (used
  in markup, defined nowhere); both are now on the codified tiers.
- **Pre-existing, flagged, not fixed** (outside presentation scope):
  PharmacyPage's verification-table header row uses `<td>` rather
  than `<th>`, so the header band does not apply to that one row.

## Gate (coordinator-run on the final tree)

| Check | Result |
|---|---|
| Backend config tests (`test_ux_shell_config.py` + `test_studio_config.py`) | **48 passed** |
| Web full suite | **79 files, 551 tests, all passed** (baseline 534; +17 new design-pass tests) |
| Production build (tsc --noEmit + vite) | **exit 0** |
| Case-collision audit over web/src (207 files) | **ZERO** |
| Contrast re-computation | Spec table reproduced exactly |

## NOT RUN

- **Rendered/browser verification: NOT RUN** (standing VM limitation).
  The acting browser gate is Min's click-through on the MacBook Air
  after the parent syncs this pass. No rendered claim is made.
- Full backend suite not re-run for this pass: the only backend change
  is the `groupColors` validation block, covered by the 48 tests
  above; the pass folds into the next full gate.
- All standing NOT RUN items (live external legs, Docker/Kubernetes,
  mobile compilation, live LLM) are unchanged and unaffected.

## Parent addendum (2026-10-20)

Parent re-verification on the final tree, independently run: backend config tests (test_ux_shell_config + test_studio_config) 48 passed; web full vitest 79 files / 551 tests passed; production build exit 0; case-collision audit over web/src (207 files) ZERO. Coordinator gate figures reproduced exactly. Air sync = Run 11.
