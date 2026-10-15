# BioMedicalAI

A hands-on computational biomedical science portfolio and learning repository.

## Computational Biology Preparation

**Algorithms for Computational Molecular Biology — built from scratch in Python**

This track develops strong programming and algorithmic skills through computational molecular biology projects. Topic selection was informed by Stanford's computational molecular biology coursework; every implementation, exercise, and word here is original.

### Roadmap

1. Python & DNA Sequence Toolkit
2. Data Structures
3. Algorithm Analysis
4. k-mer Analysis
5. Dynamic Programming & Edit Distance
6. Sequence Alignment
7. FASTA/FASTQ & Sequencing QC
8. Genome Assembly & Graph Algorithms
9. Single-Cell RNA-seq Algorithms
10. Computational Molecular Biology Capstone

## Repository principles

- Implement core algorithms from scratch before using specialized libraries.
- Analyze time and space complexity.
- Write unit tests.
- Benchmark implementations on increasing data sizes.
- Connect every computational method to a biological problem.

## Current status

Computational Biology: Weeks 1-4 complete - Week 5 (dynamic programming & edit distance) in progress

## FDE / Data Architect Bootcamp (Oct 2026 - Jan 2027)

A 3-month, job-targeted track added 2026-10-12, built from Harvard / Stanford / UCSF public content and aimed at Forward Deployed Engineer and Data Architect roles in biomedical and health-data fields. Existing tracks above (Computational Biology, ForwardDeployment/01) continue unchanged and feed into it.

- `Bootcamp/` — 12-module curriculum, job pipeline (36 verified postings), skills-gap log, interview prep
- `ClinicalML/` — Portfolio 1: clinical ML on synthetic/public data (Synthea / MIMIC demo)
- `GenomicsRAG/` — Portfolio 2: genomics pipeline + biomedical RAG with eval harness
- `DataArchitecture/` — FHIR -> OMOP in dbt: the recurring gap in real postings
- `ForwardDeployment/02-fde-capstone/` — Portfolio 3: FDE-style deployment into a LIMS/lab workflow

## Open_EHR — Open-Source EHR (OpenCare)

An original, open-source (Apache-2.0) EHR with feature categories similar to large enterprise EHR suites — built FHIR-native from day one, in `Open_EHR/`. Added 2026-10-15; see [`Open_EHR/README.md`](Open_EHR/README.md) for the full guide and `Open_EHR/docs/` for the roadmap and SQA reports.

- **Design:** FastAPI clinical core (PostgreSQL primary behind a portable repository layer — other RDBMS, document, pgvector and Valkey/Redis-API stores by tier) with Go planned as the high-throughput API/integration tier and Rust for performance-critical compute (DICOM, de-identification, terminology); APIs each in their own lane — FHIR R4 REST, GraphQL, gRPC, MCP — and native SwiftUI (iOS) + Kotlin (Android) mobile. 100% open-source stack.
- **System Studio concepts, reused:** versioned module packs install as designer drafts only and go live only through separate human steps — approve → attach → enable — with pack endpoints disabled until enabled, role gates, and a hash-chained audit log.
- **Status:** Phase 0 (foundation: pack lifecycle, RBAC, audit, FHIR Patient/Encounter/Observation) and Phase 1 (core EHR: Condition, Procedure, MedicationRequest, DiagnosticReport, Appointment scheduling/ADT, results inbox, patient-portal read scoping, Keycloak-ready OIDC auth) are complete — **37 tests passing**, with a written SQA report per phase (`Open_EHR/docs/sqa/`). Next: Lab LIS, then the FHIR → OMOP warehouse.

## Biomedical Vision AI Preparation

Vision foundation models (representation, generative, and vision-language) and their real-world use in biomedicine. Prep track in `BiomedicalVisionAI/` — vision foundations, representation learning, generative models, vision-language models, biomedical applications, and a final project. Added 2026-10-12; feeds the ClinicalML portfolio and the FDE capstone. Topic selection informed by Stanford's advanced computer-vision-in-biomedicine course; all material here is original.

## Biomedical Imaging Preparation

The classical methods layer under Biomedical Vision AI — image representation and intensity statistics, convolution/filtering and Sobel edges, Otsu segmentation with Dice evaluation (all from scratch, code + tests), then registration & features and classification & interpretation, closing with a classical-pipeline final project on a public dataset. Prep track in `BiomedicalImaging/`. Added 2026-10-14; runs alongside Biomedical Vision AI in Weeks 5–8 of the 3-month prep. Topic selection informed by Stanford's biomedical image analysis coursework; all material here is original.
