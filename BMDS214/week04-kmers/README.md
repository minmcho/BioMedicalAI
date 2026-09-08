# Week 4 — k-mer Analysis

## Goal
Learn how short DNA substrings can be represented, counted, indexed, and used in computational genomics.

## Project
Build a k-mer analysis engine from scratch.

Implement:

```python
generate_kmers(sequence, k)
count_kmers(sequence, k)
most_frequent_kmers(sequence, k)
find_kmer_positions(sequence, kmer)
```

## Experiments

Benchmark k = 3, 5, 7, and 9 on sequences from 100 bp through 1,000,000 bp.

Compare a simple dictionary implementation with a naive repeated-search approach.

## Biology connection

k-mers are fundamental representations used in sequence analysis, genome assembly, read classification, indexing, and error correction.

## Deliverables

- Python implementation
- Unit tests
- Runtime benchmark
- Complexity analysis
- One notebook with results
- 300-word biological interpretation
