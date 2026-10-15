import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AuditLog

GENESIS_HASH = "0" * 64


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def append_audit(session: Session, actor: str, action: str, entity: str, payload: dict) -> AuditLog:
    last = session.scalars(select(AuditLog).order_by(AuditLog.id.desc())).first()
    prev_hash = last.entry_hash if last else GENESIS_HASH
    material = prev_hash + actor + action + entity + _canonical(payload)
    entry_hash = hashlib.sha256(material.encode("utf-8")).hexdigest()
    row = AuditLog(actor=actor, action=action, entity=entity, payload=payload,
                   prev_hash=prev_hash, entry_hash=entry_hash)
    session.add(row)
    session.flush()
    return row


def verify_chain(session: Session) -> bool:
    rows = session.scalars(select(AuditLog).order_by(AuditLog.id.asc())).all()
    prev = GENESIS_HASH
    for row in rows:
        material = prev + row.actor + row.action + row.entity + _canonical(row.payload)
        if row.prev_hash != prev:
            return False
        if hashlib.sha256(material.encode("utf-8")).hexdigest() != row.entry_hash:
            return False
        prev = row.entry_hash
    return True
