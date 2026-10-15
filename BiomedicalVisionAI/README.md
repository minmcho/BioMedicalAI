# Biomedical Vision AI

**Vision foundation models for biomedicine — an original, self-directed prep track.**

## Sources & attribution
Topic selection was informed by Stanford's advanced topics course in computer
vision and biomedicine (course site: https://web.stanford.edu/class/bmds276/).
Only the public syllabus/course site was consulted; no course materials are
copied. All code, exercises, and text in this track are original.

## What this track covers (summary)
An advanced AI course on computer-vision methods for visual data in biomedicine: families of vision foundation models (representation learners, diffusion and other generative models, vision-only and vision-language models), plus real-world deployment considerations such as model size and compute, training/inference settings, and training data. Learning is by written analyses of recent work, discussion, and a substantial final project.

## Prerequisites — self-check before starting
The source course expects comfort with deep learning and computer vision at about the CS231N / BIODS 220 level. Honest self-check:
- [ ] CNNs, training loops, augmentation, transfer learning (PyTorch)
- [ ] Transformers / attention basics
- [ ] Evaluation: ROC/PR, calibration, dataset shift, subgroup analysis
- [ ] One prior imaging project (pathology, radiology, or pose/vision work counts)

## Prep roadmap
This track mirrors the shape of a graduate course: assignments, written analyses, and a substantial final project:
1. `week01-vision-foundations` — image tensors, patching, normalization (code + tests, from scratch first)
2. `week02-representation-learning` — embeddings, self-supervised ideas, retrieval over image patches
3. `week03-generative-models` — diffusion/generative concepts, synthetic-data risks in biomedicine
4. `week04-vision-language-models` — image-text models, zero-shot classification, report-style prompting, failure modes
5. `week05-biomedical-applications` — pathology / radiology / retinal imaging case analyses (written, discussion-style)
6. `project/` — final project: proposal, milestone report, final report + demo

## How this connects
- Feeds `ClinicalML/` and the FDE capstone: an imaging model is only done when it has an eval report and a deployment note.
- Natural project substrate: digital pathology imaging (whole-slide patches) — no real patient images, public datasets only (e.g. public histopathology patch datasets), with source + license documented per dataset.
- Repo principles still apply: implement core ideas from scratch before libraries, analyze complexity, write tests, connect every method to a biological/clinical problem.

## Current status
Track added 2026-10-12 — Week 1 (vision foundations) scaffold ready.
