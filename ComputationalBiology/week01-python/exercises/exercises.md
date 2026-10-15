# Week 1 Exercises

## Exercise 1 — Nucleotide counting

Implement `nucleotide_count(sequence)` without using `collections.Counter`.

Input: `ATGCGTACG`

Expected counts:

```text
A = 2
C = 2
G = 3
T = 2
```

## Exercise 2 — GC content

Calculate the GC percentage of `ATGCGTACG` by hand before running Python.

## Exercise 3 — Reverse complement

Calculate the reverse complement of `ATGCCGTA` by hand.

## Exercise 4 — Transcription

Convert `ATGCCGTA` to RNA.

## Exercise 5 — Translation

Translate `ATGGCCATT` using the standard genetic code.

## Exercise 6 — Complexity

Explain the time and space complexity of each function in `dna.py`.

## Exercise 7 — Edge cases

Add tests for:

- lowercase input
- whitespace around input
- empty input
- invalid characters
- a one-base sequence
- a sequence whose length is not divisible by 3

## Exercise 8 — Algorithm design

Rewrite `nucleotide_count()` using one pass through the sequence. Compare its complexity with the current implementation.

## Exercise 9 — Benchmark

Generate random DNA sequences with lengths 100, 1,000, 10,000, 100,000, and 1,000,000. Measure runtime for the main functions.

## Exercise 10 — Biology explanation

Write 200–300 words explaining why reverse complements are important when working with DNA sequencing data.
