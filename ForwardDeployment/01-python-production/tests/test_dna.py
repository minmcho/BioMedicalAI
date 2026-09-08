import pytest

from biomedical_sequence import gc_content, nucleotide_count, reverse_complement, transcribe, translate, validate_sequence


def test_validation():
    assert validate_sequence(" atgc ") == "ATGC"
    with pytest.raises(ValueError):
        validate_sequence("ATGX")


def test_counts():
    assert nucleotide_count("ATGCGT") == {"A": 1, "C": 1, "G": 2, "T": 2}


def test_gc():
    assert gc_content("ATGC") == pytest.approx(50.0)


def test_reverse_complement():
    assert reverse_complement("ATGC") == "GCAT"


def test_transcription():
    assert transcribe("ATGC") == "AUGC"


def test_translation():
    assert translate("ATG") == "M"
    assert translate("ATGTAA") == "M*"
