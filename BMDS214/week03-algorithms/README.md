# Week 3 — Algorithm Analysis, Searching, and Sorting

## Goal
Develop the algorithmic reasoning expected for CS106B-level programming and connect it to genomic data.

## Topics

- Algorithm design
- Big-O time complexity
- Space complexity
- Linear search
- Binary search
- Insertion sort
- Selection sort
- Merge sort
- Quicksort
- Correctness and invariants
- Benchmarking

## Hands-on Project — Gene Search and Sorting Engine

Build a Python program that stores gene records and supports searching and sorting.

Implement from scratch:

```text
linear_search()
binary_search()
insertion_sort()
selection_sort()
merge_sort()
quicksort()
```

Do not use Python's built-in `sort()` for the algorithm implementations.

## Dataset

Start with a small manually created dataset of gene records, then generate synthetic datasets of:

```text
100
1,000
10,000
100,000
```

records.

Each record can contain:

```text
Gene ID
Gene name
Expression value
Sequence length
GC percentage
```

## Benchmark

Measure runtime for each algorithm at increasing input sizes. Record results in CSV and create a plot.

## Questions

1. Why does binary search require sorted data?
2. Why is merge sort O(n log n)?
3. What is the worst-case complexity of quicksort?
4. Which algorithms are in-place?
5. When would a hash table be preferable to binary search?
6. How does algorithm choice matter for large genomic datasets?

## Acceptance criteria

- Six algorithm implementations
- Unit tests
- Complexity table
- Runtime benchmark
- One visualization
- 300–500 word interpretation
