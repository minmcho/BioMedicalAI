# Architecture — Enterprise AI Data Deployment

## What this is
A reference enterprise deployment of a biomedical AI data platform, designed the
way a customer (a lab, a clinic network, a health-data team) would actually run
it: on infrastructure they control, with their data isolated, audited, and
gated — not a notebook demo.

## Data flow
Synthea FHIR bundles -> **ingest gate** (PHI scanner blocks identifiers) ->
**FHIR -> OMOP mapping** (shared contract with DataArchitecture/) ->
tenant-scoped warehouse (Postgres) -> AI query layer (Claude API + MCP tools)
-> every action written to the **tamper-evident audit log**.

## Enterprise controls (each one implemented + tested in `app/`)
| Control | Module | Why an enterprise buyer requires it |
|---|---|---|
| Tenant isolation | `tenant_store.py` | One customer's data must never leak to another |
| RBAC, deny-by-default | `rbac.py` | Least-privilege access for viewers/analysts/operators/admins |
| Tamper-evident audit | `audit.py` | Regulators ask "who did what, prove history wasn't edited" |
| PHI ingest gate | `deidentify.py` | Intentions aren't controls; gates are |
| Eval gate | `eval_gate.py` | A model/prompt change that regresses quality must not ship |
| FHIR -> OMOP mapping | `ingest_fhir.py` | Interoperability is the enterprise data contract |

## Deployment shape (deploy/)
Docker Compose: Postgres 16 (no public port, named volume, healthcheck) +
app (non-root, env-only secrets) + Caddy reverse proxy (automatic TLS,
security headers). Mirrors the stack already running Min's Agentic_App on
Oracle Cloud, so operations experience transfers directly.

## SLOs (operating targets once live)
- Availability: 99.5% monthly for the query endpoint (pilot scale)
- Ingest correctness: 100% of bundles either mapped or rejected with a reason — never silently dropped
- Audit: 100% of mutating actions logged; chain verified nightly
- Recovery: nightly pg_dump backup, 14-day rotation; restore tested, not assumed

## Honest status
Core controls: implemented and unit-tested. API service entrypoint, live VM
deployment, and real pilot users are scheduled steps in the 12-week plan
(Weeks 3, 6, 9) — see EXPERIENCE.md for how this becomes experience, not
just code.
