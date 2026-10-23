# OpenCare EHR — Design System Specification

**Status:** Written specification of the presentation layer as built
(platform pass, 2026-10-18). This document *records* the design system
that exists in code — `web/src/styles.css` (tokens + idioms) and
`web/src/components/` (shared components) — so conformance can be
checked against a written standard instead of inferred from the code.
Where code and this document disagree, the code is the defect or this
document is stale; either way the disagreement is a finding, not a
choice. The design standard itself is Min's (2026-10-18): *beautiful
and simple layout, easy to operate, helps the user easily understand
the ops and workflow.*

---

## 1. Design principles

1. **One calm, consistent language.** A single palette, a single type
   ramp, one card idiom, one table idiom, everywhere — clinician
   workspace, portal, and System Studio alike.
2. **One obvious primary action per panel.** Each panel carries a
   clear title and exactly one visually dominant action (accent-filled
   button); secondary actions are quiet (outline or text buttons).
3. **Workflow state is visible.** Wherever work moves through steps
   or states, the current state is shown with a Stepper or a labelled
   status — **text + colour, never colour alone** — so the next step
   is obvious without training.
4. **Fewest clicks the task allows.** Sections that are distinct
   views become in-panel tabs, not one long scroll; deep-links from
   navigation land on the relevant tab.
5. **Honest states.** Loading, empty, and error are distinct,
   labelled states. An empty list says why it is empty; a 403 reads
   as *Permission denied* with the server's detail, never as an empty
   list; an unreachable backend says so. Nothing fails silently.
6. **No dead controls.** Every visible control does something real.
   A control whose data source is empty renders **disabled with the
   reason shown**, never as a live-looking but inert widget.
7. **Validation is the app's, inline, and shared.** Forms validate
   client-side (shared validators) and server-side with the same
   rules; errors appear inline under the field they name
   (`.field-error`), in text. Every authored form carries
   `noValidate` so native browser validation never swallows a submit
   before app validation runs.

## 2. Organisation — three tiers

| Tier | Mechanism | Configuration source |
|---|---|---|
| 1. Grouped menu | Sidebar modules under group headings (`.nav-group-label`) | Published `navigation` config: each module item carries a `group` |
| 2. Sub-menus | Section links indented under a module (`.nav-sub`) | Same config: `sections` deep-links into the module's tab/sections |
| 3. In-panel tabs | `Tabs` component inside the panel | Module page state; section keys match the navigation `sections` slugs |

Group membership and sub-menu composition are **published
configuration** — regrouping is a republish in System Studio, never
a code change. Navigation entries are modules only (a page or form
can never be a menu entry — enforced server-side, HTTP 400).

## 2A. Simple Navigation Model (normative — Min's directive, 2026-10-18)

These are **rules**, not aspirations: a change that breaks one fails
SQA. They describe the shell as implemented (`web/src/App.tsx`,
`web/src/auth/session.ts`) and bind new work.

- **N1 — Three tiers, never four.** Tier 1: navigation **groups**
  (the eight, in fixed order: Patient Care, Clinical, Laboratory,
  Pharmacy, Operations, Revenue, Data & Platform, System Studio).
  Tier 2: **modules** within a group, with optional sub-menu
  **sections**. Tier 3: **in-panel tabs** inside the module's page.
  No fourth level may be introduced; depth is bought by grouping,
  never by nesting menus inside menus.
- **N2 — Role-filtered navigation is the default behaviour.** The
  sidebar shows a module only when ALL of these pass: the published
  navigation config does not hide it (`visible: false`), the item's
  `roles` list (when present) contains the user's role, and the
  published permissions matrix grants the role access to at least
  one of the module's pages (`resolveNavItems` /
  `canAccessWith`). The same check guards direct page access: a
  role that somehow lands on a page it cannot access is redirected
  to its role home. What a user cannot use, they do not see.
- **N3 — Role home is automatic.** Sign-in lands each role on its
  home with **zero clicks**: staff on Home (their published
  audience dashboard), patients on the portal. Nobody lands on a
  generic menu and hunts.
- **N4 — The sidebar is flat.** Groups are **labels, not
  accordions**: every permitted module is visible at once and is
  exactly **one click** from anywhere in the app. Sub-menu sections
  expand only under the **active** module (contextual, never a
  pre-expanded tree). The active module — and the chart context
  (Patients stays highlighted in a chart) — is always visibly
  marked. Home is pinned above the groups for staff.
- **N5 — Click budgets (maxima, per journey).** Sign-in → role
  home: **0**. Any module from anywhere: **1**. A patient's chart
  from sign-in: **2** (Home widget or Patients row → chart).
  Register a patient → booked appointment: **one combined flow**,
  no module switch. Inside a module, a core task's primary action
  is reachable within **2 clicks** of the module landing (tab
  switch = 1). A budget breach is a defect, filed against the
  journey, not a style note.
- **N6 — Location is always legible.** At any moment the user can
  answer "where am I" from three visible facts: the highlighted
  module (tier 2), its group label (tier 1), and the active tab's
  text label (tier 3). No breadcrumbs: at depth ≤ 3 they would be
  noise. Workflow position inside a task is the Stepper's job (§4.2),
  with state words, never colour alone.
- **N7 — Finding a patient.** Patient search lives where patients
  are worked: the Home dashboards and the Patients module (search →
  row → chart, inside the N5 budget). The shell deliberately has no
  global search box today; that is a **named gap** (see the platform
  SQA's navigation contradictions), not an oversight to paper over.
- **N8 — New modules conform or don't ship.** A new module declares
  its group and sections in navigation config (modules-only rule,
  §2), states its core journey's click count against N5 in its SQA,
  and uses the shared `Tabs` for tier 3. A module without a group
  assignment is incomplete.

Conformance note: §2A is written against the shell as audited on
2026-10-18. A parallel navigation & UX audit
(`OpenCare-Navigation-UX-Spec.md`, goal files) is being produced;
when it lands, this section is reconciled against it and any
divergence is recorded in the SQA — until then the contradictions
already visible are listed in `docs/sqa/PLATFORM_SQA_REPORT.md`
(§ "Navigation contradictions").

**Alignment addendum (2026-10-18, the UX spec has landed).** §2A
and the spec's Part B agree on the three tiers (spec N-1), the
permissions floor (N-3's `canAccessWith` intersection), the
chart-from-any-list rule (N-6, satisfied for Home lists by the
F-2 fix), the global-find gap (N-7 = N7 above, S-7 Phase 15), and
hint truthfulness (N-10, exercised by the F-1 fix). The spec
carries four rules §2A did not, adopted here as normative with
their build homes named in the SQA's S-deferral list:

- **Group size 2–7 (spec N-2).** No tier-1 group holds more than
  7 modules or fewer than 2. Current violations: Operations (8)
  and the one-module groups (Laboratory, Pharmacy, System
  Studio). Regrouping is a published navigation change, never
  code (S-11, Phase 15).
- **Job-role profiles (spec N-3).** The visible menu = the user's
  job-role profile ∩ the permissions floor. Profiles (nurse,
  pharmacist, lab technologist, front desk, clinician, manager)
  are published Studio configuration with menu-size caps (front
  desk ≤ 6, nurse ≤ 10, pharmacist ≤ 8, lab technologist ≤ 8,
  clinician ≤ 14). Design recorded here; enforcement build is
  Phase 15 (S-3).
- **Boards get board width (spec P-8 / S-14).** Operational
  boards (Command Center, Bed board, ED board, Unit board)
  render on a wide layout class without the 980px reading cap;
  reading pages keep the column. The class is a shell-level page
  property — the build is Phase 15; the rule is normative now.
- **Place persistence + landing-is-your-queue (spec N-8 / N-9).**
  Modules, tabs and charts should be restorable/shareable
  places, and a module's default tab should be the entering
  role's primary queue. The current shell meets neither
  (state-based routing; fixed landing tabs) — recorded as
  contradictions in the SQA (S-13 / S-15, Phase 15), not
  silently absorbed here.

## 3. Tokens (`web/src/styles.css`, `:root`)

| Token | Value | Role |
|---|---|---|
| `--ink` | `#17222b` | Primary text |
| `--muted` | `#5b6b76` | Secondary text, group labels, meta lines |
| `--accent` | `#0b6e4f` | Primary action, active states, links (deployment-overridable via published `branding` config `primaryColor`) |
| `--border` | `#d6dee3` | Card, table, and input borders |
| `--error-bg` | `#fdecec` | Error state background |
| `--error-ink` | `#7a1f1f` | Error text |

Supporting literal colours (component-level, from the same palette
family): page background `#f6f8f9`; card white `#fff`; soft accent
wash `#e6f2ec` (active nav/tab, `ok` tone); hover wash `#eef3f1`;
table header wash `#eef3f1`; warn wash `#fdf3e0` / warn ink `#9a6700`;
critical wash `#fde8e8` / critical ink `#b42318`; info wash `#e8eef7`
/ info ink `#1d4e7e`; neutral wash `#eef2f4`.

**Type:** system UI stack (`system-ui, -apple-system, "Segoe UI",
sans-serif`); monospace (`ui-monospace`) only for config payloads and
code. Base size inherits the browser default; panel titles use the
page-header pattern (below); widget values step up deliberately
(1.9rem standard, 2.6rem big-number).

**Geometry:** card radius 10px; row/button radius 8px; input radius
6px; pills (badges, steps) radius 999px. Content column max-width
980px; sidebar 220px, collapsing to a top bar under 720px.

## 4. Shared components (`web/src/components/`)

### 4.1 `Tabs` (`Tabs.tsx`)
Props: `tabs: {key, label}[]`, `activeKey`, `onChange`, `panels:
Record<key, ReactNode>`, optional `ariaLabel`. Renders a
`role="tablist"` bar of native buttons (`role="tab"`,
`aria-selected`) and **only the active panel** (`role="tabpanel"`) —
inactive panels' effects never run. CSS: `.tabs-bar`, `.tab-button`
(`.active` = accent wash + border + semibold). Grouped tab bars may
interleave `.tab-group-label` separators (uppercase micro-labels, the
nav-group idiom). System Studio's own tab strip (`.studio-tabs`)
predates `Tabs` and follows the same visual idiom.

### 4.2 `Stepper` (`Stepper.tsx`)
Props: `steps: string[]`, `current: number` (0-based). An ordered
list (`.stepper`) of pill steps (`.step`); states are `Done`
(`.step-done`, accent wash), `Current` (`.step-current`, accent fill,
`aria-current="step"`), `Pending` (plain). Every step renders its
label **plus a state word in parentheses** — state is text + colour
by construction. A lighter inline variant, `.workflow-steps` /
`.step-current` with "(current)" text, is used in compact strips
(e.g. Register → Book → Check-in).

### 4.3 `StatusBadge` (`StatusBadge.tsx`)
Props: `tone: "ok" | "warn" | "bad" | "info" | "neutral"`, children =
the label text. Renders `.badge.status-badge` plus a tone class
(`ok` = base accent wash; `warn` = `.badge-warn`; `bad` =
`.badge-critical`; `info` = `.status-badge-info`; `neutral` =
`.status-badge-neutral`). The label is always caller-supplied text,
so a status is never a bare coloured dot. The older `.badge`,
`.badge-warn`, `.badge-critical` classes remain the same idiom for
inline use.

### 4.4 `StatusViews` (`StatusViews.tsx`) — the honest states
`Loading({label})` (`.state.state-loading`, `role="status"`),
`Empty({label})` (`.state.state-empty`, `role="status"` — the label
must name the reason), `ErrorView({error})` (`.state.state-error`,
`role="alert"`) which specialises `ApiError`: 403 → "Permission
denied." + server detail; 401 → "Not signed in."; status 0 →
"Backend unreachable."; other statuses → "Request failed (N)." —
each with the server's detail text. Inline variants: `.error-note`
(block note) and `.field-error` (per-field, under the field).

### 4.5 `DashboardView` / `DashboardGrid` (`DashboardView.tsx`)
Renders a published `dashboard` config's widgets on `.dashboard-grid`
of `.dashboard-widget` cards (title `.dashboard-widget-title`, value
`.dashboard-widget-value`; `.dashboard-widget-wide` for wide
widgets). Widget displays (hand-rolled SVG, no chart library):
- **big-number** — one value at 2.6rem, optional unit suffix.
- **line / bar** — series charts drawn as inline SVG from the
  widget's data points.
- **gauge** — an arc gauge with value + unit (`.gauge-unit`).
  **Gauge semantics are higher-is-better only** — a gauge is never
  used for a lower-is-better quantity (documented in the Phase 13
  SQA; a gauge that fills as things worsen is a defect).
- **table** — `.dashboard-table`, border-bottom rows.
Widgets whose data URL fails render their own honest state; a
dashboard never fabricates a value.

### 4.6 `FormRenderer` (`FormRenderer.tsx`)
Renders a published `form` config (FHIR Questionnaire shape):
`group` items become `<fieldset>`/`<legend>` (`.form-group`), leaf
items become labelled `.form-field` controls (boolean, decimal,
integer, string, text, date, dateTime, time, choice, open-choice,
quantity), `display` items become text. `initialFormValues(form)`
and `buildAnswers(form, values)` convert between control state and
QuestionnaireResponse-shaped answers (groups nest; empty answers are
omitted). Per-field errors render as `.form-error`. Governed forms
at workflow checkpoints (checklists, certifications) are this
component everywhere — there is no second form engine.

### 4.7 Panel and page patterns (CSS idioms, no component)
- **Page header:** `.page-header` — flex row, title left
  (`h2`, margin 0.4rem 0), the panel's ONE primary action right.
- **Card:** `.card` — white, 1px `--border`, radius 10px, padding
  1rem 1.2rem.
- **Tables:** `.lab-table` / `.studio-table` / `.dashboard-table` —
  collapsed borders, `--border` grid, header row on `#eef3f1`.
- **Forms:** `.field` (label above control, max-width 320px),
  `.inline-form` (end-aligned row of fields + accent submit),
  `.row-actions` (quiet action row inside a card/row).
- **Primary button:** accent fill, white text, radius 8px, semibold;
  disabled = opacity 0.55 + default cursor. **Secondary button:**
  white, accent border + text (`.appt-actions button`,
  `.studio-editor button`). **Text action:** `.linklike`.
- **Downtime banner:** `.downtime-banner` (warn wash, darkened warn
  ink, thin gold border) — the read-only-mode notice; it names the
  mode in text.
- **Brand chip:** `.studio-color-chip` renders the published
  branding colour as a swatch next to its hex value (colour + text).

## 5. Rules for new work (checked at SQA and at the final gate)

1. Use the tokens; do not introduce a new literal colour without
   adding it to §3 of this document in the same change.
2. Use the shared components (`Tabs`, `Stepper`, `StatusBadge`,
   `StatusViews`, `DashboardView`, `FormRenderer`) before building a
   local variant; a local variant needs a stated reason.
3. Every panel: `.page-header` title + one primary action;
   secondaries quiet.
4. Multi-step work shows a `Stepper` (or `.workflow-steps` strip);
   every status shows a `StatusBadge`-style labelled text.
5. Multi-section panels use `Tabs`; section keys match the published
   navigation `sections` slugs so sub-menu deep-links land correctly.
6. Every data surface handles Loading / Empty (with reason) /
   ErrorView; selects are populated from a real endpoint or config,
   or disabled with the reason shown.
7. Forms carry `noValidate`; validation errors are inline
   (`.field-error`) and mirror the server rules.
8. Gauges only for higher-is-better quantities.
9. Accessibility floor: native buttons for tabs/actions,
   `role`/`aria-selected`/`aria-current` as the components provide,
   status never by colour alone, focusable controls are real
   controls (no clickable divs).
10. Navigation conforms to §2A (three tiers, role-filtered by
    default, click budgets N5); a new module states its group,
    sections and core-journey click count in its SQA.

## 6. What this system deliberately is not

- No CSS framework and no chart library: the idiom is hand-rolled
  CSS + inline SVG, small enough to audit line by line.
- No dark theme in the baseline (`color-scheme: light`); branding
  configuration changes the accent and names, not the theme.
- Printed output (invoices, statements, reports) is server-side PDF
  on published print templates — a separate, governed surface; a
  visual report designer is report-engine work, not part of this
  baseline spec.
