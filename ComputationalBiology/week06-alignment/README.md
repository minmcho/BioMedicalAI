# Week 6 — Sequence Alignment (Needleman–Wunsch & Smith–Waterman)

## Goal
Move from Week 5's single distance number to scored alignment: place two
sequences against each other so homologous positions line up, using
match/mismatch scores and gap penalties — globally (Needleman–Wunsch,
end to end) and locally (Smith–Waterman, best-matching region only).

## Project
Build an alignment engine from scratch.

Implement:

```python
needleman_wunsch_table(a, b, match=2, mismatch=-1, gap=-2)
needleman_wunsch(a, b, ...)        # -> (aligned_a, aligned_b, score)
smith_waterman_table(a, b, ...)
smith_waterman(a, b, ...)          # -> (aligned_a, aligned_b, score)
alignment_score(aligned_a, aligned_b, ...)
percent_identity(aligned_a, aligned_b)
```

## Experiments

- Verify the gap-placement case by hand: `ACGT` vs `AGT` aligns as
  `ACGT / A-GT` with score 4 under the default scheme.
- Gap-penalty sensitivity: align the same pair at gap = -1, -2, -5 and
  record where the alignment changes shape. There is no "correct"
  penalty — it is a modelling choice about how costly indels are
  relative to substitutions, and the worksheet asks you to say so in
  your own words.
- Global vs local on a read embedded in a longer reference
  (`TTTACGTAAA` vs `ACGT`): Needleman–Wunsch must pay for the flanks;
  Smith–Waterman ignores them. Note the score difference and why.
- Runtime: the table is still O(n*m) — confirm empirically at 100–1600 bp,
  as in Week 5, and note that the traceback is only O(n+m).

## Biology connection

This is the algorithmic core of read mapping and pairwise homology
search: a sequencing read against a reference locus (global-ish, or
local when only part of the read belongs), a gene against its ortholog
in another species, a mutated sequence against wild type with the indel
placed explicitly instead of counted anonymously. Production tools
(BLAST, minimap2) add seeding and heuristics for scale, but the scoring
idea — matches rewarded, mismatches and gaps penalised, alignment
chosen to maximise the total — is exactly this one.

## Deliverables

- Python implementation
- Unit tests
- Gap-penalty sensitivity note (worksheet)
- 300-word biological interpretation
