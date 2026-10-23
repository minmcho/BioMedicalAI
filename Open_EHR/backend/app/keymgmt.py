"""Key management: KEK providers, rotation and DEK re-wrap — Phase 17,
BD-51 (CMP-30).

Phase 10's envelope encryption (app.crypto) resolved exactly ONE
master key (KEK) from the environment / a key file, and an envelope
could only be opened while that exact key stayed configured. That is
a key *storage* story, not a key *management* story: there was no
rotation, no re-wrap, no retirement. This module is the management
layer the Phase 10 SQA report promised ("the KEK is held by a KMS …
rotation = generate a new KEK, publish its kid, and re-wrap DEKs"):

PROVIDER INTERFACE — a KEK provider resolves key material by kid:

- `env` (EnvKeyProvider): TODAY'S behaviour, unchanged. The active
  KEK comes from OPENEHR_MASTER_KEY / OPENEHR_MASTER_KEY_FILE / the
  local dev key file — i.e. the deployment injects the unwrapped KEK
  from its own secret store. An optional previous KEK
  (OPENEHR_MASTER_KEY_PREVIOUS[_FILE]) stays resolvable so a
  deployment-side rotation can be followed by a re-wrap. Rotation
  itself is a deployment operation for this provider and the API
  says so (409 with the procedure) rather than pretending otherwise.

- `software-kms` (SoftwareKmsProvider): a LOCAL SOFTWARE KMS. This
  is a software key store — **NOT AN HSM**, and every surface that
  names it says so. The keyring lives OUTSIDE the repo tree
  (default ~/.opencare/software-kms-keyring.json, mode 0600;
  OPENEHR_KMS_KEYRING overrides). Each KEK in the keyring is itself
  wrapped (AES-256-GCM) under a master protection key held
  separately (OPENEHR_KMS_MASTER_KEY / _FILE, else a generated
  ~/.opencare/software-kms-master, mode 0600): stealing the keyring
  alone yields no usable KEK. It stands in for a cloud KMS / HSM in
  this environment; a production deployment implements this same
  interface against its real KMS and nothing else changes.

ROTATION LIFECYCLE (software-kms): rotate() mints a new active KEK
and demotes the old one to `retiring` (still resolvable — in-flight
data keeps reading); rewrap_all() re-wraps every stored envelope's
DEK from its old KEK onto the active KEK (the ciphertext and nonce
never change — re-wrap is a DEK operation, exactly what envelope
encryption is for); retire(kid) then DESTROYS the retired key's
wrapped material, after which that kid resolves to nothing and any
leftover envelope under it can no longer be unwrapped.

SCHEDULE — the rotation policy is published Studio configuration
(kind=integration, key=kms-rotation): provider (advisory — the env
var OPENEHR_KMS_PROVIDER is the deployment's binding choice),
rotationIntervalDays, autoRewrapOnRotate. Status reports the last
rotation (from the keyring / audit chain), the computed next-due
instant and whether rotation is overdue. Seeded published v1 lazily
on first use (the pack-manifest lazy-load pattern) so main.py needs
no seed call.

Every mutation is hash-chain audited (kms.rotate / kms.rewrap /
kms.retire). Key material is NEVER returned by any endpoint — kids,
statuses and timestamps only.
"""
import base64
import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import (ConfigStatus, list_config_versions,
                          published_config)
from .models import AuditLog, FhirResource, StudioConfig, User
from .security import get_db, require_capability

router = APIRouter(prefix="/security/kms", tags=["security-kms"])

CONFIG_KIND = "integration"
CONFIG_KEY = "kms-rotation"
DEFAULT_ROTATION_INTERVAL_DAYS = 90

SOFTWARE_KMS_LABEL = ("SOFTWARE-KMS — local software key store "
                      "(NOT AN HSM)")
ENV_LABEL = ("ENV/KEY-FILE — deployment-injected KEK "
             "(pre-Phase-17 behaviour)")

_DEFAULT_KEYRING_PATH = Path.home() / ".opencare" / "software-kms-keyring.json"
_DEFAULT_MASTER_PATH = Path.home() / ".opencare" / "software-kms-master"
_DEV_KEY_PATH = Path.home() / ".opencare" / "dev-master-key"


class KeyMgmtError(Exception):
    """Key-management failure (unknown/retired kid, bad keyring,
    tampered wrapped key). app.crypto translates this to CryptoError
    at the decrypt boundary."""


def _b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"))


def kek_id(key: bytes) -> str:
    return "k-" + hashlib.sha256(key).hexdigest()[:12]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Provider interface
# ---------------------------------------------------------------------------

class KeyProvider:
    """A KEK provider: resolves key material by kid.

    - active() -> (kid, key bytes) of the key new envelopes wrap under.
    - resolve(kid) -> key bytes, or None when the kid is unknown or
      RETIRED (retirement is precisely "resolve stops answering").
    - list_keys() -> metadata only (kid/status/timestamps), never
      material.
    """

    name = "base"
    label = ""

    def active(self) -> tuple[str, bytes]:  # pragma: no cover - interface
        raise NotImplementedError

    def resolve(self, kid: str) -> bytes | None:  # pragma: no cover
        raise NotImplementedError

    def list_keys(self) -> list[dict]:  # pragma: no cover
        raise NotImplementedError

    def rotate(self) -> dict:
        raise KeyMgmtError(
            f"Provider '{self.name}' does not rotate in-process")

    def retire(self, kid: str) -> dict:
        raise KeyMgmtError(
            f"Provider '{self.name}' does not retire in-process")


def _load_key_from_env_or_file(env_key: str, env_file: str,
                               fallback_path: Path | None) -> bytes:
    raw = os.environ.get(env_key)
    if raw:
        try:
            key = _b64d(raw.strip())
        except Exception as exc:
            raise KeyMgmtError(f"{env_key} is not valid base64") from exc
    else:
        path = os.environ.get(env_file)
        key_path = Path(path) if path else fallback_path
        if key_path is not None and key_path.exists():
            try:
                key = _b64d(key_path.read_text(encoding="utf-8").strip())
            except Exception as exc:
                raise KeyMgmtError(
                    f"Key file {key_path} is not valid base64") from exc
        elif path:
            raise KeyMgmtError(f"{env_file} {path} does not exist")
        elif key_path is not None:
            # Local development key: generated OUTSIDE the repo tree
            # (byte-for-byte the Phase 10 behaviour).
            key = AESGCM.generate_key(bit_length=256)
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text(_b64e(key), encoding="utf-8")
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
        else:
            raise KeyMgmtError(f"Neither {env_key} nor {env_file} is set")
    if len(key) != 32:
        raise KeyMgmtError("KEK must be exactly 32 bytes (AES-256)")
    return key


class EnvKeyProvider(KeyProvider):
    """The Phase 10 behaviour as a provider: one active deployment-
    injected KEK, plus an optional previous KEK kept resolvable for
    the re-wrap window after a deployment-side rotation."""

    name = "env"
    label = ENV_LABEL

    def active(self) -> tuple[str, bytes]:
        key = _load_key_from_env_or_file(
            "OPENEHR_MASTER_KEY", "OPENEHR_MASTER_KEY_FILE", _DEV_KEY_PATH)
        return kek_id(key), key

    def _previous(self) -> tuple[str, bytes] | None:
        if not (os.environ.get("OPENEHR_MASTER_KEY_PREVIOUS")
                or os.environ.get("OPENEHR_MASTER_KEY_PREVIOUS_FILE")):
            return None
        key = _load_key_from_env_or_file(
            "OPENEHR_MASTER_KEY_PREVIOUS",
            "OPENEHR_MASTER_KEY_PREVIOUS_FILE", None)
        return kek_id(key), key

    def resolve(self, kid: str) -> bytes | None:
        active_kid, key = self.active()
        if kid == active_kid:
            return key
        prev = self._previous()
        if prev is not None and kid == prev[0]:
            return prev[1]
        return None

    def list_keys(self) -> list[dict]:
        active_kid, _ = self.active()
        keys = [{"kid": active_kid, "status": "active",
                 "source": "environment / key file"}]
        prev = self._previous()
        if prev is not None:
            keys.append({"kid": prev[0], "status": "retiring",
                         "source": "previous-key environment / key file"})
        return keys

    def rotate(self) -> dict:
        raise KeyMgmtError(
            "The env/key-file provider rotates at deployment time: "
            "generate a new KEK, configure it as OPENEHR_MASTER_KEY"
            "[_FILE], move the old one to OPENEHR_MASTER_KEY_PREVIOUS"
            "[_FILE], restart, then call POST /security/kms/rewrap "
            "and remove the previous key once re-wrap completes")


class SoftwareKmsProvider(KeyProvider):
    """Local SOFTWARE-KMS (NOT AN HSM): a keyring file outside the
    repo whose KEKs are wrapped under a separately held master
    protection key. See the module docstring."""

    name = "software-kms"
    label = SOFTWARE_KMS_LABEL

    def keyring_path(self) -> Path:
        raw = os.environ.get("OPENEHR_KMS_KEYRING")
        return Path(raw) if raw else _DEFAULT_KEYRING_PATH

    def _master_key(self) -> bytes:
        return _load_key_from_env_or_file(
            "OPENEHR_KMS_MASTER_KEY", "OPENEHR_KMS_MASTER_KEY_FILE",
            _DEFAULT_MASTER_PATH)

    def _load_keyring(self) -> dict:
        path = self.keyring_path()
        if not path.exists():
            return {"format": 1, "provider": self.name,
                    "label": self.label, "keys": []}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise KeyMgmtError(f"Software-KMS keyring {path} is not "
                               "valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(
                data.get("keys"), list):
            raise KeyMgmtError(f"Software-KMS keyring {path} is malformed")
        return data

    def _save_keyring(self, data: dict) -> None:
        path = self.keyring_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass

    def _wrap_kek(self, kek: bytes) -> str:
        master = self._master_key()
        nonce = os.urandom(12)
        return _b64e(nonce + AESGCM(master).encrypt(nonce, kek, None))

    def _unwrap_kek(self, entry: dict) -> bytes:
        wrapped = entry.get("wrappedKek")
        if not wrapped:
            raise KeyMgmtError(
                f"Key '{entry.get('kid')}' has no material (retired)")
        master = self._master_key()
        raw = _b64d(wrapped)
        try:
            return AESGCM(master).decrypt(raw[:12], raw[12:], None)
        except (InvalidTag, ValueError) as exc:
            raise KeyMgmtError(
                f"Key '{entry.get('kid')}' failed integrity "
                "verification against the software-KMS master key"
            ) from exc

    def _ensure_active(self, data: dict) -> dict:
        for entry in data["keys"]:
            if entry.get("status") == "active" and entry.get("wrappedKek"):
                return entry
        entry = self._mint(data)
        self._save_keyring(data)
        return entry

    def _mint(self, data: dict) -> dict:
        kek = AESGCM.generate_key(bit_length=256)
        entry = {"kid": kek_id(kek), "wrappedKek": self._wrap_kek(kek),
                 "status": "active", "createdAt": _now().isoformat(),
                 "retiredAt": None}
        data["keys"].append(entry)
        return entry

    def active(self) -> tuple[str, bytes]:
        data = self._load_keyring()
        entry = self._ensure_active(data)
        return entry["kid"], self._unwrap_kek(entry)

    def resolve(self, kid: str) -> bytes | None:
        data = self._load_keyring()
        for entry in data["keys"]:
            if entry.get("kid") == kid:
                if entry.get("status") == "retired" \
                        or not entry.get("wrappedKek"):
                    return None
                return self._unwrap_kek(entry)
        return None

    def list_keys(self) -> list[dict]:
        data = self._load_keyring()
        self._ensure_active(data)
        return [{"kid": e["kid"], "status": e["status"],
                 "createdAt": e.get("createdAt"),
                 "retiredAt": e.get("retiredAt"),
                 "hasMaterial": bool(e.get("wrappedKek"))}
                for e in data["keys"]]

    def rotate(self) -> dict:
        data = self._load_keyring()
        current = self._ensure_active(data)
        current["status"] = "retiring"
        fresh = self._mint(data)
        self._save_keyring(data)
        return {"previous_kid": current["kid"], "active_kid": fresh["kid"],
                "rotated_at": fresh["createdAt"]}

    def retire(self, kid: str) -> dict:
        data = self._load_keyring()
        for entry in data["keys"]:
            if entry.get("kid") == kid:
                if entry.get("status") == "active":
                    raise KeyMgmtError(
                        f"Key '{kid}' is the ACTIVE key — rotate "
                        "first, re-wrap, then retire")
                if entry.get("status") == "retired":
                    raise KeyMgmtError(f"Key '{kid}' is already retired")
                entry["status"] = "retired"
                entry["wrappedKek"] = None  # material destroyed
                entry["retiredAt"] = _now().isoformat()
                self._save_keyring(data)
                return {"kid": kid, "status": "retired",
                        "retired_at": entry["retiredAt"]}
        raise KeyMgmtError(f"Unknown key '{kid}'")


_PROVIDERS = {"env": EnvKeyProvider, "software-kms": SoftwareKmsProvider}


def provider_name() -> str:
    """The binding provider choice is the deployment's:
    OPENEHR_KMS_PROVIDER (default 'env' = Phase 10 behaviour)."""
    name = os.environ.get("OPENEHR_KMS_PROVIDER", "env").strip().lower()
    return name if name in _PROVIDERS else "env"


def get_provider() -> KeyProvider:
    return _PROVIDERS[provider_name()]()


# ---------------------------------------------------------------------------
# Schedule (published Studio config: integration/kms-rotation)
# ---------------------------------------------------------------------------

def default_schedule_payload() -> dict:
    return {
        "provider": "env",
        "rotationIntervalDays": DEFAULT_ROTATION_INTERVAL_DAYS,
        "autoRewrapOnRotate": False,
        "keyAlgorithm": "AES-256-GCM",
        "notes": ("Rotation policy for envelope-encryption KEKs "
                  "(BD-51/CMP-30). 'provider' is advisory: the binding "
                  "choice is the deployment's OPENEHR_KMS_PROVIDER. "
                  "The software-kms provider is a local SOFTWARE key "
                  "store — NOT AN HSM. Publish changes through Studio "
                  "governance; consumers read the published version."),
    }


def ensure_keymgmt_defaults(session: Session) -> None:
    """Seed the published rotation schedule (lazy, idempotent — the
    pack-manifest pattern: no main.py seed call needed)."""
    if list_config_versions(session, CONFIG_KIND, CONFIG_KEY):
        return
    session.add(StudioConfig(
        kind=CONFIG_KIND, key=CONFIG_KEY, version=1,
        status=ConfigStatus.published, payload=default_schedule_payload(),
        created_by="system", updated_by="system"))
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{CONFIG_KIND}/{CONFIG_KEY}@v1",
                 payload={"kind": CONFIG_KIND, "key": CONFIG_KEY,
                          "version": 1, "seeded_default": True})
    session.commit()


def effective_schedule(session: Session) -> dict:
    cfg = published_config(session, CONFIG_KIND, CONFIG_KEY)
    payload = (cfg.payload or {}) if cfg is not None else {}
    interval = payload.get("rotationIntervalDays")
    if isinstance(interval, bool) or not isinstance(interval, int) \
            or interval <= 0:
        interval = DEFAULT_ROTATION_INTERVAL_DAYS
    return {
        "provider_configured": payload.get("provider")
        if isinstance(payload.get("provider"), str) else "env",
        "rotation_interval_days": interval,
        "auto_rewrap_on_rotate": payload.get("autoRewrapOnRotate") is True,
        "source": (f"{CONFIG_KIND}/{CONFIG_KEY}@v{cfg.version}"
                   if cfg is not None else
                   "built-in defaults (schedule not yet seeded)"),
    }


def _last_rotation_at(session: Session,
                      provider: KeyProvider) -> datetime | None:
    """Last rotation instant: the active key's creation in the
    keyring (software-kms) or the latest kms.rotate audit entry."""
    if isinstance(provider, SoftwareKmsProvider):
        stamps = []
        for entry in provider.list_keys():
            if entry.get("status") == "active" and entry.get("createdAt"):
                try:
                    stamps.append(datetime.fromisoformat(
                        entry["createdAt"]))
                except ValueError:
                    pass
        return max(stamps) if stamps else None
    row = session.scalars(
        select(AuditLog).where(AuditLog.action == "kms.rotate")
        .order_by(AuditLog.id.desc())).first()
    if row is not None and row.created_at is not None:
        ts = row.created_at
        return ts if ts.tzinfo is not None else ts.replace(
            tzinfo=timezone.utc)
    return None


def kms_status(session: Session) -> dict:
    provider = get_provider()
    schedule = effective_schedule(session)
    active_kid, _ = provider.active()
    last = _last_rotation_at(session, provider)
    next_due = (last + timedelta(days=schedule["rotation_interval_days"])
                if last is not None else None)
    return {
        "provider": provider.name,
        "provider_label": provider.label,
        "is_hsm": False,
        "active_kid": active_kid,
        "keys": provider.list_keys(),
        "schedule": schedule,
        "last_rotation_at": last.isoformat() if last else None,
        "next_rotation_due": next_due.isoformat() if next_due else None,
        "rotation_overdue": bool(next_due and _now() > next_due),
        "config_provider_matches": (
            schedule["provider_configured"] == provider.name),
    }


# ---------------------------------------------------------------------------
# Re-wrap: move every stored envelope's DEK onto the active KEK
# ---------------------------------------------------------------------------

def _is_envelope(value) -> bool:
    return (isinstance(value, dict) and value.get("v") == 1
            and isinstance(value.get("ct"), str)
            and isinstance(value.get("wk"), str)
            and isinstance(value.get("n"), str)
            and isinstance(value.get("kid"), str))


def rewrap_envelope(envelope: dict, provider: KeyProvider) -> dict:
    """Re-wrap one envelope's DEK under the provider's ACTIVE KEK.
    Ciphertext + nonce are untouched; only `wk` and `kid` change."""
    active_kid, active_key = provider.active()
    if envelope.get("kid") == active_kid:
        return envelope
    old_key = provider.resolve(str(envelope.get("kid")))
    if old_key is None:
        raise KeyMgmtError(
            f"Envelope key '{envelope.get('kid')}' is unknown or "
            "retired — its DEK cannot be recovered for re-wrap")
    wrapped = _b64d(envelope["wk"])
    try:
        dek = AESGCM(old_key).decrypt(wrapped[:12], wrapped[12:], None)
    except (InvalidTag, ValueError) as exc:
        raise KeyMgmtError("Wrapped DEK failed integrity "
                           "verification") from exc
    nonce = os.urandom(12)
    new_wrapped = AESGCM(active_key).encrypt(nonce, dek, None)
    out = dict(envelope)
    out["kid"] = active_kid
    out["wk"] = _b64e(nonce + new_wrapped)
    return out


def _rewrap_value(value, provider: KeyProvider, stats: dict):
    """Recursively re-wrap envelopes inside a stored FHIR body:
    envelopes appear as dicts and as JSON strings (the Phase 9/10
    message-body shape: contentString holds json.dumps(envelope))."""
    if _is_envelope(value):
        stats["envelopes"] += 1
        if value.get("kid") == provider.active()[0]:
            stats["already_current"] += 1
            return value
        stats["rewrapped"] += 1
        return rewrap_envelope(value, provider)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("{") and '"wk"' in text:
            try:
                parsed = json.loads(text)
            except ValueError:
                return value
            if _is_envelope(parsed):
                stats["envelopes"] += 1
                if parsed.get("kid") == provider.active()[0]:
                    stats["already_current"] += 1
                    return value
                stats["rewrapped"] += 1
                return json.dumps(rewrap_envelope(parsed, provider))
        return value
    if isinstance(value, dict):
        return {k: _rewrap_value(v, provider, stats)
                for k, v in value.items()}
    if isinstance(value, list):
        return [_rewrap_value(v, provider, stats) for v in value]
    return value


def rewrap_all(session: Session, provider: KeyProvider) -> dict:
    """Scan every stored FHIR resource body and re-wrap any envelope
    not already under the active KEK. Bodies whose encrypted-content
    flag names the old kid get the flag updated to the new kid in the
    same pass (the Phase 10 extension mirrors the envelope kid)."""
    stats = {"resources_scanned": 0, "envelopes": 0,
             "rewrapped": 0, "already_current": 0}
    active_kid, _ = provider.active()
    rows = session.scalars(select(FhirResource)).all()
    for row in rows:
        stats["resources_scanned"] += 1
        before = stats["rewrapped"]
        new_body = _rewrap_value(row.body, provider, stats)
        if stats["rewrapped"] != before:
            if isinstance(new_body, dict):
                for ext in new_body.get("extension") or []:
                    if isinstance(ext, dict) and str(
                            ext.get("url", "")).endswith(
                            "encrypted-content"):
                        ext["valueString"] = active_kid
            row.body = new_body
            row.version = (row.version or 1) + 1
    session.flush()
    return stats


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/status")
def status(session: Session = Depends(get_db),
           user: User = Depends(require_capability("security.kms"))):
    """Provider, key inventory (metadata only), rotation schedule and
    due state. The software provider is labelled SOFTWARE-KMS /
    NOT AN HSM in every response."""
    ensure_keymgmt_defaults(session)
    return kms_status(session)


@router.get("/keys")
def keys(session: Session = Depends(get_db),
         user: User = Depends(require_capability("security.kms"))):
    ensure_keymgmt_defaults(session)
    provider = get_provider()
    return {"provider": provider.name, "provider_label": provider.label,
            "is_hsm": False, "keys": provider.list_keys()}


@router.post("/rotate")
def rotate(session: Session = Depends(get_db),
           user: User = Depends(require_capability("security.kms"))):
    """Rotate the active KEK. Software-KMS: mint a new active key,
    demote the old to retiring (data keeps reading), optionally
    re-wrap immediately when the published schedule says so. The
    env/key-file provider refuses with the deployment procedure —
    rotation there is an operator act, never faked in-process."""
    ensure_keymgmt_defaults(session)
    provider = get_provider()
    try:
        result = provider.rotate()
    except KeyMgmtError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    schedule = effective_schedule(session)
    append_audit(session, actor=user.username, action="kms.rotate",
                 entity=f"kms:{provider.name}",
                 payload={"provider": provider.name,
                          "provider_label": provider.label,
                          "previous_kid": result["previous_kid"],
                          "active_kid": result["active_kid"]})
    session.flush()
    rewrap_stats = None
    if schedule["auto_rewrap_on_rotate"]:
        rewrap_stats = rewrap_all(session, provider)
        append_audit(session, actor=user.username, action="kms.rewrap",
                     entity=f"kms:{provider.name}",
                     payload={"provider": provider.name,
                              "active_kid": result["active_kid"],
                              "trigger": "auto-on-rotate", **rewrap_stats})
    session.commit()
    return {"rotated": True, "provider": provider.name,
            "provider_label": provider.label,
            "previous_kid": result["previous_kid"],
            "active_kid": result["active_kid"],
            "rotated_at": result["rotated_at"],
            "rewrap": rewrap_stats,
            "next_step": ("retire the previous key once re-wrap "
                          "completes: POST /security/kms/keys/"
                          f"{result['previous_kid']}/retire")}


@router.post("/rewrap")
def rewrap(session: Session = Depends(get_db),
           user: User = Depends(require_capability("security.kms"))):
    """Re-wrap every stored envelope's DEK onto the active KEK."""
    ensure_keymgmt_defaults(session)
    provider = get_provider()
    active_kid, _ = provider.active()
    try:
        stats = rewrap_all(session, provider)
    except KeyMgmtError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    append_audit(session, actor=user.username, action="kms.rewrap",
                 entity=f"kms:{provider.name}",
                 payload={"provider": provider.name,
                          "active_kid": active_kid, **stats})
    session.commit()
    return {"provider": provider.name, "active_kid": active_kid, **stats}


@router.post("/keys/{kid}/retire")
def retire(kid: str, session: Session = Depends(get_db),
           user: User = Depends(require_capability("security.kms"))):
    """Retire a non-active key: its material is destroyed, so the
    kid stops resolving and leftover envelopes under it can no
    longer be unwrapped. Refused for the active key (409)."""
    ensure_keymgmt_defaults(session)
    provider = get_provider()
    try:
        result = provider.retire(kid)
    except KeyMgmtError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    append_audit(session, actor=user.username, action="kms.retire",
                 entity=f"kms:{provider.name}/{kid}",
                 payload={"provider": provider.name, "kid": kid,
                          "material_destroyed": True})
    session.commit()
    return {"provider": provider.name, **result,
            "material_destroyed": True}
