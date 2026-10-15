"""PHI guardrail: scan text/records for identifier patterns before ingest.

The deployment's data rule is synthetic/public data only — but enterprise
deployments do not rely on intentions, they rely on gates. This scanner
blocks ingestion when common identifier patterns appear: SSN-like numbers,
MRNs, US phone numbers, email addresses, and dates of birth fields.
It is a safety net, not a de-identification certification.
"""
import re

PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b"),
    "mrn": re.compile(r"\bMRN[:#]?\s*\d{6,}\b", re.IGNORECASE),
}

BLOCKED_KEYS = {"ssn", "social_security", "dob", "date_of_birth",
                "phone", "email", "mrn", "medical_record_number"}


def scan_text(text):
    """Return the sorted names of identifier patterns found in text."""
    return sorted(name for name, pat in PATTERNS.items() if pat.search(text))


def scan_record(record):
    """Find identifiers in a record: blocked key names + pattern hits in values."""
    findings = set()
    for key, value in record.items():
        if key.lower() in BLOCKED_KEYS:
            findings.add(f"key:{key.lower()}")
        if isinstance(value, str):
            findings.update(scan_text(value))
    return sorted(findings)


def assert_clean(record):
    findings = scan_record(record)
    if findings:
        raise ValueError(f"identifier patterns blocked at ingest: {findings}")
