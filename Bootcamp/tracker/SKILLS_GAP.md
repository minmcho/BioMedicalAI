# Week 1 skills audit — DRAFT prepared 2026-10-12 (research stage, not yet committed to BioMedicalAI)

Bootcamp Week 1 (Oct 12–18, 2026) deliverable per Bootcamp/README.md: "Skills gap log, env, repo hygiene."
Existing Bootcamp/tracker/SKILLS_GAP.md is a 3-line stub. This draft expands it using ONLY evidence
already gathered on 2026-10-12: the 36 verified postings (3 batches), the Anthropic FDE scorecard
(9/20), and the rebuilt resume. No new claims about Min's skills are invented; items marked
[MIN TO CONFIRM] need his yes/no before this is committed.

## Strong — evidenced, resume-ready
- LIMS / regulated lab domain: 18 yrs, CA DTSC Environmental Chemistry Lab; LabWorks/SLIMS,
  instrument integration (GC/MS, LC/MS, FTIR, Raman, XRF)
- Data modeling + Postgres: multi-tenant designs, Agentic_App Postgres data layer, migrations,
  tenant isolation (518/518 tests at completion)
- Pipelines / warehousing: instrument-output warehousing, Azure Data Factory/Data Lake prototype,
  Agentic_App pipelines + materialized warehouse layer
- AI delivery: RAG (chunk/embed/retrieve), eval harness with regression verdicts, fine-tune data
  pipeline, agent workflows — all in Agentic_App, audit-logged and role-gated
- Stakeholder / FDE-style delivery: requirements with scientists, training materials + delivery,
  user support; PMP + iMBA (2026)

## Gaps — ranked by how often the 36 postings demanded them
1. **External-customer AI deployment** — #1 gap in 9 of 12 FDE postings. No GenAI deployment
   inside an external customer / health-system environment yet.
   Fix: Bootcamp Weeks 7–10 pilot + case study (Anthropic roadmap); Agentic_App invite-only beta
   is the nearest real deployment evidence. [MIN TO CONFIRM: beta status line for resume]
2. **FHIR / HL7 / OMOP depth** — core for Verily, HealthLeap, OpenAI Healthcare FDE.
   LIMS is not EHR; vocabulary gap, not a domain gap.
   Fix: Week 2 clinical data + DataArchitecture FHIR R4 → OMOP CDM in dbt (already in repo).
3. **Databricks / Snowflake at production scale** — Collective Health (Databricks lakehouse is
   the role's centerpiece), Covetrus, Databricks SA itself.
   Fix: Week 8 lakehouse project (FHIR→OMOP in dbt on Databricks/Snowflake free tiers).
4. **dbt in production** — currently project-level only (resume states this honestly).
   Fix: same Week 8 project; do not upgrade resume wording until it ships.
5. **Pre-sales / quota-carrying Solutions Architect record** — Databricks SA gap only;
   not a blocker for Data Architect or most FDE roles.
6. **Palantir Foundry** — essential ONLY for Novartis Data42 + Palantir postings (both also
   require relocation). Deliberately NOT a bootcamp priority.

## Scorecard tie-in (Anthropic FDE, 9/20 on 2026-10-12)
Four proof artifacts move the score toward ~15/20 by Week 7:
public Claude app · public MCP server with sub-agents · published eval report · pilot case
study written as a deployment playbook. These outrank any additional coursework.

## Week 1 remaining checklist (from tracker/WEEKLY_LOG.md — status unconfirmed)
- [ ] Env setup (Python 3.12, uv/venv, Jupyter) — Min may have done this; not verified
- [ ] Skills audit vs FDE + Data Architect postings — THIS DRAFT, pending Min's review
- [ ] Pick 10 target postings — pipeline already ranks them; Collective Health staleness
      check and HCAI salary/eligibility check gate the top two (see separate notes)

## Next step for Min
Review the [MIN TO CONFIRM] items and the gap ranking; on approval this replaces the
SKILLS_GAP.md stub in BioMedicalAI/Bootcamp/tracker/ and Week 1's audit box can be checked.
