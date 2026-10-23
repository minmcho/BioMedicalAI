# Contributing to OpenCare EHR

Thank you for helping build an open-source electronic health record.
OpenCare EHR is a research and development (R&D) system, developed
and tested exclusively with synthetic data (see `DISCLAIMER.md`).
This project has strong engineering rules; they exist because the
software models clinical work. Please read this file before your first
pull request.

## Ground rules (non-negotiable)

1. **Synthetic data only.** Never commit real patient data, real
   credentials, or anything that could be PHI. All seed data, fixtures
   and examples are synthetic. If you think you found real data in the
   tree, stop and follow `SECURITY.md`.
2. **Configure, don't fork.** One codebase serves an academic medical
   centre, a specialty site, and a solo practice. Every difference
   between sites, specialties, or deployments is **System Studio
   configuration data** (versioned, governed: draft → publish,
   published immutable). Code changes are only for genuinely new
   capabilities. If a requirement can't be met by configuration,
   extend the configuration surface — never hardcode a site's need.
3. **Nothing ungoverned goes live.** Studio-built artefacts (packs,
   config, agents, MCP tools, reports, SOPs) are drafts until a human
   publishes and, where applicable, separately enables them.
   AI-generated configuration is always a draft.
4. **Audit everything.** Mutations are hash-chain audited. New write
   paths must write audit entries; audit records carry field names,
   not PHI values.
5. **Honesty in reporting.** A test that was not run is NOT RUN, never
   "passed". A feature that is designed but not built is described as
   designed, not built. No mock clinical behaviour presented as real
   (no faked payer EDI, no faked video, no invented drug-interaction
   knowledge).
6. **Open standards, no proprietary code.** FHIR R4, SMART on FHIR,
   HL7 v2. Do not copy proprietary EHR code, text, or trademarks.

## Architecture in one paragraph

Multi-layered, all open source. Presentation: React web + native
SwiftUI (iOS, Universal) and Kotlin Compose (Android). Protocol
lanes: FHIR REST, MCP, A2A, HL7 v2. Application services: FastAPI
clinical core, Go network/edge services, a Rust compute tier, and the
ADK agent runtime (google-adk + LiteLLM) as a separate service. Data:
Postgres + pgvector primary store with a portable repository layer,
a two-zone data lake (immutable raw zone → curated zone) with
lineage, governed pipelines, and hot/warm/cold tiering policy held
as Studio configuration. System Studio is the configuration layer
for the whole system.

## Development setup

Backend (from `backend/`):

```bash
python -m venv ../.venv && ../.venv/bin/pip install -r requirements.txt
../.venv/bin/uvicorn app.main:app --port 8010
```

ADK agent service (separate venv, from `backend/adk/`):

```bash
.venv/bin/python -m pytest        # its own suite
```

Web (from `web/`):

```bash
npm install
VITE_API_BASE=http://127.0.0.1:8010 npm run dev   # http://127.0.0.1:5173
```

## Running the tests (the SQA gate)

Every change must pass the suites it touches; phase work must pass
all of them.

- Backend, during development: from `backend/`,
  `../.venv/bin/python -m pytest tests/` — invoke it exactly this
  way (a bare `pytest` also collects the ADK service's tests under
  the wrong virtualenv).
- Backend, full gate: the suite is large. Run it as **two sequential
  halves** over `tests/test_*.py` (alphabetical split), never in
  parallel, and clear stale pytest temp dirs first — on small-temp
  filesystems a full single run corrupts results. Report both counts.
- Web: `npm test` and `npm run build` must both be clean.
- ADK service: its own venv's pytest.
- Go services: `go test ./...`. Rust: `cargo test`.

New backend route groups must be added to **both** web proxy configs
(`web/vite.config.ts` and `web/nginx.conf`).

## What a good pull request looks like

- One coherent capability; nothing partial shipped as finished — if
  something must be cut, the PR says so plainly.
- Tests for new behaviour, including the failure paths (validation
  errors, permission denials, hard stops).
- An SQA note in the PR description: what was built, exact test
  counts (command + result), a **configuration-coverage list** (what
  is Studio configuration vs code, and why), a **compliance-coverage
  list** when the change touches a regulated workflow, and an
  explicit NOT RUN list for anything unverified.
- User-facing changes follow the design system: one obvious primary
  action per panel, workflow state visible as labelled status (text,
  never colour alone), inline validation errors, no dead controls,
  dropdowns populated from real data or disabled with the reason
  shown.

## Governance of contributions

By contributing you agree your work is dual-licensed under the
Apache License, Version 2.0 and the MIT License, at the user's
option (see `LICENSE`, `LICENSE-APACHE`, `LICENSE-MIT`). Add
yourself to
`CONTRIBUTORS.md` in your first PR. Maintainers may ask for changes
to meet the ground rules above; clinical-safety and governance rules
are not waived for convenience.

Questions: open a discussion or an issue. Security concerns: see
`SECURITY.md` — never a public issue.
