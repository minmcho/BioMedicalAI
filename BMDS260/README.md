# BMDS 260 Preparation

**Stanford BMDS 260 — Computational Methods for Biomedical Image Analysis and Interpretation**
Cross-listed in the Stanford catalog as BMP 260 / BIOMEDIN 260 / CS 235 (Biomedical Image Analysis/Interpretation).

## What the course is (summary)
The classical computational core of biomedical image analysis: how medical images are
represented and enhanced, how structures are segmented and aligned, how measurements
are extracted, and how an analysis pipeline is evaluated and interpreted. Where
BMDS 276 covers modern vision foundation models, BMDS 260 is the methods layer
underneath them — filtering, segmentation, registration, features, and honest
evaluation. Both tracks live in this repo and feed the same imaging project.

## Prerequisites — self-check before starting
- [ ] Python from scratch (BMDS 214 Weeks 1-4 cover this)
- [ ] Basic statistics: mean/variance, histograms, thresholding trade-offs
- [ ] Linear algebra intuition: images as matrices, convolution as a sliding weighted sum
- [ ] No real patient images — public/synthetic data only, license documented per dataset

## Prep roadmap (practices, in order)
1. `week01-image-representation` — grayscale images as matrices, intensity statistics, histograms, min-max normalization, thresholding (code + tests)
2. `week02-filtering-enhancement` — convolution from scratch, box blur, Sobel edge detection (code + tests)
3. `week03-segmentation` — Otsu thresholding from scratch, binary masks, Dice overlap scoring (code + tests)
4. `week04-registration-features` — image registration concepts (translation search, similarity metrics), shape/intensity features (lesson plan)
5. `week05-classification-interpretation` — features to classifier, ROC/AUC thinking, failure analysis and interpretation (lesson plan)
6. `project/` — final project: a classical analysis pipeline on a public biomedical image dataset, evaluated with Dice/AUC and a written interpretation

## How this connects
- Runs alongside BMDS 276 in Weeks 5-8 of the 3-month prep (`Bootcamp/PREP_3_MONTHS.md`): 260 supplies the classical methods, 276 the modern models; the Week 8 imaging project can use either or both.
- Repo principles still apply: implement core ideas from scratch before libraries, analyze complexity, write tests, connect every method to a biological/clinical problem. No diagnosis or treatment claims — analysis methods and evaluation only.
