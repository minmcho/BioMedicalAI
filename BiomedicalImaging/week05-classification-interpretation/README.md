# Week 5 — Classification, Evaluation & Interpretation (lesson plan)

## Goal
Close the classical pipeline: features -> simple classifier -> evaluation you
can defend -> an interpretation a clinician could challenge productively.

## Topics
- From features to decisions: threshold rules and logistic-style scoring (concept level)
- Evaluation: confusion matrix, sensitivity/specificity, ROC and AUC, calibration — and why accuracy alone misleads on imbalanced biomedical data
- Interpretation: which features drove the decision, failure cases by subgroup, dataset shift between scanners/sites
- Reporting: dataset provenance + license, split hygiene (same patient never in train and test), limitations section

## Practice (build when this week starts)
1. Implement confusion-matrix metrics from scratch (sensitivity, specificity, precision) with tests on hand-counted examples.
2. Write a one-page failure analysis of the Week 3 segmentation on a hard synthetic case.

## Biology connection
Interpretation is the deliverable: a number without its failure modes is not
usable in biomedicine. This week produces the evaluation habits the FDE
capstone and Biomedical Vision AI project both require.
