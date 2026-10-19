"""Sequence alignment for Computational Biology Week 6.

Global alignment (Needleman-Wunsch) and local alignment
(Smith-Waterman) with linear gap penalties. Scoring defaults are
match=+2, mismatch=-1, gap=-2; every function takes the scheme as
arguments so the effect of the scoring choice is itself testable.
"""

GAP_CHAR = "-"


def _pair_score(x: str, y: str, match: int, mismatch: int) -> int:
    return match if x == y else mismatch


def needleman_wunsch_table(
    a: str, b: str, match: int = 2, mismatch: int = -1, gap: int = -2
) -> list[list[int]]:
    """Return the full Needleman-Wunsch score table for a and b.

    table[i][j] = best global-alignment score for a[:i] against b[:j].
    The first row and column accumulate gap penalties: aligning a
    prefix against nothing costs one gap per character.
    """
    rows, cols = len(a) + 1, len(b) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(1, rows):
        table[i][0] = table[i - 1][0] + gap
    for j in range(1, cols):
        table[0][j] = table[0][j - 1] + gap
    for i in range(1, rows):
        for j in range(1, cols):
            table[i][j] = max(
                table[i - 1][j - 1] + _pair_score(a[i - 1], b[j - 1], match, mismatch),
                table[i - 1][j] + gap,   # gap in b
                table[i][j - 1] + gap,   # gap in a
            )
    return table


def needleman_wunsch(
    a: str, b: str, match: int = 2, mismatch: int = -1, gap: int = -2
) -> tuple[str, str, int]:
    """Return one optimal global alignment as (aligned_a, aligned_b, score).

    Traceback is deterministic: on ties it prefers the diagonal move,
    then a gap in b, then a gap in a.
    """
    table = needleman_wunsch_table(a, b, match, mismatch, gap)
    i, j = len(a), len(b)
    aligned_a: list[str] = []
    aligned_b: list[str] = []
    while i > 0 or j > 0:
        if (
            i > 0
            and j > 0
            and table[i][j]
            == table[i - 1][j - 1] + _pair_score(a[i - 1], b[j - 1], match, mismatch)
        ):
            aligned_a.append(a[i - 1])
            aligned_b.append(b[j - 1])
            i, j = i - 1, j - 1
        elif i > 0 and table[i][j] == table[i - 1][j] + gap:
            aligned_a.append(a[i - 1])
            aligned_b.append(GAP_CHAR)
            i -= 1
        else:
            aligned_a.append(GAP_CHAR)
            aligned_b.append(b[j - 1])
            j -= 1
    return "".join(reversed(aligned_a)), "".join(reversed(aligned_b)), table[len(a)][len(b)]


def smith_waterman_table(
    a: str, b: str, match: int = 2, mismatch: int = -1, gap: int = -2
) -> list[list[int]]:
    """Return the full Smith-Waterman score table for a and b.

    Identical recurrence to Needleman-Wunsch except no cell may go
    below zero: a local alignment is allowed to start anywhere, so a
    prefix that only hurts the score is simply not part of it.
    """
    rows, cols = len(a) + 1, len(b) + 1
    table = [[0] * cols for _ in range(rows)]
    for i in range(1, rows):
        for j in range(1, cols):
            table[i][j] = max(
                0,
                table[i - 1][j - 1] + _pair_score(a[i - 1], b[j - 1], match, mismatch),
                table[i - 1][j] + gap,   # gap in b
                table[i][j - 1] + gap,   # gap in a
            )
    return table


def smith_waterman(
    a: str, b: str, match: int = 2, mismatch: int = -1, gap: int = -2
) -> tuple[str, str, int]:
    """Return one optimal local alignment as (aligned_a, aligned_b, score).

    Traceback starts at the highest-scoring cell and stops at a zero
    cell. If nothing aligns positively, returns ("", "", 0).
    """
    table = smith_waterman_table(a, b, match, mismatch, gap)
    best, best_i, best_j = 0, 0, 0
    for i, row in enumerate(table):
        for j, value in enumerate(row):
            if value > best:
                best, best_i, best_j = value, i, j
    i, j = best_i, best_j
    aligned_a: list[str] = []
    aligned_b: list[str] = []
    while i > 0 and j > 0 and table[i][j] > 0:
        if table[i][j] == table[i - 1][j - 1] + _pair_score(
            a[i - 1], b[j - 1], match, mismatch
        ):
            aligned_a.append(a[i - 1])
            aligned_b.append(b[j - 1])
            i, j = i - 1, j - 1
        elif table[i][j] == table[i - 1][j] + gap:
            aligned_a.append(a[i - 1])
            aligned_b.append(GAP_CHAR)
            i -= 1
        else:
            aligned_a.append(GAP_CHAR)
            aligned_b.append(b[j - 1])
            j -= 1
    return "".join(reversed(aligned_a)), "".join(reversed(aligned_b)), best


def alignment_score(
    aligned_a: str, aligned_b: str, match: int = 2, mismatch: int = -1, gap: int = -2
) -> int:
    """Recompute the score of a finished alignment, column by column."""
    if len(aligned_a) != len(aligned_b):
        raise ValueError("aligned sequences must have equal length")
    total = 0
    for x, y in zip(aligned_a, aligned_b):
        if x == GAP_CHAR or y == GAP_CHAR:
            total += gap
        else:
            total += _pair_score(x, y, match, mismatch)
    return total


def percent_identity(aligned_a: str, aligned_b: str) -> float:
    """Return the percentage of columns where both characters match.

    Gap columns count as non-identical (they are in the denominator),
    which is the convention used when reporting read alignments.
    """
    if len(aligned_a) != len(aligned_b):
        raise ValueError("aligned sequences must have equal length")
    if not aligned_a:
        return 0.0
    matches = sum(
        1
        for x, y in zip(aligned_a, aligned_b)
        if x == y and x != GAP_CHAR
    )
    return 100.0 * matches / len(aligned_a)
