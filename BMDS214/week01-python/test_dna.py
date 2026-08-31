"""Tests for the Week 1 DNA toolkit."""

import pytest

from dna import (
    gc_content,
    nucleotide_count,
    reverse_complement,
    transcribe,
    translate,
    validate_sequence,
)


def test_validate_uppercase_and_whitespace():
    assert validate_sequence(" atgc ") == "ATGC"


def test_validate_invalid_base():
    with pytest.raises(ValueError):
        validate_sequence("ATGX")


def test_validate_empty():
    with pytest.raises(ValueError):
        validate_sequence("")


def test_validate_wrong_type():
    with pytest.raises(TypeError):
        validate_sequence(123)


def test_nucleotide_count():
    assert nucleotide_count("ATGCGT") == {"A": 1, "C": 1, "G": 2, "T": 2}


def test_nucleotide_count_case_insensitive():
    assert nucleotide_count("atgc") == {"A": 1, "C": 1, "G": 1, "T": 1}


def test_gc_content():
    assert gc_content("ATGCGT") == pytest.approx(50.0)


def test_gc_content_all_gc():
    assert gc_content("GCGC") == pytest.approx(100.0)


def test_gc_content_no_gc():
    assert gc_content("ATAT") == pytest.approx(0.0)


def test_reverse_complement():
    assert reverse_complement("ATGC") == "GCAT"


def test_reverse_complement_palindrome():
    assert reverse_complement("ATAT") == "ATAT"


def test_transcribe():
    assert transcribe("ATGC") == "AUGC"


def test_transcribe_lowercase():
    assert transcribe("atgc") == "AUGC"


def test_translate():
    assert translate("ATGGCC") == "MA"


def test_translate_stop_codon():
    assert translate("ATGTAA") == "M*"


def test_translate_ignores_incomplete_trailing_codon():
    assert translate("ATGGCCG") == "MA"
