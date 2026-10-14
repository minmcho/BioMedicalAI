# 12-Week Biomedical AI Prep — FINAL VERSION

**Owner:** Min Myint Cho · **Window:** Mon Oct 12, 2026 → Mon Jan 4, 2027 (12 weeks) + buffer week Jan 5–11, 2027
**Home:** github.com/minmcho/BioMedicalAI — every deliverable below is a folder, commit, or document in this repo.
**Finalized:** Oct 14, 2026. This version supersedes all earlier drafts.

## The goal
Finish the 12 weeks with *checkable evidence*, not coursework notes:
1. BMDS 214 complete (Weeks 5–10 built, tested, pushed)
2. BMDS 260 + BMDS 276 imaging tracks complete, with two evaluated imaging projects
3. A Databricks lakehouse proof (FHIR → OMOP on Delta Lake) — public and linkable
4. One public Claude deployment (API + MCP server + sub-agents) with a published eval report
5. A real-user pilot written up as a deployment case study / playbook
6. Applications submitted to verified FDE / Data Architect roles at the $180k+ base floor (see `tracker/COMP_TARGET.md`)

## How we work (standing pattern)
- **Muse prepares** each week's scaffold, starter code, and tests in the repo first.
- **Min learns**: works the lesson and the by-hand exercises, writes the short interpretation pieces.
- **Together**: review, run tests, push to GitHub. Nothing counts as done until it is pushed and verified.
- Rhythm: ~1–2 focused hours on weekdays + one longer weekend session. Light weeks are marked (Thanksgiving, holidays) — slipping there is planned, not failure.
- Rules: public/synthetic data only (Synthea, MIMIC demo, public image datasets with license noted); no real PHI, ever; no diagnosis/treatment claims; every resume claim must point to a commit, README, or demo in this repo.

## Track map — where each track finishes
| Track | Content | Finishes |
|---|---|---|
| BMDS 214 | W5 DP/edit distance → W6 alignment → W7 FASTA/FASTQ → W8 assembly → W9 single-cell → W10 capstone | Week 6 |
| Anthropic Academy | Claude 101 → Claude API → MCP Intro/Advanced → Agent Skills → Claude Code | Week 6 |
| DataArchitecture | FHIR/OMOP basics → Databricks lakehouse MVP → gold + MLflow → dbt depth | Week 8 |
| BMDS 260 | W1 representation → W2 filtering → W3 segmentation → W4 registration/features → W5 classification/interpretation → project | Week 8 |
| BMDS 276 | W2 representation → W3 generative → W4 vision-language → W5 applications → imaging project | Week 8 |
| Claude deployment | API build → MCP server → eval set → pilot → case study | Week 10 |
| Portfolios | GenomicsRAG (W9), ClinicalML (W10), FDE capstone write-up (W10) | Week 10 |
| Applications | Databricks Nov 20 → Anthropic W9 → sprint W10 → loops W11–12 | Week 12 |

---

## Week 1 — Mon Oct 12 – Sun Oct 18 · Foundations
- **Build:** BMDS 214 Week 5 — dynamic programming & edit distance (`BMDS214/week05-dynamic-programming/`) ✓ built, 9 tests pass
- **Learn:** Anthropic Academy — create account, start *Claude 101*; environment setup (Python, venv, git clone of this repo)
- **Min's tasks:** Week 5 Exercise 1 by hand (DP table for ACG → AGG, predict then verify); 300-word biological interpretation draft
- **Done when:** worksheet + interpretation committed; Academy account active

## Week 2 — Mon Oct 19 – Sun Oct 25 · Alignment + clinical data
- **Build:** BMDS 214 Week 6 — sequence alignment: Needleman–Wunsch (global) and Smith–Waterman (local) with traceback (`BMDS214/week06-alignment/`)
- **Learn:** FHIR R4 core resources + OMOP CDM core tables (Stanford/UCSF material, linked only); Academy — *Building with the Claude API*
- **Setup:** Databricks Free Edition account; first Delta Lake notebook (create/read/time travel)
- **Done when:** alignment module + tests pushed; FHIR→OMOP mapping notes in `DataArchitecture/`; Delta notebook exported to the repo

## Week 3 — Mon Oct 26 – Sun Nov 1 · Sequencing QC + lakehouse MVP
- **Build:** BMDS 214 Week 7 — FASTA/FASTQ parsing & QC metrics (`BMDS214/week07-fastq-qc/`)
- **Build:** Lakehouse MVP — Synthea FHIR JSON → bronze (raw) → silver (OMOP-shaped) Delta tables on Databricks; notebooks exported to `DataArchitecture/`
- **Learn:** Academy — *Introduction to MCP*
- **Done when:** QC module + tests pushed; bronze/silver tables exist with a README diagram

## Week 4 — Mon Nov 2 – Sun Nov 8 · Assembly + gold layer · applications open
- **Build:** BMDS 214 Week 8 — genome assembly & de Bruijn graphs (`BMDS214/week08-assembly/`)
- **Build:** Lakehouse gold table + one MLflow-tracked run; lakehouse README frozen and linkable
- **Learn:** Academy — *MCP: Advanced Topics*
- **Apply:** applications open — only roles verified live on the employer's site and clearing the $180k base floor
- **Done when:** assembly module pushed; gold table + MLflow run linked from `DataArchitecture/README.md`

## Week 5 — Mon Nov 9 – Sun Nov 15 · Single-cell + imaging begins + Claude project starts
- **Build:** BMDS 214 Week 9 — single-cell RNA-seq algorithms (normalization, PCA intuition, clustering) — ties to Min's dissertation (`BMDS214/week09-single-cell/`)
- **Build:** BMDS 260 Week 1 (image representation) + Week 2 (filtering/Sobel) — code + tests already in `BMDS260/`; Min works the exercises
- **Learn:** BMDS 276 Week 2 — representation learning; Academy — *Introduction to Agent Skills*
- **Build:** Claude project starts — Claude API tool use + structured outputs on a biomedical task (`ForwardDeployment/03-anthropic-fde/` project folder)
- **Done when:** single-cell module pushed; BMDS 260 W1–2 exercises done; Claude API demo runs end to end

## Week 6 — Mon Nov 16 – Sun Nov 22 · BMDS 214 capstone + Databricks application
- **Build:** BMDS 214 Week 10 — capstone: end-to-end mini pipeline (reads → QC → k-mers → alignment) (`BMDS214/week10-capstone/`) — **BMDS 214 complete**
- **Build:** BMDS 260 Week 3 — Otsu segmentation + Dice (code + tests in `BMDS260/week03-segmentation/`; Min works the exercises)
- **Build:** Claude project — public MCP server + sub-agents live
- **Learn:** Academy — *Claude Code in Action* — **Academy core complete**
- **⭐ Milestone — Fri Nov 20: submit Databricks Solutions Architect (Healthcare/Life Sciences) application** with the lakehouse proof linked (deadline Dec 2)
- **Done when:** capstone pushed; MCP server public; Databricks application submitted

## Week 7 — Mon Nov 23 – Sun Nov 29 · Vision models (LIGHT — Thanksgiving Thu Nov 26)
- **Learn:** BMDS 276 Week 3 — generative models; Week 4 — vision-language models (written analyses, discussion style)
- **Build:** BMDS 260 Week 4 — registration & features practice (translation search + region features, per the lesson plan in `BMDS260/week04-registration-features/`)
- **Build:** Claude project — graded eval set v1 (cases, grader, baseline scores)
- **Done when:** two 276 analyses committed; registration practice + tests pushed; eval set v1 published

## Week 8 — Mon Nov 30 – Sun Dec 6 · Imaging projects + data depth
- **Build:** BMDS 276 imaging project — run on a public histopathology patch dataset; results + failure examples
- **Build:** BMDS 260 Week 5 (classification & interpretation) + classical pipeline project — evaluated with Dice/AUC; final report compares classical (260) vs learned (276) on the same data — **both imaging tracks complete**
- **Build:** DataArchitecture depth — dbt project on Databricks + governance/lineage pack
- **Note:** Wed Dec 2 is the Databricks posting deadline — application already in since Nov 20
- **Done when:** both project reports committed; dbt project runs against the lakehouse

## Week 9 — Mon Dec 7 – Sun Dec 13 · Pilot + Anthropic application
- **Run:** Claude deployment pilot with real users (invite-only); log usage, failures, fixes
- **Write:** deployment case study + playbook draft (problem, architecture, evals, rollout, lessons)
- **Build:** GenomicsRAG portfolio finished — genomics RAG with its eval harness green (`GenomicsRAG/`)
- **⭐ Milestone: submit Anthropic FDE (Applied AI) application** — live demo + MCP server + eval report linked; fit scorecard re-scored honestly (target 15+/20)
- **Done when:** pilot running with named users; case study draft committed; Anthropic application submitted

## Week 10 — Mon Dec 14 – Sun Dec 20 · Evidence published · applications sprint
- **Publish:** pilot results, eval report, deployment playbook (final versions in the repo)
- **Build:** ClinicalML portfolio finished (`ClinicalML/`); FDE capstone write-up — LIMS/lab deployment story (`ForwardDeployment/02-fde-capstone/`)
- **Apply:** applications sprint 2 — FDE / Data Architect roles, each verified live + salary-checked before tailoring
- **Done when:** playbook + both portfolios final; sprint applications submitted

## Week 11 — Mon Dec 21 – Sun Dec 27 · Interview prep (LIGHT — holidays)
- **Practice:** coding — DP and alignment problems (Weeks 5–6 material returns), Python fluency drills
- **Practice:** deployment-case rehearsal — walk the Week 9 pilot as an interview story; data-architecture design questions from the lakehouse project
- **Done when:** two mock sessions logged in `Bootcamp/tracker/INTERVIEW_PREP.md`

## Week 12 — Mon Dec 28 – Mon Jan 4 · Loops + final review
- **Run:** interview loops as scheduled; same-day retro notes per loop
- **Review:** final portfolio review — every claim on the resume maps to a repo artifact; fix or delete unbacked claims
- **Write:** 12-week retrospective + next-quarter plan
- **Done when:** retrospective committed; repo is in its final, presentable state

## Buffer — Tue Jan 5 – Mon Jan 11
Interviews, follow-ups, and negotiation — against the comp floor, in writing. Any week that slipped gets finished here, in the order: lakehouse → Claude deployment → imaging projects → BMDS 214.

---

## Milestones at a glance
- **Week 1:** edit-distance engine live; Academy started
- **Fri Nov 20 (Week 6):** Databricks application submitted (posting deadline Wed Dec 2)
- **Week 6:** BMDS 214 complete; MCP server public; Academy core complete
- **Week 8:** BMDS 260 + BMDS 276 complete, two evaluated imaging projects
- **Week 9:** pilot live; Anthropic FDE application submitted
- **Week 10:** playbook + eval report published; portfolios complete
- **Week 12:** interview loops; final portfolio review

## Definition of done for the 12 weeks
- [ ] BMDS 214 Weeks 5–10 in the repo, all tests green
- [ ] BMDS 260 practices + project report; BMDS 276 analyses + imaging project report
- [ ] Lakehouse: bronze/silver/gold Delta tables + MLflow run + README, all linked
- [ ] Claude deployment: live demo, public MCP server, published eval report
- [ ] Pilot case study + deployment playbook published
- [ ] Databricks + Anthropic applications submitted; sprint applications logged in `tracker/JOB_PIPELINE.md`
- [ ] Resume contains only claims backed by artifacts in this repo
