# Biomedical Imaging

**Computational methods for biomedical image analysis and interpretation — an original, self-directed prep track.**

## Sources & attribution
Topic selection was informed by Stanford's biomedical image analysis coursework
(public catalog descriptions only). No course materials are copied; all code,
exercises, and text in this track are original.

## What this track covers (summary)
The classical computational core of biomedical image analysis: how medical images are
represented and enhanced, how structures are segmented and aligned, how measurements
are extracted, and how an analysis pipeline is evaluated and interpreted. Where
Biomedical Vision AI covers modern vision foundation models, Biomedical Imaging is the methods layer
underneath them — filtering, segmentation, registration, features, and honest
evaluation. Both tracks live in this repo and feed the same imaging project.

## Prerequisites — self-check before starting
- [ ] Python from scratch (Computational Biology Weeks 1-4 cover this)
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
- Runs alongside Biomedical Vision AI in Weeks 5-8 of the 3-month prep (`Bootcamp/PREP_3_MONTHS.md`): 260 supplies the classical methods, 276 the modern models; the Week 8 imaging project can use either or both.
- Repo principles still apply: implement core ideas from scratch before libraries, analyze complexity, write tests, connect every method to a biological/clinical problem. No diagnosis or treatment claims — analysis methods and evaluation only.
