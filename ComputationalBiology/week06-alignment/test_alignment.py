import pytest

from alignment import (
    alignment_score,
    needleman_wunsch,
    percent_identity,
    smith_waterman,
)


def test_global_identical_sequences():
    aligned_a, aligned_b, score = needleman_wunsch("ACGT", "ACGT")
    assert (aligned_a, aligned_b) == ("ACGT", "ACGT")
    assert score == 8  # 4 matches x +2


def test_global_empty_sequence():
    aligned_a, aligned_b, score = needleman_wunsch("", "ACG")
    assert (aligned_a, aligned_b) == ("---", "ACG")
    assert score == -6  # 3 gaps x -2


def test_global_gap_placement():
    # one deletion explains ACGT -> AGT better than any mismatch path
    aligned_a, aligned_b, score = needleman_wunsch("ACGT", "AGT")
    assert (aligned_a, aligned_b) == ("ACGT", "A-GT")
    assert score == 4  # 3 matches x +2, 1 gap x -2


def test_global_score_matches_recomputed_score():
    aligned_a, aligned_b, score = needleman_wunsch("GATTACA", "GCATGC")
    assert alignment_score(aligned_a, aligned_b) == score
    # removing the gap characters recovers the inputs
    assert aligned_a.replace("-", "") == "GATTACA"
    assert aligned_b.replace("-", "") == "GCATGC"


def test_global_mismatch_beats_two_gaps():
    # mismatch (-1) is cheaper than opening two gaps (-4)
    _, _, score = needleman_wunsch("ACGT", "AGGT")
    assert score == 5  # 3 matches x +2, 1 mismatch x -1


def test_local_finds_embedded_match():
    aligned_a, aligned_b, score = smith_waterman("TTTACGTAAA", "ACGT")
    assert (aligned_a, aligned_b) == ("ACGT", "ACGT")
    assert score == 8


def test_local_no_positive_alignment():
    assert smith_waterman("AAAA", "TTTT") == ("", "", 0)


def test_local_score_matches_recomputed_score():
    aligned_a, aligned_b, score = smith_waterman("ACGTACGT", "CGT")
    assert alignment_score(aligned_a, aligned_b) == score
    assert score == 6  # CGT against CGT, 3 matches x +2


def test_percent_identity():
    assert percent_identity("ACGT", "A-GT") == 75.0
    assert percent_identity("ACGT", "ACGT") == 100.0
    assert percent_identity("", "") == 0.0


def test_percent_identity_requires_equal_length():
    with pytest.raises(ValueError):
        percent_identity("ACG", "ACGT")


def test_alignment_score_requires_equal_length():
    with pytest.raises(ValueError):
        alignment_score("ACG", "ACGT")
