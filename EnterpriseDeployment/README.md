# EnterpriseDeployment — True Enterprise-Level AI Data Deployment

The deployment spine of the 12-week prep: a biomedical AI data platform built
and operated the way an enterprise customer would run it — tenant isolation,
RBAC, tamper-evident audit, a PHI ingest gate, an eval gate, a FHIR -> OMOP
data contract, and a Compose deployment stack (Postgres + Caddy TLS) for a
real VM.

- `ARCHITECTURE.md` — design, data flow, control table, SLOs, honest status
- `EXPERIENCE.md` — how this becomes *experience*: deploy -> operate -> pilot users -> case study
- `RUNBOOK.md` — deploy, update/rollback, backup/restore, incident basics
- `app/` — the six enterprise controls, each implemented from scratch
- `tests/` — the contracts as tests (8 passing): tenant isolation, RBAC,
  audit tamper detection, PHI blocking, FHIR->OMOP mapping, eval gating
- `deploy/` — Dockerfile, docker-compose.yml, Caddyfile, .env.example

Public/synthetic data only. No real PHI. No diagnosis/treatment claims.
