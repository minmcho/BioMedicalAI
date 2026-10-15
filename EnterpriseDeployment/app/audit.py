"""Append-only audit log with a hash chain.

Every mutating action in the deployment writes one entry. Each entry
hashes the previous entry's hash plus its own content, so silent edits
or deletions anywhere in the history break the chain and are detectable.
This is the property auditors actually ask for: tamper evidence.
"""
import hashlib
import json


class AuditLog:
    def __init__(self):
        self._entries = []

    def append(self, actor, action, detail):
        prev_hash = self._entries[-1]["hash"] if self._entries else "GENESIS"
        payload = {
            "seq": len(self._entries) + 1,
            "actor": actor,
            "action": action,
            "detail": detail,
            "prev_hash": prev_hash,
        }
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        entry = dict(payload, hash=digest)
        self._entries.append(entry)
        return entry

    def entries(self):
        return list(self._entries)

    def verify_chain(self):
        """Recompute every hash; True only if the full history is intact."""
        prev_hash = "GENESIS"
        for i, entry in enumerate(self._entries):
            payload = {
                "seq": i + 1,
                "actor": entry["actor"],
                "action": entry["action"],
                "detail": entry["detail"],
                "prev_hash": prev_hash,
            }
            digest = hashlib.sha256(
                json.dumps(payload, sort_keys=True).encode()
            ).hexdigest()
            if digest != entry["hash"] or entry["seq"] != i + 1:
                return False
            prev_hash = entry["hash"]
        return True
