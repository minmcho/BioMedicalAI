# Week 4 — Registration & Features (lesson plan)

## Goal
Align images of the same structure (registration), then summarize regions with
numbers a classifier or a clinician can use (features).

## Topics
- Why register: longitudinal scans, multi-modal fusion (e.g. MRI + PET), atlas mapping
- Transformation families: rigid (rotation/translation), affine, deformable — and what each assumes
- Similarity metrics: sum of squared differences, cross-correlation, mutual information (concept level)
- Brute-force translation search as the from-scratch exercise: shift one small image over another, score each shift, pick the best
- Features from a mask + image: area, perimeter, mean/std intensity, centroid, eccentricity (concept level)

## Practice (build when this week starts)
1. Implement translation search on nested lists with SSD scoring; test on a shifted synthetic pattern.
2. Implement region_features(mask, image) returning area/centroid/mean intensity; unit tests on hand-computed regions.

## Biology connection
Registration errors look like biology (a "growing" tumor that is really a
misaligned scan). Features are the bridge from classical analysis to ML:
Weeks of Biomedical Vision AI models still get evaluated against measurements like these.
