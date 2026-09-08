import pytest

from kmers import count_kmers, find_kmer_positions, generate_kmers, most_frequent_kmers


def test_generate_kmers():
    assert generate_kmers("ACGTAC", 3) == ["ACG", "CGT", "GTA", "TAC"]


def test_generate_kmers_too_large():
    assert generate_kmers("ACG", 5) == []


def test_generate_kmers_invalid_k():
    with pytest.raises(ValueError):
        generate_kmers("ACG", 0)


def test_count_kmers():
    assert count_kmers("ACGTACGT", 3) == {"ACG": 2, "CGT": 2, "GTA": 1, "TAC": 1}


def test_most_frequent_kmers():
    assert most_frequent_kmers("AAAAAA", 2) == ["AA"]


def test_tied_most_frequent_kmers():
    assert most_frequent_kmers("ACGT", 1) == ["A", "C", "G", "T"]


def test_find_positions_including_overlaps():
    assert find_kmer_positions("AAAA", "AA") == [0, 1, 2]


def test_find_empty_kmer():
    with pytest.raises(ValueError):
        find_kmer_positions("ACGT", "")
