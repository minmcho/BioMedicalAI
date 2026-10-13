# Week 5 — Dynamic Programming & Edit Distance

## Goal
Learn dynamic programming by solving a real sequence problem: how many
single-character edits (insert, delete, substitute) turn one sequence
into another — and which edits they are.

## Project
Build an edit-distance engine from scratch.

Implement:

```python
edit_distance_table(a, b)
edit_distance(a, b)
edit_operations(a, b)
hamming_distance(a, b)
```

## Experiments

- Verify your implementation against the classic kitten -> sitting example (distance 3).
- Measure runtime as sequence length doubles: 100, 200, 400, 800, 1600 bp.
  The table has (n+1) x (m+1) cells — confirm the O(n*m) growth empirically.
- Compare edit distance and hamming distance on equal-length reads with
  and without indels; note where hamming becomes meaningless.

## Biology connection

Edit distance is the scoring core underneath sequence alignment (Week 6):
a sequencing read vs a reference, a mutated gene vs wild type, or two
homologous proteins. Counting and locating the differences is how
variants are first described.

## Deliverables

- Python implementation
- Unit tests
- Runtime benchmark
- Complexity analysis
- 300-word biological interpretation
