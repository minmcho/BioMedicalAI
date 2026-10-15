# Week 1 — Image Representation & Intensity Basics

## Goal
Treat a biomedical image as data: a matrix of intensity values with statistics
you can compute, plot, and reason about before any modeling.

## Project
Implement from scratch: validate(), intensity_stats(), histogram(),
normalize_minmax(), threshold_binary().

## Exercises
1. By hand: compute the mean and the 4-bin histogram of a 3x3 image you invent; check with code.
2. Explain in two sentences why the same tissue can have different raw intensities on two scanners, and what min-max normalization does and does NOT fix.
3. Complexity: histogram() is O(?) in pixels and bins — state both costs.

## Biology connection
Every downstream method (segmentation, registration, ML) inherits the intensity
scale of the acquisition. Understanding representation first is what separates
analysis from button-pushing.
