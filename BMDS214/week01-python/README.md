# Week 1 — Python & DNA Sequence Toolkit

## Objective

Build a small DNA sequence analysis toolkit while practicing Python functions, strings, dictionaries, validation, testing, and computational complexity.

## Learning outcomes

By the end of Week 1 you should be able to:

- write clean Python functions
- validate biological sequence input
- manipulate strings and dictionaries
- explain basic time complexity
- write unit tests
- translate DNA into RNA and protein

## Project

Implement the functions in `dna.py`:

1. `validate_sequence`
2. `nucleotide_count`
3. `gc_content`
4. `reverse_complement`
5. `transcribe`
6. `translate`

## Rules

For the first implementation, do not use BioPython or specialized sequence-analysis libraries. The goal is to demonstrate algorithmic understanding.

## Acceptance criteria

- At least 15 unit tests
- Clear docstrings and type hints
- Invalid DNA input produces a useful error
- Tests cover normal, empty, lowercase, and invalid inputs
- Include complexity analysis in the final report

## Exercises

### Exercise 1
Calculate GC content for `ATGCGTACG`.

### Exercise 2
Find the reverse complement of `ATGCCGTA`.

### Exercise 3
Transcribe `ATGCCGTA` to RNA.

### Exercise 4
Explain the time complexity of `reverse_complement`.

### Exercise 5
Write a test for an invalid DNA sequence containing `X`.
