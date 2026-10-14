# Week 2 — Filtering & Enhancement

## Goal
Enhancement as computation: convolution slides a small kernel over the image;
blur suppresses noise, gradient kernels expose boundaries.

## Project
Implement from scratch: convolve2d(), box_blur(), sobel_magnitude().

## Exercises
1. Predict, then verify: box-blur a checkerboard — what survives and why?
2. Complexity: convolution cost in terms of image size N pixels and kernel size k.
   At what kernel size does a separable implementation start to matter?
3. Frequency view (reading): relate blur to low-pass filtering and Sobel to
   high-pass; one paragraph in your own words.

## Biology connection
Preprocessing decides what a segmentation or a radiologist's eye can see:
denoising a low-dose CT vs blurring away a microcalcification is the same
operation with opposite value — parameters are clinical decisions.
