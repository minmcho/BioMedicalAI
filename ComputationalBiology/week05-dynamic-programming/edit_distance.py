"""Dynamic programming and edit distance for Computational Biology Week 5."""


def edit_distance_table(a: str, b: str) -> list[list[int]]:
    """Return the full Levenshtein DP table for a and b.

    table[i][j] = edit distance between a[:i] and b[:j], where an edit is
    an insertion, a deletion, or a substitution, each costing 1.
    """
    rows, cols = len(a) + 1, len(b) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        table[i][0] = i
    for j in range(cols):
        table[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            substitution = table[i - 1][j - 1] + (a[i - 1] != b[j - 1])
            table[i][j] = min(
                table[i - 1][j] + 1,   # deletion from a
                table[i][j - 1] + 1,   # insertion into a
                substitution,
            )
    return table


def edit_distance(a: str, b: str) -> int:
    """Return the Levenshtein edit distance between a and b."""
    return edit_distance_table(a, b)[len(a)][len(b)]


def edit_operations(a: str, b: str) -> list[str]:
    """Trace back one optimal edit script turning a into b.

    Operations are returned in order and use the forms
    'match X', 'substitute X->Y', 'delete X', 'insert Y'.
    """
    table = edit_distance_table(a, b)
    i, j = len(a), len(b)
    operations: list[str] = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and a[i - 1] == b[j - 1] and table[i][j] == table[i - 1][j - 1]:
            operations.append(f"match {a[i - 1]}")
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and table[i][j] == table[i - 1][j - 1] + 1:
            operations.append(f"substitute {a[i - 1]}->{b[j - 1]}")
            i, j = i - 1, j - 1
        elif i > 0 and table[i][j] == table[i - 1][j] + 1:
            operations.append(f"delete {a[i - 1]}")
            i -= 1
        else:
            operations.append(f"insert {b[j - 1]}")
            j -= 1
    return operations[::-1]


def hamming_distance(a: str, b: str) -> int:
    """Return the number of positions where equal-length strings differ."""
    if len(a) != len(b):
        raise ValueError("hamming distance requires equal lengths")
    return sum(x != y for x, y in zip(a, b))
