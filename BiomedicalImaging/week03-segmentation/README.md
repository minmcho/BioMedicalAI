# Week 3 — Segmentation

## Goal
Turn intensities into structures: choose a threshold from the data itself
(Otsu), produce a mask, and score it honestly against a reference (Dice).

## Project
Implement from scratch: otsu_threshold(), segment(), dice_score().

## Exercises
1. Construct an image where Otsu fails (three intensity clusters, or a strong
   gradient) and explain which assumption broke.
2. By hand: Dice for masks of sizes 10 and 12 overlapping in 8 pixels.
3. Interpretation: a Dice of 0.85 on a large organ vs 0.85 on a 20-pixel lesion
   — same quality? Write three sentences.

## Biology connection
Segmentation is where images become measurements: tumor volume, cell counts,
vessel area. The evaluation metric is part of the method — a pipeline without
a Dice/AUC against a reference is an opinion, not a result.
