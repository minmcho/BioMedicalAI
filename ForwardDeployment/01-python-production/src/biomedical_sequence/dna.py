"""Validated DNA operations suitable for use by an application service."""

from typing import Final

DNA_BASES: Final = frozenset("ACGT")


def validate_sequence(sequence: str) -> str:
    """Normalize and validate a DNA sequence."""
    if not isinstance(sequence, str):
        raise TypeError("sequence must be a string")
    normalized = sequence.strip().upper()
    if not normalized:
        raise ValueError("sequence cannot be empty")
    invalid = set(normalized) - DNA_BASES
    if invalid:
        raise ValueError(f"invalid DNA bases: {sorted(invalid)}")
    return normalized


def nucleotide_count(sequence: str) -> dict[str, int]:
    """Return counts for A, C, G, and T."""
    sequence = validate_sequence(sequence)
    counts = dict.fromkeys("ACGT", 0)
    for base in sequence:
        counts[base] += 1
    return counts


def gc_content(sequence: str) -> float:
    """Return GC content as a percentage."""
    sequence = validate_sequence(sequence)
    return 100.0 * sum(base in "GC" for base in sequence) / len(sequence)


def reverse_complement(sequence: str) -> str:
    """Return the reverse complement of a DNA sequence."""
    sequence = validate_sequence(sequence)
    complement = str.maketrans("ACGT", "TGCA")
    return sequence.translate(complement)[::-1]


def transcribe(sequence: str) -> str:
    """Transcribe DNA to RNA."""
    return validate_sequence(sequence).replace("T", "U")


def translate(sequence: str) -> str:
    """Translate complete codons using a standard codon table."""
    sequence = validate_sequence(sequence)
    codons = {
        "ATG": "M", "TAA": "*", "TAG": "*", "TGA": "*",
        "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
        "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    }
    return "".join(codons.get(sequence[i:i + 3], "X") for i in range(0, len(sequence) - 2, 3))
