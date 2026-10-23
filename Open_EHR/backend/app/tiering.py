"""Hot/cold tiering for the raw-zone lake — Platform Pass, Workstream 5.

What this module does
---------------------
Lake raw entries (``LakeRawEntry``) start HOT: the payload text lives
in the database row itself. As entries age past a governed hot window,
this module moves their payload bytes to a COLD store and leaves the
row as a lightweight, verifiable stub. Tiering is governed entirely by
Studio configuration and NEVER runs unless that configuration says so
(see "Policy" below) — there is no daemon, no background thread and no
schedule claimed here; the only trigger is the operator endpoint
``POST /tiering/run`` (or a direct call to :func:`run_tiering`).

Policy (Studio config, kind ``integration``, key ``tiering``)
-------------------------------------------------------------
The policy is an ordinary published Studio config, publishable through
the existing Studio API today — the ``integration`` kind's validator
passes unknown sections through, so no kind registration is needed.
Payload shape::

    {"enabled": bool,
     "hot_window_days": int,
     "classes": [{"payload_type": str-or-"*",
                  "hot_window_days": int  # optional override
                 }]}

A class whose ``payload_type`` exactly matches an entry's type wins
over a ``"*"`` wildcard class, which wins over the global window. A
class without its own ``hot_window_days`` falls back to the global
window. When nothing is published, :func:`tiering_policy` falls back
to :func:`default_tiering_payload` — tiering DISABLED, 90-day window.

Phase 17 (BD-63): the S3-shaped interface now has a REAL S3-backed
implementation (:class:`S3ColdStore`, boto3 against any S3-compatible
endpoint — MinIO in this environment), selected by the published
policy's ``backend`` field (``"s3"`` + an ``s3`` section naming
bucket/endpoint/region; credentials only from the environment).
The filesystem store below remains the DEFAULT.

Cold store — an S3-SHAPED interface, a LOCAL FILESYSTEM implementation
----------------------------------------------------------------------
The cold store sits behind a small storage-agnostic interface,
:class:`ColdStore`: ``put(key, bytes) -> ref``, ``get(ref) -> bytes``,
``exists(ref)``. That put/get-by-key shape is deliberately S3-SHAPED
so a real S3 / MinIO implementation can satisfy the same interface
later without touching the tiering job or the retrieval endpoint.

Honesty note: the implementation shipped here,
:class:`FilesystemColdStore`, is a LOCAL FILESYSTEM store rooted at
the directory named by the ``OPENCARE_COLD_DIR`` environment variable
(default ``<cwd>/cold-store``). It is NOT S3, no S3 (or MinIO, or any
object-storage service) is claimed, and nothing in this module talks
to a network object store. Objects are content-addressed: the object
key is the payload's SHA-256, stored under two-level fan-out
directories (``<root>/<sha[0:2]>/<sha[2:4]>/<sha>``) with a JSON
sidecar (``<sha>.meta.json`` beside it) recording
``{raw_entry_id, payload_type, content_hash, tiered_at,
original_bytes}``.

Why emptying ``payload_text`` respects the raw-zone contract
------------------------------------------------------------
The raw-zone contract (app.lake) is that a raw row is immutable and
auditable: the exact bytes that arrived, their SHA-256
``content_hash``, source and receive timestamp are preserved so an
auditor can always replay what arrived. Tiering moves the BYTES, not
the record:

- ``content_hash``, ``received_at`` and every other field on the row
  are left UNTOUCHED. The hash remains the verification anchor: the
  retrieval endpoint re-hashes the cold bytes and refuses to serve
  them unless the digest equals the row's preserved ``content_hash``
  (HTTP 500 on mismatch — unverified bytes are never served).
- The move is recorded as an append-only ``LakeLineageLink`` of kind
  ``tiered-cold`` whose ``curated_ref`` is ``cold://<sha256>``, plus a
  hash-chained audit row (``lake.tiering.moved``). Lineage links are
  the tiering record — there is deliberately no side table.
- The payload remains retrievable BYTE-EXACT from the cold store via
  ``GET /tiering/cold/{entry_id}`` — tiering is relocation with proof,
  not deletion, and the job is idempotent (an already-tiered entry,
  recognised by its ``tiered-cold`` link, is never moved twice).

The tension, stated honestly: the row's ``payload_text`` itself IS
changed — set to ``""`` — which is the one deliberate, governed
exception to "raw rows never change", made only under a published
enabled policy and fully recorded as above. The concrete consequence
is that existing list/detail endpoints that expose ``payload_text``
(notably ``GET /devices/lake/{id}``) will show an empty payload for a
tiered row. The retrieval endpoint in THIS module is the access path
for tiered payloads; it verifies the bytes against the preserved hash
before returning them, which the hot path never needed to do.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import append_audit
from .configstore import published_config
from .models import LakeLineageLink, LakeRawEntry, User
from .security import get_db, require_capability

router = APIRouter(prefix="/tiering", tags=["tiering"])

TIERING_KIND = "integration"
TIERING_KEY = "tiering"
TIERED_LINK_KIND = "tiered-cold"
COLD_REF_PREFIX = "cold://"


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def default_tiering_payload() -> dict:
    """The built-in fallback policy: tiering OFF, 90-day hot window."""
    return {"enabled": False, "hot_window_days": 90, "classes": []}


def _normalise_policy(raw: dict | None) -> dict:
    payload = dict(raw) if isinstance(raw, dict) else {}
    default = default_tiering_payload()
    # Phase 17 (BD-63): cold-store backend selection is governed config
    # too — payload may carry {"backend": "filesystem"|"s3",
    # "s3": {"bucket": str, "endpoint_url": str, "region": str}}.
    # Secrets NEVER live in config: S3 credentials come only from the
    # process environment (AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY,
    # e.g. a 0600 credentials file sourced by the start script).
    backend = payload.get("backend")
    if backend not in ("filesystem", "s3"):
        backend = "filesystem"
    s3_raw = payload.get("s3")
    s3_cfg: dict = {}
    if isinstance(s3_raw, dict):
        for field in ("bucket", "endpoint_url", "region"):
            value = s3_raw.get(field)
            if isinstance(value, str) and value:
                s3_cfg[field] = value
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        enabled = default["enabled"]
    window = payload.get("hot_window_days")
    if isinstance(window, bool) or not isinstance(window, int) or window < 0:
        window = default["hot_window_days"]
    classes = payload.get("classes")
    if not isinstance(classes, list):
        classes = []
    clean_classes = []
    for entry in classes:
        if not isinstance(entry, dict):
            continue
        payload_type = entry.get("payload_type")
        if not isinstance(payload_type, str) or not payload_type:
            continue
        clean: dict = {"payload_type": payload_type}
        class_window = entry.get("hot_window_days")
        if (isinstance(class_window, int)
                and not isinstance(class_window, bool)
                and class_window >= 0):
            clean["hot_window_days"] = class_window
        clean_classes.append(clean)
    return {"enabled": enabled, "hot_window_days": window,
            "classes": clean_classes, "backend": backend, "s3": s3_cfg}


def tiering_policy(session: Session) -> dict:
    """The effective tiering policy: the published ``integration`` /
    ``tiering`` Studio config, normalised, falling back to
    :func:`default_tiering_payload` when nothing is published."""
    cfg = published_config(session, TIERING_KIND, TIERING_KEY)
    if cfg is None:
        return default_tiering_payload()
    return _normalise_policy(cfg.payload or {})


def _window_for(payload_type: str, policy: dict) -> int:
    """The hot window (days) for one payload type: an exact-type class
    wins over a ``"*"`` wildcard class, which wins over the global
    window. A class without its own window falls back to the global."""
    wildcard_window: int | None = None
    for entry in policy.get("classes") or []:
        if entry.get("payload_type") == payload_type:
            if "hot_window_days" in entry:
                return int(entry["hot_window_days"])
            return int(policy["hot_window_days"])
        if entry.get("payload_type") == "*" and wildcard_window is None:
            if "hot_window_days" in entry:
                wildcard_window = int(entry["hot_window_days"])
    if wildcard_window is not None:
        return wildcard_window
    return int(policy["hot_window_days"])


# ---------------------------------------------------------------------------
# Cold store
# ---------------------------------------------------------------------------

@runtime_checkable
class ColdStore(Protocol):
    """Storage-agnostic cold-store interface (S3-shaped: put/get by
    key). ``put`` returns the object's ref (``cold://<key>``);
    ``metadata``, when supplied, is persisted alongside the object
    (the filesystem implementation writes it as a JSON sidecar)."""

    def put(self, key: str, data: bytes,
            metadata: dict | None = None) -> str:
        ...

    def get(self, ref: str) -> bytes:
        ...

    def exists(self, ref: str) -> bool:
        ...


def _key_from_ref(ref_or_key: str) -> str:
    value = str(ref_or_key)
    if value.startswith(COLD_REF_PREFIX):
        return value[len(COLD_REF_PREFIX):]
    return value


class FilesystemColdStore:
    """Local-filesystem :class:`ColdStore` — NOT S3 (see the module
    docstring). Content-addressed: the object key is the payload's
    SHA-256, stored under two-level fan-out directories with a JSON
    sidecar carrying the tiering metadata."""

    def __init__(self, root: str | Path | None = None):
        if root is None:
            root = os.environ.get("OPENCARE_COLD_DIR") or str(
                Path.cwd() / "cold-store")
        self.root = Path(root)

    def _object_path(self, key: str) -> Path:
        return self.root / key[:2] / key[2:4] / key

    def _sidecar_path(self, key: str) -> Path:
        obj = self._object_path(key)
        return obj.parent / (obj.name + ".meta.json")

    def put(self, key: str, data: bytes,
            metadata: dict | None = None) -> str:
        key = _key_from_ref(key)
        path = self._object_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Content-addressed: writing the same key again writes the
        # same bytes (idempotent); a conflicting existing object would
        # mean a hash collision or tampering, which the retrieval
        # verification — not the write path — is responsible for
        # surfacing, so the write stays unconditional and simple.
        path.write_bytes(data)
        sidecar = dict(metadata) if isinstance(metadata, dict) else {}
        sidecar.setdefault("content_hash", key)
        sidecar.setdefault("original_bytes", len(data))
        sidecar.setdefault(
            "tiered_at", datetime.now(timezone.utc).isoformat())
        self._sidecar_path(key).write_text(
            json.dumps(sidecar, sort_keys=True), encoding="utf-8")
        return f"{COLD_REF_PREFIX}{key}"

    def get(self, ref: str) -> bytes:
        key = _key_from_ref(ref)
        return self._object_path(key).read_bytes()

    def exists(self, ref: str) -> bool:
        key = _key_from_ref(ref)
        return self._object_path(key).is_file()

    def read_metadata(self, ref: str) -> dict | None:
        """The JSON sidecar for one object, or None when absent /
        unreadable. (Filesystem-specific helper, not part of the
        ColdStore protocol.)"""
        key = _key_from_ref(ref)
        path = self._sidecar_path(key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        return data if isinstance(data, dict) else None


class S3ColdStore:
    """Real S3-backed :class:`ColdStore` (Phase 17, BD-63) — talks to
    any S3-compatible endpoint (AWS S3, or MinIO in this environment)
    through boto3. Same content-addressed contract as the filesystem
    store: the object key is the payload's SHA-256 and the tiering
    metadata rides as a ``<key>.meta.json`` sidecar OBJECT, so the
    tiering job and the hash-verifying retrieval path are unchanged.

    Connection facts (endpoint / bucket / region) come from the
    published tiering config's ``s3`` section, falling back to the
    ``OPENCARE_S3_ENDPOINT`` / ``OPENCARE_S3_BUCKET`` /
    ``OPENCARE_S3_REGION`` environment variables. Credentials come
    ONLY from the standard boto3 environment chain
    (``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY``) — never from
    Studio config, never from code."""

    def __init__(self, *, bucket: str | None = None,
                 endpoint_url: str | None = None,
                 region: str | None = None, client=None):
        import boto3  # lazy: filesystem-only deployments need no boto3
        from botocore.config import Config as _BotoConfig

        self.bucket = (bucket or os.environ.get("OPENCARE_S3_BUCKET")
                       or "opencare-cold")
        endpoint = (endpoint_url if endpoint_url is not None
                    else os.environ.get("OPENCARE_S3_ENDPOINT"))
        region_name = (region or os.environ.get("OPENCARE_S3_REGION")
                       or "us-east-1")
        if client is not None:
            self._client = client
        else:
            self._client = boto3.client(
                "s3", endpoint_url=endpoint or None, region_name=region_name,
                config=_BotoConfig(s3={"addressing_style": "path"}))
        self.endpoint_url = endpoint

    # -- ColdStore protocol -------------------------------------------
    def put(self, key: str, data: bytes,
            metadata: dict | None = None) -> str:
        key = _key_from_ref(key)
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data)
        sidecar = dict(metadata) if isinstance(metadata, dict) else {}
        sidecar.setdefault("content_hash", key)
        sidecar.setdefault("original_bytes", len(data))
        sidecar.setdefault(
            "tiered_at", datetime.now(timezone.utc).isoformat())
        self._client.put_object(
            Bucket=self.bucket, Key=key + ".meta.json",
            Body=json.dumps(sidecar, sort_keys=True).encode("utf-8"),
            ContentType="application/json")
        return f"{COLD_REF_PREFIX}{key}"

    def get(self, ref: str) -> bytes:
        from botocore.exceptions import ClientError

        key = _key_from_ref(ref)
        try:
            resp = self._client.get_object(Bucket=self.bucket, Key=key)
        except ClientError as exc:
            code = (exc.response.get("Error") or {}).get("Code", "")
            if code in ("NoSuchKey", "NoSuchBucket", "404"):
                raise FileNotFoundError(
                    f"S3 cold object {key} not found in bucket "
                    f"{self.bucket}") from exc
            raise OSError(f"S3 get failed for {key}: {exc}") from exc
        return resp["Body"].read()

    def exists(self, ref: str) -> bool:
        from botocore.exceptions import ClientError

        key = _key_from_ref(ref)
        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def read_metadata(self, ref: str) -> dict | None:
        key = _key_from_ref(ref)
        try:
            resp = self._client.get_object(
                Bucket=self.bucket, Key=key + ".meta.json")
            data = json.loads(resp["Body"].read().decode("utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def ensure_bucket(self) -> None:
        """Create the bucket when absent (MinIO / first-run setup)."""
        from botocore.exceptions import ClientError

        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self.bucket)


def s3_store_from_policy(policy: dict) -> S3ColdStore:
    s3_cfg = policy.get("s3") or {}
    return S3ColdStore(bucket=s3_cfg.get("bucket"),
                       endpoint_url=s3_cfg.get("endpoint_url"),
                       region=s3_cfg.get("region"))


def cold_store_from_config(session: Session) -> ColdStore:
    """The cold store selected by the PUBLISHED tiering policy
    (Phase 17, BD-63): ``backend: "s3"`` selects the real S3 store;
    anything else keeps the filesystem default. The environment
    override ``OPENCARE_COLD_BACKEND=s3`` selects S3 when no session
    policy is in play (drills / scripts)."""
    policy = tiering_policy(session)
    if policy.get("backend") == "s3":
        return s3_store_from_policy(policy)
    return FilesystemColdStore()


def default_cold_store() -> ColdStore:
    """The cold store for API calls: resolved at CALL time (never
    import time). ``OPENCARE_COLD_BACKEND=s3`` selects the S3 store
    from the environment; otherwise the filesystem store rooted at
    ``OPENCARE_COLD_DIR``. Session-aware callers prefer
    :func:`cold_store_from_config` (published-config selection)."""
    if os.environ.get("OPENCARE_COLD_BACKEND") == "s3":
        return S3ColdStore()
    return FilesystemColdStore()


# ---------------------------------------------------------------------------
# Tiering job
# ---------------------------------------------------------------------------

def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _tiered_entry_ids(session: Session) -> set[int]:
    return set(session.scalars(
        select(LakeLineageLink.raw_entry_id).where(
            LakeLineageLink.link_kind == TIERED_LINK_KIND)).all())


def _tiered_link_for(session: Session,
                     entry_id: int) -> LakeLineageLink | None:
    return session.scalars(
        select(LakeLineageLink).where(
            LakeLineageLink.raw_entry_id == entry_id,
            LakeLineageLink.link_kind == TIERED_LINK_KIND)).first()


def run_tiering(session: Session, *, now: datetime | None = None,
                cold_store: ColdStore | None = None,
                actor: str = "system") -> dict:
    """Run one tiering pass over the raw zone.

    Disabled policy -> ``{"ran": False, "reason": "disabled"}`` and
    nothing moves. Otherwise every raw entry whose payload is
    non-empty, whose ``received_at`` is older than its class window,
    and which has no existing ``tiered-cold`` lineage link is moved:
    its bytes go to the cold store, a ``tiered-cold`` lineage link is
    appended, ``payload_text`` is emptied (hash, timestamp and every
    other field untouched), and a ``lake.tiering.moved`` audit row is
    written. Returns the tallies ``{moved, skipped_young,
    skipped_already, bytes_moved}`` (plus ``ran: True``). Idempotent:
    a second run moves 0 because moved entries are recognised by
    their lineage link. The caller owns the transaction (this
    function flushes; the API endpoint commits).
    """
    policy = tiering_policy(session)
    if not policy["enabled"]:
        return {"ran": False, "reason": "disabled"}
    store = (cold_store if cold_store is not None
             else cold_store_from_config(session))
    moment = _ensure_aware(now) if now is not None else datetime.now(
        timezone.utc)
    already = _tiered_entry_ids(session)
    rows = session.scalars(
        select(LakeRawEntry).order_by(LakeRawEntry.id.asc())).all()
    moved = 0
    skipped_young = 0
    skipped_already = 0
    bytes_moved = 0
    for row in rows:
        if row.id in already:
            skipped_already += 1
            continue
        if not row.payload_text:
            # Nothing to move (e.g. a payload that arrived empty) and
            # no tiering record to write — it stays hot and empty.
            continue
        window = _window_for(row.payload_type, policy)
        received = (_ensure_aware(row.received_at)
                    if row.received_at is not None else moment)
        age_days = (moment - received).total_seconds() / 86400.0
        if age_days <= window:
            skipped_young += 1
            continue
        data = row.payload_text.encode("utf-8")
        key = row.content_hash
        metadata = {
            "raw_entry_id": row.id,
            "payload_type": row.payload_type,
            "content_hash": row.content_hash,
            "tiered_at": moment.isoformat(),
            "original_bytes": len(data),
        }
        ref = store.put(key, data, metadata=metadata)
        cold_ref = (ref if isinstance(ref, str)
                    and ref.startswith(COLD_REF_PREFIX)
                    else f"{COLD_REF_PREFIX}{key}")
        session.add(LakeLineageLink(
            raw_entry_id=row.id, curated_ref=cold_ref,
            link_kind=TIERED_LINK_KIND))
        session.flush()
        append_audit(session, actor=actor, action="lake.tiering.moved",
                     entity=f"lake-raw:{row.id}",
                     payload={"content_hash": row.content_hash,
                              "bytes": len(data), "cold_ref": cold_ref})
        # The ONE governed mutation of a raw row: the payload text
        # leaves the row; its hash stays as the verification anchor.
        row.payload_text = ""
        session.flush()
        moved += 1
        bytes_moved += len(data)
    return {"ran": True, "moved": moved, "skipped_young": skipped_young,
            "skipped_already": skipped_already,
            "bytes_moved": bytes_moved}


# ---------------------------------------------------------------------------
# Status computation (shared by the endpoint and tests)
# ---------------------------------------------------------------------------

def tiering_status(session: Session,
                   cold_store: ColdStore | None = None) -> dict:
    """Policy + hot/cold counts. Counts come from the raw rows plus
    their ``tiered-cold`` lineage links; ``cold_bytes`` is the total
    size of the cold objects those links point at (read back through
    the store interface — an object that cannot be read contributes
    0 rather than failing the whole status)."""
    policy = tiering_policy(session)
    store = (cold_store if cold_store is not None
             else cold_store_from_config(session))
    total_rows = len(session.scalars(select(LakeRawEntry.id)).all())
    links = session.scalars(
        select(LakeLineageLink).where(
            LakeLineageLink.link_kind == TIERED_LINK_KIND)).all()
    cold_entry_ids = {link.raw_entry_id for link in links}
    cold_bytes = 0
    counted_refs: set[str] = set()
    for link in links:
        ref = link.curated_ref
        if ref in counted_refs:
            continue
        counted_refs.add(ref)
        try:
            if store.exists(ref):
                cold_bytes += len(store.get(ref))
        except OSError:
            continue
    cold_rows = len(cold_entry_ids)
    return {
        "policy": policy,
        "counts": {
            "hot_rows": total_rows - cold_rows,
            "cold_rows": cold_rows,
            "cold_bytes": cold_bytes,
        },
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/status")
def get_status(session: Session = Depends(get_db),
               user: User = Depends(require_capability("devices.read"))):
    return tiering_status(session)


@router.get("/cold/{entry_id}")
def get_cold_entry(entry_id: int, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("devices.read"))):
    """The access path for a tiered payload (see the module docstring
    on the payload_text tension): 404 when the entry does not exist
    or was never tiered; otherwise the cold bytes are read back and
    VERIFIED against the row's preserved content_hash — a mismatch
    (or an unreadable object) is a 500 integrity error and the bytes
    are never served unverified."""
    row = session.scalars(
        select(LakeRawEntry).where(LakeRawEntry.id == entry_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Raw entry {entry_id} not found")
    link = _tiered_link_for(session, row.id)
    if link is None:
        raise HTTPException(
            status_code=404,
            detail=f"Raw entry {entry_id} has not been tiered to cold storage")
    store = cold_store_from_config(session)
    try:
        data = store.get(link.curated_ref)
    except OSError:
        raise HTTPException(
            status_code=500,
            detail=(f"Integrity error: cold object {link.curated_ref} for "
                    f"raw entry {entry_id} could not be read"))
    digest = hashlib.sha256(data).hexdigest()
    if digest != row.content_hash:
        raise HTTPException(
            status_code=500,
            detail=(f"Integrity error: cold object for raw entry {entry_id} "
                    f"hashes to {digest}, which does not match the raw "
                    f"row's content_hash {row.content_hash}; refusing to "
                    "serve unverified bytes"))
    return {"id": row.id, "content_hash": row.content_hash,
            "payload_text": data.decode("utf-8"), "verified": True,
            "cold_ref": link.curated_ref}


@router.post("/run")
def run_endpoint(session: Session = Depends(get_db),
                 user: User = Depends(require_capability("pipelines.run"))):
    """Operator-triggered single tiering pass (no daemon is claimed —
    scheduling is an operator / external-scheduler concern). 409 when
    the published policy has tiering disabled."""
    policy = tiering_policy(session)
    if not policy["enabled"]:
        raise HTTPException(
            status_code=409,
            detail=("Tiering is disabled: the published integration/tiering "
                    "policy has enabled=false (or no enabled policy is "
                    "published), so no tiering run was performed"))
    result = run_tiering(session, actor=user.username)
    if not result.get("ran"):
        raise HTTPException(
            status_code=409,
            detail=f"Tiering run refused: {result.get('reason', 'disabled')}")
    session.commit()
    return result
