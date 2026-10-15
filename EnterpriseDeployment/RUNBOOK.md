# Runbook — Enterprise Deployment (Oracle VM)

## First deploy
1. On the VM: clone this repo, `cd EnterpriseDeployment/deploy`
2. `cp .env.example .env` and set real values (long random POSTGRES_PASSWORD)
3. Point the Caddyfile hostname at the real domain (DNS A record first)
4. `docker compose up -d --build`
5. Verify: `docker compose ps` (db healthy), `curl https://<host>/health`
6. Record the deploy in OPS_LOG.md: date, commit, health output, issues

## Update / rollback
- Update: `git pull && docker compose up -d --build`, re-run eval gate first
- Rollback: check out the previous commit, same compose command; data volume is untouched by code rollbacks

## Backup / restore
- Nightly: `docker compose exec db pg_dump -U $POSTGRES_USER $POSTGRES_DB | gzip > backup-$(date +%F).sql.gz` (cron, 14-day rotation)
- Restore test (monthly, into a scratch database — never over production):
  `gunzip -c backup-YYYY-MM-DD.sql.gz | docker compose exec -T db psql -U $POSTGRES_USER scratch`

## Incident basics
- App down: `docker compose logs app --tail 100`; check db health first
- Audit concern: run the chain verification before trusting any history
- Suspected data cross-over: stop ingest, preserve logs, check tenant scoping tests
