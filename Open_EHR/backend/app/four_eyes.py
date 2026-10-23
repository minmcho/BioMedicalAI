"""Shared four-eyes service — C-AI (AI-G3) / Wave C.

This is the shared approver≠author service for governed transitions:
the person who authored a governed artefact may not also be the person
who approves, publishes or enables it — a second authorised person
must perform that step. It is intentionally artefact-agnostic: Wave C
workstream C-1 generalises it to controlled documents, and C-AI already
rides it for agent / ai-provider publish and for T2/T3 agent enable.

Refusals are audited (action ``governance.four_eyes.refused``) and
raised as 403. UI hiding is not enforcement; this service is the
server-side enforcement point every governed transition calls.
"""
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .models import StudioConfig

# Kinds whose publish transition is four-eyes gated, and — for kinds
# gated only at some risk tiers — the tiers that trigger the gate.
# Semantics: publish four-eyes applies to ``ai-provider`` at EVERY
# tier (provider definitions carry no tier; the tuple lists all tiers
# for symmetry), and to ``agent`` only when the agent's payload
# riskTier is T2 or T3 (T1 informational agents may be published by
# their author; enablement still carries its own gates).
TIER_GATED_KINDS = {"agent": ("T2", "T3"), "ai-provider": ("T1", "T2", "T3")}

VALID_TIERS = ("T1", "T2", "T3")


def config_author(session: Session, kind: str, key: str,
                  version: int) -> str | None:
    """The StudioConfig.created_by for that exact version, or None
    when no such version exists."""
    row = session.scalars(select(StudioConfig).where(
        StudioConfig.kind == kind, StudioConfig.key == key,
        StudioConfig.version == version)).first()
    return row.created_by if row is not None else None


def agent_tier_of(payload: dict | None) -> str:
    """The risk tier of an agent payload: its ``riskTier`` when it is
    one of T1/T2/T3, else "T2" — an unclassified agent is governed as
    assistive, never as informational (framework §3: T1 must be
    positively classified)."""
    if isinstance(payload, dict):
        tier = payload.get("riskTier")
        if tier in VALID_TIERS:
            return tier
    return "T2"


def enforce(session: Session, *, actor: str, authors, action: str,
            entity: str, detail: str) -> None:
    """Refuse (audit + 403) when ``actor`` is one of ``authors``.

    ``authors`` is an iterable of usernames; None and "system" are
    ignored — system-seeded artefacts have no human builder, so a
    human publishing/enabling a seeded artefact is not its author.
    On refusal the audit row is committed before raising, so the
    refusal itself is durable evidence."""
    real_authors = sorted({a for a in (authors or [])
                           if a and a != "system"})
    if actor not in real_authors:
        return
    append_audit(session, actor=actor,
                 action="governance.four_eyes.refused", entity=entity,
                 payload={"attempted_action": action,
                          "authors": real_authors, "detail": detail})
    session.commit()
    raise HTTPException(
        status_code=403,
        detail=(f"Four-eyes rule: {detail} — the author of a governed "
                "artefact may not also approve/publish/enable it; a "
                "second authorised person must perform this step"))
