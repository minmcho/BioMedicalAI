"""Basic DNA sequence utilities for BMDS 214 preparation."""

DNA_BASES = frozenset("ACGT")
CODON_TABLE = {
    "TTT": "F", "TTC": "F", "TTA": "L", "TTG": "L",
    "TCT": "S", "TCC": "S", "TCA": "S", "TCG": "S",
    "TAT": "Y", "TAC": "Y", "TAA": "*", "TAG": "*",
    "TGT": "C", "TGC": "C", "TGA": "*", "TGG": "W",
    "CTT": "L", "CTC": "L", "CTA": "L", "CTG": "L",
    "CCT": "P", "CCC": "P", "CCA": "P", "CCG": "P",
    "CAT": "H", "CAC": "H", "CAA": "Q", "CAG": "Q",
    "CGT": "R", "CGC": "R", "CGA": "R", "CGG": "R",
    "ATT": "I", "ATC": "I", "ATA": "I", "ATG": "M",
    "ACT": "T", "ACC": "T", "ACA": "T", "ACG": "T",
    "AAT": "N", "AAC": "N", "AAA": "K", "AAG": "K",
    "AGT": "S", "AGC": "S", "AGA": "R", "AGG": "R",
    "GTT": "V", "GTC": "V", "GTA": "V", "GTG": "V",
    "GCT": "A", "GCC": "A", "GCA": "A", "GCG": "A",
    "GAT": "D", "GAC": "D", "GAA": "E", "GAG": "E",
    "GGT": "G", "GGC": "G", "GGA": "G", "GGG": "G",
}


def validate_sequence(sequence: str) -> str:
    """Return an uppercase DNA sequence or raise ValueError."""
    if not isinstance(sequence, str):
        raise TypeError("sequence must be a string")
    sequence = sequence.strip().upper()
    if not sequence:
        raise ValueError("sequence cannot be empty")
    invalid = set(sequence) - DNA_BASES
    if invalid:
        raise ValueError(f"invalid DNA bases: {sorted(invalid)}")
    return sequence


def nucleotide_count(sequence: str) -> dict[str, int]:
    """Count A, C, G, and T bases."""
    sequence = validate_sequence(sequence)
    return {base: sequence.count(base) for base in "ACGT"}


def gc_content(sequence: str) -> float:
    """Return GC percentage in the sequence."""
    sequence = validate_sequence(sequence)
    return 100.0 * (sequence.count("G") + sequence.count("C")) / len(sequence)


def reverse_complement(sequence: str) -> str:
    """Return the reverse complement of a DNA sequence."""
    sequence = validate_sequence(sequence)
    complement = str.maketrans("ACGT", "TGCA")
    return sequence.translate(complement)[::-1]


def transcribe(sequence: str) -> str:
    """Transcribe DNA into RNA by replacing T with U."""
    return validate_sequence(sequence).replace("T", "U")


def translate(sequence: str) -> str:
    """Translate a DNA sequence into amino-acid symbols from the first base.

    Incomplete trailing codons are ignored. Stop codons are represented by '*'.
    """
    sequence = validate_sequence(sequence)
    amino_acids = []
    for index in range(0, len(sequence) - 2, 3):
        amino_acids.append(CODON_TABLE[sequence[index:index + 3]])
    return "".join(amino_acids)
