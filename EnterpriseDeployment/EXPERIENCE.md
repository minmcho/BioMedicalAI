# From Project to Experience — the honest pathway

Code in a repo is a *project*. Employers pay for *experience*. The difference
is operation: real environment, real users, real incidents, real numbers.
This deployment is run as experience, in four reps:

## Rep 1 — Deploy it like a customer would (prep Week 3)
Stand the compose stack up on the Oracle VM (separate project from
Agentic_App), with a real hostname + TLS, secrets in a host .env, database
unexposed. Deliverable: RUNBOOK.md filled with the actual commands used,
the actual health-check output, the actual problems hit.

## Rep 2 — Operate it (prep Weeks 4-8)
Weekly operations, logged in OPS_LOG.md: backups + one restore test,
an update/rollback drill, audit-chain verification, eval gate run on every
change. Numbers accumulate: uptime, ingest counts, eval scores over time.

## Rep 3 — Give it users who are not you (prep Week 9)
Invite-only pilot: lab colleagues / professional contacts run real queries
on synthetic-but-realistic data. Their feedback and failures drive fixes.
This is the rep that converts "built" into "deployed for users" — the exact
line FDE postings screen for.

## Rep 4 — Tell it with evidence (prep Week 10)
Case study + playbook: problem, architecture, controls, eval results,
pilot numbers, incidents and what changed. Every claim links to a commit,
a log, or the live demo. This document is what gets attached to the
Anthropic and Databricks-style applications.

## What this is NOT
Not a course, not a certificate, not a simulation. If a rep doesn't happen,
the case study says so — the resume only claims the reps actually done.
