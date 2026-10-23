"""Typed field validation shared by the front-door write surfaces
(accounts, registration, staff edit) — Front Door pass.

The quality bar for every form in this system: values are validated
BY TYPE on the server exactly as the client validates them — a date
must be a real calendar date (2026-02-30 is refused, not stored), an
email must look like an email, a phone must be dialable characters
with enough digits, and every failure is a 422 whose message names
the field and what is wrong with it, so the UI can show the user an
inline error they can act on. Nothing fails silently.
"""
import re
from datetime import date

from fastapi import HTTPException

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^[+()\-.\s\d]+$")


def require_str(body: dict, field: str) -> str:
    value = body.get(field)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' is required and must be a non-empty string")
    return value.strip()


def optional_str(body: dict, field: str) -> str | None:
    value = body.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a string")
    return value.strip() or None


def check_email(value: str | None, field: str = "email") -> str | None:
    """Format-check an email when one is supplied (None/'' passes as
    'not supplied' and comes back as None)."""
    if value is None or value == "":
        return None
    if len(value) > 200 or not _EMAIL_RE.match(value):
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be a valid email address "
                   "(e.g. name@example.org)")
    return value


def check_phone(value: str | None, field: str = "phone") -> str | None:
    """Format-check a phone when one is supplied: dialable characters
    only, 7–15 digits."""
    if value is None or value == "":
        return None
    digits = sum(1 for ch in value if ch.isdigit())
    if not _PHONE_RE.match(value) or not (7 <= digits <= 15):
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be a valid phone number "
                   "(7-15 digits; +, spaces, dashes and brackets allowed)")
    return value


def check_iso_date(value: str | None, field: str,
                   required: bool = False) -> str | None:
    """A date field must be a REAL calendar date in YYYY-MM-DD form:
    date.fromisoformat rejects impossibilities like 2026-02-30."""
    if value is None or value == "":
        if required:
            raise HTTPException(
                status_code=422,
                detail=f"'{field}' is required (a real date, YYYY-MM-DD)")
        return None
    if not isinstance(value, str):
        raise HTTPException(status_code=422,
                            detail=f"'{field}' must be a date string (YYYY-MM-DD)")
    try:
        parsed = date.fromisoformat(value.strip())
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f"'{field}' must be a real calendar date (YYYY-MM-DD); "
                   f"'{value}' is not one")
    return parsed.isoformat()
