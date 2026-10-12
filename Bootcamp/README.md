# Biomedical AI/ML — FDE / Data Architect Bootcamp (3 months)

**Owner:** Min Myint Cho — Union City, CA (Bay Area)
**Window:** Mon 2026-10-12 to Mon 2027-01-11 (12 modules over 13 calendar weeks, 1 buffer week)
**Targets:** Forward Deployed Engineer (FDE) and Data Architect roles in biomedical, health-data, genomics, and lab informatics.
**Wedge:** 18 years LIMS / lab systems (CA environmental chemistry lab) + DCS Big Data Analytics (2025) + MS Data Science/ML (2021) + ML for breast-cancer genomics dissertation + a production AI-agent / data platform (Agentic_App: Postgres, multi-source connections, pipelines, RAG, eval harness).

## How this bootcamp works
- Study sources are *linked*, not copied — Harvard, Stanford, and UCSF public courses/programs below.
- Every module ends in code in one of the 3 portfolio repos, not just notes.
- Apply in parallel from Week 4 — do not wait until Week 12.

## Source map (link + summarize only)
### Harvard
- Harvard Medical School Executive Education — AI in Health Care: From Strategies to Implementation (8-week, capstone): https://execonline.hms.harvard.edu/artificial-intelligence-in-health-care-from-strategies-to-implementation
- Harvard Online — Digital Health: AI and Emerging Technologies in Health Care: https://harvardonline.harvard.edu/course/digital-health
- Harvard course catalog (Data Science, CS50 AI with Python, Genetics Essentials): https://pll.harvard.edu/catalog/701+709/all

### Stanford
- Stanford AI in Healthcare Specialization (Coursera): Introduction to Clinical Data; Fundamentals of Machine Learning for Healthcare; Evaluations of AI Applications in Healthcare
- Stanford Online — Agentic AI (CS224V): https://online.stanford.edu/courses/cs224v-agentic-ai
- Stanford CS224N (NLP with Deep Learning), CS229 (Machine Learning), CS336 (Language Modeling from Scratch) — public lectures/assignments
- Stanford Center for AI in Medicine and Imaging (AIMI) — Grand Rounds / education

### UCSF
- UCSF Certificate in Health Data Science (CiHDaS): https://catalog.ucsf.edu/programs/health-data-sci-certificate/
- UCSF Biological and Medical Informatics (BMI) program: https://catalog.ucsf.edu/programs/biological-medical-informatics/
- UCSF Biomedical Informatics courses (BIO MD INF 203 Biocomputing Algorithms, 206 Statistical Methods for Bioinformatics): https://catalog.ucsf.edu/course-catalog/bio_md_inf/
- UCSF Bakar Computational Health Sciences Institute (BCHSI) + UCSF/UC Berkeley Bakar Computational Biomedicine Initiative: https://www.ucsf.edu/news/2026/07/432166/new-program-ucsf-and-uc-berkeley-will-drive-advances-medicine
- UCSF Clinical Informatics, Data Science and AI (CIDS-AI) Pathway: https://meded.ucsf.edu/current-residents-fellows-overview/gme-pathways/gme-pathways/clinical-informatics-data-science-and-artificial-intelligence-pathway

## 12 modules
| Wk | Dates (2026-27) | Theme | Source anchor | Deliverable |
|---|---|---|---|---|
| 1 | Oct 12-18 | Audit + Python/SQL/data-modeling sprint | Harvard Data Science | Skills gap log, env, repo hygiene |
| 2 | Oct 19-25 | Clinical / EHR data, FHIR + OMOP basics | Stanford Intro to Clinical Data; UCSF CiHDaS | Portfolio 1: data profile |
| 3 | Oct 26-Nov 1 | ML for healthcare | Stanford Fundamentals of ML for Healthcare | Portfolio 1: baseline model |
| 4 | Nov 2-8 | Evaluation, fairness, regulation, privacy | Stanford Evaluations of AI in Healthcare; Harvard AI in Health Care | Eval report; **start applying** |
| 5 | Nov 9-15 | Genomics / bioinformatics | UCSF BMI 203/206; Harvard Genetics | Portfolio 2: genomics pipeline |
| 6 | Nov 16-22 | Biomedical NLP / LLMs | Stanford CS224N | Portfolio 2: LLM extraction |
| 7 | Nov 23-29 | RAG + agents for biomed knowledge | Stanford CS224V; Harvard Digital Health | Portfolio 2: RAG + eval harness |
| 8 | Nov 30-Dec 6 | Data architecture: lakehouse, pipelines, governance | UCSF BCHSI; his Agentic_App data platform | Architecture doc + dbt/Airflow-style pipeline in Capstone repo |
| 9 | Dec 7-13 | FDE craft: discovery, 48-hr POC, stakeholder demos | FDE postings pattern (production LLM, customer-facing) | Capstone: deployed lab-workflow POC |
| 10 | Dec 14-20 | Portfolio polish | All | READMEs, demo videos, architecture diagrams |
| 11 | Dec 21-27 | Interview prep (light, holidays) | — | Data-design + FDE case practice log |
| 12 | Dec 28-Jan 4 | Capstone demo + application sprint | — | Live demo, 15+ tailored applications |
| Buffer | Jan 5-11 | Interviews / catch-up | — | Interview loop |

## Portfolio repos (siblings)
1. `clinical-ml-ehr-risk` — clinical ML on public/demo data only (Synthea / MIMIC demo)
2. `genomics-llm-rag` — genomics + biomedical RAG with evals
3. `fde-lab-deployment-capstone` — FDE-style deployment into a LIMS/lab workflow

## Rules
- No real PHI, ever. Public / synthetic data only.
- No diagnosis/treatment claims — prediction / workflow support only, hedged.
- Every claim on a resume must point to a commit, README, or demo in these repos.

## Added 2026-10-12 — BMDS 276 (Stanford, CS 286)
Advanced Topics in Computer Vision and Biomedicine is now a parallel advanced track (`../BMDS276/` in BioMedicalAI). Slot: Weeks 5-7 alongside GenomicsRAG for the imaging half; its final project can serve as Portfolio 1's imaging extension. Prereq honesty check: CS231N-level deep learning/CV first.
