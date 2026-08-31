# Week 2 — Data Structures for Computational Biology

## Goal
Build and use fundamental data structures while applying them to biological data.

## Topics

- Lists and dynamic arrays
- Stacks
- Queues
- Linked lists
- Hash tables
- Trees
- Graphs and adjacency lists
- BFS and DFS
- Time and space complexity

## Hands-on Project — Gene Sequence Database

Build a small in-memory gene database supporting:

```text
add_gene()
remove_gene()
find_gene()
search_by_prefix()
calculate_statistics()
```

Each gene record should contain:

```text
Gene ID
Gene name
Organism
DNA sequence
Sequence length
GC percentage
```

## Implementation requirement

Implement a stack, queue, linked list, and graph yourself. Python's built-in list/dict may be used for internal storage, but the public data-structure behavior should be implemented by you.

## Exercises

1. Implement `Stack` with push, pop, peek, and is_empty.
2. Implement `Queue` with enqueue, dequeue, peek, and is_empty.
3. Implement `LinkedList` with append, prepend, search, and delete.
4. Implement a `Graph` using an adjacency list.
5. Implement BFS.
6. Implement DFS.
7. Store gene sequences in the database and search them.
8. Explain the time complexity of every public method.
9. Write at least 20 unit tests.
10. Compare list-based search with hash-based lookup.

## BMDS 214 connection

Biological datasets naturally form structures: genes can be indexed by identifiers, sequence relationships can be represented as graphs, and molecular networks can be traversed with graph algorithms.
