# Phase 10 SQA Report — Foundation: Studio-Managed Agents, Rules Engine, Compliance Framework, Security Core

Date: 2026-10-17. Scope: `docs/PHASE10_ROADMAP.md` Tracks A, B, J, K
(core slices) + the shortfall-register items assigned to Phase 10.
Staging only — NOT published to GitHub, NOT synced to any Mac, per
instruction. Synthetic data only. The parked draft under `agents/`
and `docs/AGENTS.md` were not read, modified, or included.

## Gate results (all re-run at delivery)

- Backend: **362 passed** (324 Phase 9 baseline + 38 new Phase 10
  tests; zero regressions, no existing test weakened — one existing
  web test updated for content this phase legitimately added: the
  Studio tab roster eleven → fourteen, and the permissions
  capability-count expectation 36 → 40 with its comment extended).
- ADK runtime service (own suite, own venv): **8 passed**.
- Web: **211 passed** (203 baseline + 8 new: 5 tab tests, 3 downtime
  tests) / build **clean** (`tsc --noEmit && vite build`).
- Backup/restore drill: **RUN — PASS** (see Workstream 6).

New backend suites: `tests/test_phase10_agents.py` (11),
`tests/test_phase10_rules.py` (7), `tests/test_phase10_compliance.py`
(6), `tests/test_phase10_security.py` (8),
`tests/test_phase10_studio.py` (6). ADK suite:
`backend/adk/tests/test_adk.py` (8). New web suites:
`src/pages/studio/Phase10Tabs.test.tsx` (5) + 3 downtime tests in
`src/auth/runtimeConfig.test.tsx`.

One observed flake, recorded honestly: in one full web-suite run,
ChartTabs' "notes tab renders the published template form" failed
once under parallel load; it passed standalone twice and in the two
subsequent full runs (208/208, then 211/211). No change was made to
that test or its subject code by this phase.

## What landed, per workstream

### WS1 — Proxy fix (shortfall register)

`/messages`, `/engagement`, `/quality` added to the proxy prefix
lists in `web/vite.config.ts` and `web/nginx.conf`; the Phase 10
prefixes (`/agents`, `/rules`, `/compliance`, `/privacy`) were added
to both at the same time, so portal messaging, the quality page and
all Phase 10 surfaces work in proxy mode, not only direct mode.

### WS2 — Track A core: Studio-managed agents (ADK pattern)

Design decision that had to be made (agent-runner choice):
google-adk 2.12.0 + litellm 1.94 ARE installable in this sandbox,
but installing them into the shared backend venv would downgrade
FastAPI 0.143.2 → 0.141.1. Following the Agentic_App layout, the ADK
runtime is therefore a **separate service with its own venv**
(`backend/adk/`, google-adk + LiteLLM, FastAPI app of its own) and
the EHR core talks to it over HTTP (`ADK_RUNTIME_URL`, default
`http://127.0.0.1:8100`). The contract: the core sends the full
published agent config + the caller's own bearer token per run; the
runtime executes it via Google ADK's Runner with LiteLlm, so any
LiteLLM model string works and the local-first default is a local
Ollama route. Its one tool, `opencare_api_call`, calls back to the
EHR API with the caller's token, gated by the agent config's
`apiAllowlist` BEFORE any HTTP happens — and, stricter than a
general assistant, there is no implicit GET-anywhere: every
reachable prefix is named in governed config.

- Agent definition = Studio config kind **`agent`** (model route,
  instructions, tool allow-list drawn from a server registry —
  today only `opencare_api_call` —, `apiAllowlist`, modalities,
  temperature, maxSteps). Credential-looking model strings are
  refused at validation (422): model routes never carry secrets.
  An empty model string is valid config and means "no route".
- Lifecycle mirrors packs: config draft → publish (published
  versions immutable) → a separate **enable** step
  (`agents.enable`/`agents.disable`, audited; enable with no
  published version is 409). Drafts are never listed as runnable
  and never run (404).
- **Honest-off, three ways, all test-proven:** runtime unreachable →
  503 "Agent runtime unavailable… No answer was generated.";
  runtime up but no model configured → the runtime's own 503
  surfaced verbatim; modality not enabled in config → 422 before
  any model call. A failed/refused run records nothing — no run
  row, no audit entry claiming a run happened.
- Every completed run writes an `AgentRun` row and a hash-chained
  `agents.run` audit entry: agent key, config version, actor,
  patient ref, **input hash** (sha256 of prompt + parts), output,
  model. Output is a draft: `POST /agents/runs/{id}/disposition`
  records a human accept/reject exactly once (second disposition
  409, audited `agents.disposition`).
- Seeded as **drafts** (never self-published/enabled):
  `note-coding-assistant` and `lab-result-summariser`, both with an
  empty model route.
- Web: Studio **Agents (ADK)** tab — definitions with versions and
  lifecycle actions, a runtime-unreachable banner when the service
  is down, a run panel that shows the honest-off error verbatim,
  and a runs table with Accept/Reject disposition.
- httpx service clients use `trust_env=False` (the sandbox's proxy
  env vars otherwise corrupt httpx URL parsing); runtime-health
  probing degrades to "unreachable" instead of ever crashing the
  agents surface.

### WS3 — Track B core: Studio parity essentials

(a) **Rules/automation engine** (`app/rules.py`): rules are governed
config kind **`rule`** binding a server-defined EVENT + a structured
CONDITION (signal leaves with eq/ne/gt/gte/lt/lte/in/contains,
composed with all/any — governed expressions only, no code) to a
server-defined ACTION (`open_critical_notification`, `create_task`,
`audit_alert`). Events today: `lab.report.released`,
`inpatient.census.changed`. Rules evaluate **inside the emitting
write's transaction**; a malformed signal never fires and never
breaks the clinical write; every firing is audited `rules.fired`
with rule key + version. Two seeded published rules prove
generality: `critical-value-notification` — the Phase 2 critical
escalation re-expressed through the engine (at release of a critical
report the rule now materialises the pending notification record
the read-back workflow then completes; behaviour and worklist
unchanged, test-proven end-to-end through a real BMP release) — and
`census-threshold-alert` (occupancy ≥ 90% → a capacity-review Task
owned by bed-management; occupancy recomputed from live bed state
on every ADT event). Tests additionally prove a site-authored rule
fires on a real admission with no code change, and a disabled rule
does not fire. `GET /rules/registry`, `POST /rules/evaluate`
(studio.manage) support operations and testing.

(b) **Pack lifecycle completion** (`app/studio.py`): packs can now
be **authored** in Studio — `POST /studio/packs` (studio.manage)
creates a pack in the database as the create-draft entry point to
the same install → approve → attach → enable lifecycle file packs
use. A manifest declaring any endpoint `enabled: true` is refused
422 (surface ships disabled; enablement is a separate governed
step); duplicate name+version is 409; every step is audited
(`studio.pack.create`, `studio.install`, `studio.approve`,
`studio.attach`, `studio.enable`). `load_pack_manifests` unions
DB-authored packs into listing/install. Test-proven end-to-end.

(c) **Dashboards as config**: kind **`dashboard`** (title + widgets
with id/title/endpoint/display ∈ total|count|value(+field);
duplicate widget ids, external URLs and field-less value widgets
are 422). One published worked example ships seeded:
`operations-overview` (census total, open critical reports,
verification-queue depth). Web: Studio **Dashboards** tab renders
ANY published dashboard config generically; a widget whose endpoint
fails shows "unavailable" and the rest of the dashboard still
renders (test-proven).

Also in Track B scope: four new config kinds registered with
validators (`agent`, `rule`, `dashboard`, `regulatory_pack`) and
four new capabilities (`agents.view`, `agents.run`,
`privacy.breakglass`, `privacy.review`) with server defaults and
the web permissions mirror extended in step (36 → 40).

### WS4 — Track J core: compliance framework

- **Requirements register as data** (`app/compliance.py`, table
  `ComplianceRequirement`): body, citation, title, control
  description, implementation reference, evidence source. Reads are
  studio.read; writes studio.manage, audited
  (`compliance.requirement.create/update`). Seeded with 10 entries:
  HIPAA audit controls §164.312(b), emergency-access
  §164.312(a)(2)(ii), minimum necessary §164.502(b), breach
  notification §164.404/§164.308(a)(6) (60-day clock), documentation
  retention §164.316(b)(2)(i) (6 years), transmission security
  §164.312(e)(1); ONC/Cures §170.315(g)(10) FHIR R4 API,
  §170.315(b)(10) EHI export, 45 CFR Part 171 information blocking,
  §170.315(b)(11) decision-support-intervention transparency
  (evidence: the `agents.run` audit stream).
- **Regulatory packs as governed config** (kind
  `regulatory_pack`), seeded as **drafts**: `hipaa-baseline`
  (parameters: breachNotificationDays 60,
  documentationRetentionYears 6, medicalRecordRetentionYears 6,
  breakGlassDurationMinutes 60, justificationWindowHours 48,
  auditReviewCadenceDays 30, minimumNecessary true) and
  `onc-cures`. The parameters are not decorative: publishing the
  HIPAA pack makes it the live source of the break-glass clocks,
  and publishing a tightened v2 (30 min / 24 h) moves them —
  test-proven. Regulation changed by configuration, not code.
- **Evidence endpoint**: `GET /compliance/requirements/{id}/evidence`
  resolves the declared source live — `audit:<prefix>` returns the
  newest matching hash-chain entries; `config:<kind>/<key>` returns
  the published config summary; `file:<path>` is reported as a
  document reference, never faked into data; a missing source is
  reported as the gap it is.
- **Tracer self-audit v1**: `POST /compliance/tracer` walks one
  synthetic patient and reports pass/fail/not-applicable per named
  control: chart-access-logged (audit entries reference the
  patient), break-glass-reviewed (fails while any opening is
  unreviewed — test-proven fail → review → pass),
  critical-results-completed (released critical reports must be
  acknowledged), eligibility-recorded (prior auths without any
  eligibility check fail).
- Web: Studio **Compliance** tab — register table with per-row
  evidence viewer, regulatory packs with publish actions, and the
  break-glass policy block (duration, justification window, the
  100%-review expectation, and whether the numbers come from the
  published pack or built-in defaults).

### WS5 — Track K core: security

(a) **Break-the-glass** (`app/privacy.py`): `POST /privacy/break-glass`
(capability `privacy.breakglass`: admin/clinician) requires a reason
(422 without), 404s unknown patients, and in ONE transaction writes
the flagged `breakglass.open` audit entry + a review-queue row.
Access is time-boxed from the effective policy (published HIPAA
pack parameters, else labelled built-in defaults 60 min / 48 h).
The review side is a separate capability (`privacy.review`:
admin/configurator) — the opener is never the only reviewer.
`GET /privacy/review-queue` lists EVERY unreviewed opening,
oldest-first, with overdue flags against each item's justification
deadline; review records justified/unjustified exactly once (409 on
re-review, audited `breakglass.review`). The 100%-review
expectation ("every opening reviewed, never sampled") is stated in
the policy API and rendered in the Compliance tab.

(b) **Field-level encryption** (`app/crypto.py`): envelope
encryption — a fresh random AES-256-GCM data key per value, wrapped
under a key-encryption key; the stored envelope `{v, kid, wk, n,
ct}` carries the key id WITH the ciphertext. Key material resolves
from `OPENEHR_MASTER_KEY` (env) → `OPENEHR_MASTER_KEY_FILE` → a dev
key auto-generated at `~/.opencare/dev-master-key` (0600, OUTSIDE
the repo tree). Tampered ciphertext and wrong-kid envelopes raise —
they never decrypt to garbage. Applied to Phase 9 secure-message
bodies: the stored FHIR Communication payload is the envelope JSON
plus an `urn:opencare:extension/encrypted-content` flag carrying
the kid; decryption happens at exactly one point
(`engagement._message_text`) so thread views, inboxes and the
portal all read plaintext while the database holds ciphertext —
proven by a test that reads the raw row and asserts the plaintext
is absent and the envelope decrypts. Pre-Phase-10 plaintext rows
(no flag) still read back (test-proven).
**Production KMS path (documented, not run):** hold the KEK in a
KMS/HSM/Vault; inject it via `OPENEHR_MASTER_KEY_FILE` on a tmpfs
mount (or the env var from the secret manager) so no key material
ever touches the repo or the database; rotation = introduce a new
KEK with a new `kid`, keep old KEKs readable, and let DEKs re-wrap
lazily on next write (envelopes are self-describing, so old rows
keep decrypting under their recorded kid).

(c) **TLS**: `web/nginx.tls.conf` template beside the dev config —
HTTP→HTTPS redirect, TLS 1.2/1.3 + HSTS on 8443, the full proxy
prefix list (Phases 9+10 included), and a deployment note in the
header: terminate with the organisation's CA-issued (or ACME)
certificate at `/etc/nginx/tls/fullchain.pem` + `privkey.pem`,
never a committed certificate. Dev HTTP behaviour unchanged.

(d) **Downtime read-only mode**: published config
`integration/downtime` (seeded published OFF). When a site
publishes it on, a backend guard returns **503 with the configured
downtime notice for every non-GET write except /health and /studio
paths** — Studio stays writable so an operator can always end the
downtime; reads are unaffected, and a guard failure never takes
reads down. Web shell: `RuntimeConfigProvider` fetches the flag
with the same independent-fallback discipline as navigation/
branding/permissions (any failure → off) and renders a read-only
banner with the operator's message above the content. Test-proven
both sides (write 503 with notice, read 200, studio exempt,
recovery on publish-off; banner render + parse strictness).

### WS6 — Hardening: backup/restore drill

`scripts/backup_restore_drill.sh` — **RUN, PASS** (2026-10-17)
against the real dev database `backend/openehr.db`: SQLite online
backup → byte-copy restore → per-table row counts identical across
all **30 tables** (audit_log 131 entries, fhir_resources 21) and
`app.audit.verify_chain` verifies the hash chain on the restored
database. Drill workdir retained at `/tmp/opencare-drill.cap6aG`.

Also this phase: `/health` reports phase "10".

## Configuration-coverage list (standing rule: configure, don't fork)

Everything a site changes about Phase 10 behaviour is governed
Studio configuration data:

1. **Agent definitions** — kind `agent`: model route, instructions,
   tools, API allow-list, modalities, temperature, maxSteps;
   draft → publish → enable; two seeded drafts.
2. **Automation rules** — kind `rule`: event + condition + action;
   two seeded published rules; sites author more with no code.
3. **Dashboards** — kind `dashboard`: title + widgets over existing
   endpoints; seeded `operations-overview`; the Studio view renders
   any published dashboard.
4. **Regulatory packs** — kind `regulatory_pack`: HIPAA baseline +
   ONC/Cures drafts whose parameters drive the break-glass clocks,
   retention and breach-notification numbers.
5. **Downtime mode** — `integration/downtime` published flag +
   operator message; seeded off.
6. **Permissions** — the four new capabilities ride the existing
   published `permissions/role-matrix` like every capability since
   the realignment.
7. **Packs** — authored packs are data (manifest in DB) entering
   the same lifecycle as file packs.
8. **Requirements register** — obligations themselves are data rows,
   editable through the API, not code constants.

Code was written only for genuinely new capabilities: the ADK
runtime service, the rules evaluator, the crypto helper, the
break-glass/tracer endpoints, the downtime guard.

## Compliance-coverage list (new standing requirement, Principle 6)

| Requirement | Where it lands in Phase 10 | Evidence |
|---|---|---|
| HIPAA §164.312(b) audit controls | Register entry; hash-chained audit covers agents/rules/break-glass/compliance writes | `audit:` evidence endpoint |
| HIPAA §164.312(a)(2)(ii) emergency access | Break-the-glass: mandatory reason, time-boxed, flagged audit, review queue | `audit:breakglass.open` |
| HIPAA §164.502(b) minimum necessary | Register entry; capability-gated surfaces; evidence resolves to published `permissions/role-matrix` | `config:permissions/role-matrix` |
| HIPAA §164.404/308(a)(6) breach notification (60 d) | `hipaa-baseline` pack parameter `breachNotificationDays: 60` | pack config |
| HIPAA §164.316(b)(2)(i) documentation retention (6 y) | Pack parameters `documentationRetentionYears`/`medicalRecordRetentionYears: 6` | pack config |
| HIPAA §164.312(e)(1) transmission security | TLS template `web/nginx.tls.conf` | `file:web/nginx.tls.conf` (reference) |
| ONC §170.315(g)(10) FHIR R4 API | Register entry (Phase 0/8 surface, unchanged) | register |
| ONC §170.315(b)(10) EHI export | Register entry; Phase 8 bulk `$export` behind `interop.export` | `audit:interop.export.create` |
| 45 CFR Part 171 information blocking | Register entry over the interop audit stream | `audit:interop.` |
| ONC §170.315(b)(11) DSI transparency | Register entry; every agent run audited with model + version + input hash | `audit:agents.run` |
| Break-glass 100% post-hoc review, 24–48 h justification | Policy API + Compliance tab state the expectation; queue lists EVERY opening; window is pack-configured (48 h default) | review-queue API |
| Field-level protection of message bodies | Envelope encryption at rest, kid recorded, tamper-evident | crypto tests + ciphertext-at-rest test |

## NOT RUN / gaps — honest list

- **Live LLM run: NOT RUN.** No model endpoint exists in this
  environment (no Ollama, no API key). The ADK path is verified to
  the boundary: config parse/validation, allowlist, roster sync,
  and the no-model 503 in the service's own suite; the core's run
  path is verified with the transport substituted. A real model
  answering is unverified — nothing about agent output quality is
  claimed.
- **Postgres/Docker runtime: NOT RUN** (no Docker on this VM) —
  unchanged standing gap; the drill covers the dev SQLite database
  only. Not simulated.
- **Live Keycloak OIDC / MFA proof-run: NOT RUN** — unchanged
  standing gap; dev-token auth remains the exercised path.
- **TLS in a live server: NOT RUN.** The template is delivered and
  reviewed; no certificate exists in this environment to serve it.
- **Browser click-through E2E: NOT RUN** — the Studio tabs are
  covered by component tests at the stub-client boundary, the same
  standing limitation as every prior phase (and the class of gap
  the Phase 9 fetch bug escaped through — recorded, not hidden).
- **Pack-enablement route gating** (a pack's declared endpoints
  being refused while its installation is not enabled) remains the
  documented design gap from the shortfall register: Phase 10
  completed the pack LIFECYCLE (authoring + governed transitions),
  but endpoint enforcement by installation state was not in this
  phase's delivered scope and is NOT claimed.
- **Downtime 503 responses carry no CORS headers** (the guard sits
  outside the CORS middleware). Accepted for v1: the banner is the
  primary UX and the API error still reaches direct clients; noted
  for the hardening list.
- **Snooping detection, idle-session lock, SIEM feed, log
  redaction** (rest of Track K) and the visual designers / full
  pack-marketplace of Track B remain later-phase scope, as the
  roadmap sequences them; the framework pieces they need
  (capabilities, audit stream, policy-from-pack) are in place.
- The Phase 10 smoke anomaly (a FHIR create returning 422 after
  downtime was switched off) was investigated and closed: it was
  Patient validation (`name`/`identifier` required), not the
  downtime guard — the write had correctly passed the guard.

---

## Parent verification addendum (2026-10-18, independently re-run)

- Backend suite re-run by the parent agent with the project invocation
  (`pytest tests/`): **362 passed**, exit 0. (A bare `pytest` from
  `backend/` also collects the ADK service's tests under the wrong venv
  and errors on collection — an invocation artefact, not a failure; the
  ADK suite passes in its own venv.)
- ADK runtime suite re-run in its own venv: **8 passed**.
- Web suite re-run: **211 passed**; `npm run build` re-run clean.
- Spot checks: proxy prefixes present in both `web/vite.config.ts` and
  `web/nginx.conf` (including the Phase 10 groups); `backend/app/crypto.py`,
  `web/nginx.tls.conf`, and `scripts/backup_restore_drill.sh` all present;
  backend `/health` reports phase 10.
- **Real-browser pass: attempted three ways, still NOT RUN.** The hosted
  browser tooling refuses loopback navigation outright; a local headless
  Chromium (the VM's Chromium 152, driven via playwright-core) refuses
  loopback with `ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS` despite
  feature-disable flags and permission grants; the Playwright browser
  download failed repeatedly in this sandbox. This is an environment
  restriction, not an app finding. The definitive browser check for
  Phase 10 happens where the system actually runs — the user's MacBook
  Air, after the Phase 10 delta is synced there. A reusable check script
  is kept at `~/workspace/tools/browser-check/phase10_check.js`.
