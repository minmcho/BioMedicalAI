"""Domain models for biomedical sequence requests and results."""

from dataclasses import dataclass


@dataclass(frozen=True)
class SequenceAnalysis:
    """Immutable analysis result returned by the service layer."""

    sequence: str
    length: int
    gc_percentage: float
    reverse_complement: str

    @classmethod
    def from_sequence(cls, sequence: str, gc_percentage: float, reverse_complement: str):
        return cls(sequence, len(sequence), gc_percentage, reverse_complement)
