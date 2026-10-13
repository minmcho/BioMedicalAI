import pytest

from edit_distance import edit_distance, edit_operations, hamming_distance


def test_identical_sequences():
    assert edit_distance("ACGT", "ACGT") == 0


def test_empty_sequences():
    assert edit_distance("", "") == 0
    assert edit_distance("ACG", "") == 3
    assert edit_distance("", "ACG") == 3


def test_single_substitution():
    assert edit_distance("ACGT", "AGGT") == 1


def test_insertion_and_deletion():
    assert edit_distance("ACGT", "ACGTT") == 1
    assert edit_distance("ACGTT", "ACGT") == 1


def test_known_example():
    # classic textbook example
    assert edit_distance("kitten", "sitting") == 3


def test_operations_turn_a_into_b():
    ops = edit_operations("ACGT", "AGGT")
    assert ops == ["match A", "substitute C->G", "match G", "match T"]


def test_operations_with_insertion():
    ops = edit_operations("ACG", "ACGT")
    assert ops[-1] == "insert T"
    assert len([o for o in ops if o.startswith("match")]) == 3


def test_hamming_distance():
    assert hamming_distance("ACGT", "AGGT") == 1
    assert hamming_distance("AAAA", "AAAA") == 0


def test_hamming_requires_equal_length():
    with pytest.raises(ValueError):
        hamming_distance("ACG", "ACGT")
