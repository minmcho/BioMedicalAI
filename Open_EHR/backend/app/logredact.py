"""PHI redaction at the logging boundary — Phase 17, BD-53 (CMP-32).

Logs are where PHI leaks quietly: an exception message carrying a
patient name, a debug line with a request payload, an access log
with a query string. This module is the boundary control for the
backend, with mirror implementations for the ADK runtime
(backend/adk/app/logredact.py), the Go services
(services/go/internal/redact) and the Rust services (stderr passes
through the crate's own de-identification engine) — one rule spec,
four runtimes.

RULES AS CONFIG — AND WHY THERE IS ALSO A BUILT-IN COPY: the
governed surface is published Studio configuration
(kind=integration, key=log-redaction): a site can disable a built-in
rule by name or add custom regex rules through Studio governance,
and the published payload names the spec version it extends. But a
logging filter must never run rule-less: log lines are emitted at
import/boot, before any database or config exists, and the sibling
services cannot read Studio config at all. So the versioned built-in
rule set below (SPEC_VERSION) is the floor — always compiled in,
mirrored byte-for-byte in the ADK copy and semantically in Go/Rust —
and published config MERGES OVER it (disable-by-name + custom
additions) once a session refreshes it. Config can widen or narrow
the net; it can never leave the boundary unguarded by accident,
because removing the config restores the built-ins, not silence.

WHAT THE RULES CATCH (synthetic-PHI-shaped patterns): SSNs, emails,
US phone numbers, card-number sequences, bearer tokens, and
LABELLED values (patient name / DOB / MRN / address after their
label) — the shapes PHI actually takes in log lines. Bare clinical
numbers (BP 120/80, a dose, a lab value) deliberately do NOT match:
a redactor that eats clinical telemetry gets switched off, and an
off redactor protects nothing.

THE BOUNDARY: RedactingFilter is attached to HANDLERS (the true
emission boundary — a logger-level filter only sees records that
originate at that logger, a handler filter sees everything the
handler emits, propagated or not). install_log_redaction() runs at
import (app.main imports this module) over the root + uvicorn +
fastapi loggers and their handlers, and the refresh endpoint
re-installs for handlers created later by foreign logging configs.
The scan endpoint + scan_text() are the proof tooling: the same
patterns, run over captured log output, must find ZERO survivors
(the Phase 17 log-scan test drives a synthetic-day workload and
asserts exactly that, redaction markers present).
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ConfigStatus, list_config_versions,
                          published_config)
from .models import StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/security/log-redaction", tags=["security-logs"])

CONFIG_KIND = "integration"
CONFIG_KEY = "log-redaction"
SPEC_VERSION = "log-redaction-v1"
REDACTION = "[REDACTED]"

# (name, pattern, replacement) — replacement may reference groups.
# ORDER MATTERS: tokens/cards before phones (a card tail can look
# like a phone fragment), labelled rules before bare patterns so the
# label context is consumed first.
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


def _compile(rules) -> list[tuple[str, re.Pattern, str]]:
    compiled = []
    for name, pattern, replacement in rules:
        try:
            compiled.append((name, re.compile(pattern), replacement))
        except re.error:
            continue  # a bad custom pattern never breaks logging
    return compiled


_BUILTIN_COMPILED = _compile(BUILTIN_RULES)
_active: list[tuple[str, re.Pattern, str]] = list(_BUILTIN_COMPILED)
_active_source = "built-in rules (spec log-redaction-v1)"


def active_rules() -> list[tuple[str, re.Pattern, str]]:
    return list(_active)


def redact_text(text: str, rules=None) -> str:
    """Redact one string with the active (or given) rule set."""
    if not isinstance(text, str) or not text:
        return text
    out = text
    for _name, pattern, replacement in (rules or _active):
        out = pattern.sub(replacement, out)
    return out


def scan_text(text: str, rules=None) -> dict:
    """Count UNREDACTED PHI-pattern survivors in `text`. The proof
    metric: after the boundary, every count must be zero. Redaction
    markers ([REDACTED-…]) are counted separately so a scan can
    distinguish 'redacted' from 'never there' — and a match that IS
    a marker (a labelled rule matching its own 'Label: [REDACTED-X]'
    output) is not a survivor."""
    counts: dict[str, int] = {}
    total = 0
    for name, pattern, _repl in (rules or _BUILTIN_COMPILED):
        hits = sum(1 for m in pattern.finditer(text or "")
                   if "[REDACTED" not in m.group(0))
        if hits:
            counts[name] = hits
            total += hits
    markers = len(re.findall(r"\[REDACTED(?:-[A-Z]+)?\]", text or ""))
    return {"patterns": counts, "total": total, "clean": total == 0,
            "redaction_markers": markers}


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the fully-formatted message AND
    the rendered exception. Formatting first (getMessage) is
    deliberate: PHI hides in args at least as often as in the format
    string. The traceback matters just as much: an exception whose
    message echoes a payload would otherwise print verbatim under
    the redacted log line (Formatter renders exc_info separately),
    so the rendered traceback is redacted into record.exc_text,
    which Formatter prefers when set."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True  # a broken record must not break logging
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


def _install_on(logger: logging.Logger) -> None:
    if _FILTER not in logger.filters:
        logger.addFilter(_FILTER)
    for handler in logger.handlers:
        if _FILTER not in handler.filters:
            handler.addFilter(_FILTER)


def install_log_redaction() -> list[str]:
    """Attach the redacting filter to the root logger, the uvicorn /
    fastapi loggers and every handler they currently hold. Returns
    the logger names covered. Idempotent."""
    covered = []
    for name in ("", "uvicorn", "uvicorn.access", "uvicorn.error",
                 "fastapi"):
        logger = logging.getLogger(name)
        _install_on(logger)
        covered.append(name or "root")
    return covered


def refresh_rules(session: Session) -> dict:
    """Merge the published Studio config over the built-in rules:
    `disabledRules` (built-in names to drop) + `customRules`
    ({name, pattern, replacement}) appended after the built-ins."""
    global _active, _active_source
    cfg = published_config(session, CONFIG_KIND, CONFIG_KEY)
    if cfg is None:
        _active = list(_BUILTIN_COMPILED)
        _active_source = "built-in rules (spec log-redaction-v1)"
        return {"source": _active_source,
                "rules": [n for n, _p, _r in _active]}
    payload = cfg.payload or {}
    disabled = set(payload.get("disabledRules") or [])
    merged = [r for r in _BUILTIN_COMPILED if r[0] not in disabled]
    custom = []
    for entry in payload.get("customRules") or []:
        if isinstance(entry, dict) and isinstance(
                entry.get("name"), str) and isinstance(
                entry.get("pattern"), str):
            custom.append((entry["name"], entry["pattern"],
                           str(entry.get("replacement")
                               or "[REDACTED]")))
    merged.extend(_compile(custom))
    _active = merged
    _active_source = f"{CONFIG_KIND}/{CONFIG_KEY}@v{cfg.version}"
    return {"source": _active_source,
            "rules": [n for n, _p, _r in _active],
            "disabled": sorted(disabled),
            "custom": [c[0] for c in custom]}


def default_logredaction_payload() -> dict:
    return {
        "specVersion": SPEC_VERSION,
        "disabledRules": [],
        "customRules": [],
        "notes": ("PHI log-redaction rules (BD-53/CMP-32). The "
                  "built-in rule set (spec log-redaction-v1: ssn, "
                  "email, phone, card, bearer-token, labelled name/"
                  "dob/mrn/ssn/address) is compiled into every "
                  "service; this config merges OVER it — disable a "
                  "built-in by name or append custom regex rules. "
                  "Removing this config restores the built-ins, "
                  "never silence."),
        "builtinRules": [name for name, _p, _r in BUILTIN_RULES],
    }


def ensure_logredact_defaults(session: Session) -> None:
    if list_config_versions(session, CONFIG_KIND, CONFIG_KEY):
        return
    session.add(StudioConfig(
        kind=CONFIG_KIND, key=CONFIG_KEY, version=1,
        status=ConfigStatus.published,
        payload=default_logredaction_payload(),
        created_by="system", updated_by="system"))
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{CONFIG_KIND}/{CONFIG_KEY}@v1",
                 payload={"kind": CONFIG_KIND, "key": CONFIG_KEY,
                          "version": 1, "seeded_default": True})
    session.commit()


def logredaction_status(session: Session) -> dict:
    state = refresh_rules(session)
    covered = install_log_redaction()
    return {"spec_version": SPEC_VERSION, **state,
            "builtin_rules": [n for n, _p, _r in BUILTIN_RULES],
            "filter_installed_on": covered,
            "config": f"{CONFIG_KIND}/{CONFIG_KEY}"}


# The boundary goes up at import: app.main imports this module in
# its router list, so every backend process is guarded from boot —
# including log lines emitted before the first request is served.
try:
    install_log_redaction()
except Exception:  # logging setup must never take the app down
    pass


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/status")
def status(session: Session = Depends(get_db),
           user: User = Depends(require_capability("security.logs"))):
    ensure_logredact_defaults(session)
    return logredaction_status(session)


@router.post("/refresh")
def refresh(session: Session = Depends(get_db),
            user: User = Depends(require_capability("security.logs"))):
    """Re-merge rules from the published config and re-install the
    filter (covers handlers created after boot)."""
    ensure_logredact_defaults(session)
    return logredaction_status(session)


@router.post("/scan")
async def scan(request: Request, session: Session = Depends(get_db),
               user: User = Depends(require_capability("security.logs"))):
    """Scan caller-supplied log text for unredacted PHI patterns —
    the surveyor's tool (and the test-suite's). Body: {"text": ...}.
    Returns per-pattern survivor counts; clean == zero survivors."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    text = (body or {}).get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=422,
                            detail="Scan requires a string 'text'")
    refresh_rules(session)
    return {"spec_version": SPEC_VERSION, **scan_text(text)}


@router.post("/redact")
async def redact(request: Request, session: Session = Depends(get_db),
                 user: User = Depends(require_capability("security.logs"))):
    """Redact caller-supplied text with the active rule set (the same
    function the logging filter runs). Body: {"text": ...}."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422,
                            detail="Request body must be valid JSON")
    text = (body or {}).get("text")
    if not isinstance(text, str):
        raise HTTPException(status_code=422,
                            detail="Redact requires a string 'text'")
    refresh_rules(session)
    return {"redacted": redact_text(text), "spec_version": SPEC_VERSION}
