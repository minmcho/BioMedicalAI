"""k-mer algorithms for BMDS 214 Week 4."""


def generate_kmers(sequence: str, k: int) -> list[str]:
    """Return every length-k substring in order."""
    if k <= 0:
        raise ValueError("k must be positive")
    if k > len(sequence):
        return []
    return [sequence[i:i + k] for i in range(len(sequence) - k + 1)]


def count_kmers(sequence: str, k: int) -> dict[str, int]:
    """Count every length-k substring in one pass."""
    counts: dict[str, int] = {}
    for i in range(len(sequence) - k + 1):
        kmer = sequence[i:i + k]
        counts[kmer] = counts.get(kmer, 0) + 1
    return counts


def most_frequent_kmers(sequence: str, k: int) -> list[str]:
    """Return all k-mers tied for the highest frequency."""
    counts = count_kmers(sequence, k)
    if not counts:
        return []
    maximum = max(counts.values())
    return sorted(kmer for kmer, count in counts.items() if count == maximum)


def find_kmer_positions(sequence: str, kmer: str) -> list[int]:
    """Return zero-based positions where kmer starts, including overlaps."""
    if not kmer:
        raise ValueError("kmer cannot be empty")
    return [
        i for i in range(len(sequence) - len(kmer) + 1)
        if sequence[i:i + len(kmer)] == kmer
    ]
