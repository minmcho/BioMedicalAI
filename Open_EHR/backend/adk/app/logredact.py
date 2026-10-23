"""PHI redaction at the logging boundary — ADK runtime mirror.

Phase 17, BD-53 (CMP-32). This is the ADK service's copy of the
redaction rule spec whose source of truth is the backend's
app/logredact.py (spec version log-redaction-v1, governed surface:
Studio config integration/log-redaction). The ADK runtime is a
separate process that cannot import the backend package or read
Studio config, so the built-in rule set is mirrored here exactly —
same names, same patterns, same replacements. If the spec version
moves in the backend, this file moves with it (the Phase 17
log-scan test asserts the two copies agree).

The filter attaches to handlers (the emission boundary) and error
strings returned to callers pass through redact_text as well: an
upstream model error can echo prompt text, and prompts can carry
synthetic PHI in this system.
"""
from __future__ import annotations

import logging
import re

SPEC_VERSION = "log-redaction-v1"

BUILTIN_RULES: tuple[tuple[str, str, str], ...] = (
    ("bearer-token",
     r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{8,}",
     r"\1[REDACTED-TOKEN]"),
    ("ssn",
     r"\b\d{3}-\d{2}-\d{4}\b",
     "[REDACTED-SSN]"),
    ("email",
     r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
     "[REDACTED-EMAIL]"),
    ("card",
     r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b",
     "[REDACTED-CARD]"),
    ("phone",
     r"\b(?:\+?1[-. ]?)?\(?\d{3}\)?[-. ]\d{3}[-. ]\d{4}\b",
     "[REDACTED-PHONE]"),
    ("labelled-name",
     r"(?i)\b(patient name|full name)\s*[:=]\s*[A-Z][A-Za-z'’\-]+"
     r"(?: [A-Z][A-Za-z'’\-]+){0,2}",
     r"\1: [REDACTED-NAME]"),
    ("labelled-dob",
     r"(?i)\b(dob|date of birth|birth ?date)\s*[:=]?\s*"
     r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
     r"\1: [REDACTED-DOB]"),
    ("labelled-mrn",
     r"(?i)\b(mrn|medical record(?: number)?|chart(?: number)?)"
     r"\s*[:#=]?\s*[A-Za-z0-9][A-Za-z0-9\-]{2,}",
     r"\1: [REDACTED-MRN]"),
    ("labelled-ssn",
     r"(?i)\b(ssn|social security(?: number)?)\s*[:#=]?\s*"
     r"\d{3}[- ]?\d{2}[- ]?\d{4}",
     r"\1: [REDACTED-SSN]"),
    ("labelled-address",
     r"(?i)\b(address)\s*[:=]\s*[^\n;]+",
     r"\1: [REDACTED-ADDRESS]"),
)

_COMPILED = [(name, re.compile(pattern), replacement)
             for name, pattern, replacement in BUILTIN_RULES]


def redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    out = text
    for _name, pattern, replacement in _COMPILED:
        out = pattern.sub(replacement, out)
    return out


def scan_text(text: str) -> dict:
    counts: dict[str, int] = {}
    total = 0
    for name, pattern, _repl in _COMPILED:
        hits = sum(1 for m in pattern.finditer(text or "")
                   if "[REDACTED" not in m.group(0))
        if hits:
            counts[name] = hits
            total += hits
    return {"patterns": counts, "total": total, "clean": total == 0}


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        if record.exc_info:
            try:
                import traceback
                rendered = "".join(
                    traceback.format_exception(*record.exc_info))
                safe = redact_text(rendered)
                if safe != rendered:
                    record.exc_text = safe
            except Exception:
                pass
        if isinstance(record.stack_info, str) and record.stack_info:
            record.stack_info = redact_text(record.stack_info)
        return True


_FILTER = RedactingFilter()


def install_log_redaction() -> list[str]:
    covered = []
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error",
                 "opencare-adk"):
        logger = logging.getLogger(name)
        if _FILTER not in logger.filters:
            logger.addFilter(_FILTER)
        for handler in logger.handlers:
            if _FILTER not in handler.filters:
                handler.addFilter(_FILTER)
        covered.append(name or "root")
    return covered
