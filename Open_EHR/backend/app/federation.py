"""Phase 16 (WS-A2) — TEFCA/QHIN-pattern federation (BD-36) and
external-record reconciliation (BD-37).

QHIN-PATTERN, NOT THE LIVE TEFCA NETWORK: this module implements the
*pattern* a QHIN participant uses — a node publishes its identity and
peer list as governed Studio configuration, answers patient queries
from peer OpenCare nodes only when the patient's own Consent
resources permit the requesting peer + purpose, offers a
"data behind glass" references mode, and stages everything it pulls
for human reconciliation. There is NO connection to any live TEFCA
network, QHIN, or external exchange in this code — the live network
leg is an external dependency and is NOT RUN.

BD-36 — federation:

- **Governed configuration**: the local node identity + peer list are
  the published `integration/federation` Studio config
  ({node: {name, baseUrl, organization?}, peers: [{name, baseUrl,
  enabled}]}), seeded published v1 by
  :func:`ensure_federation_defaults` with an EMPTY peer list. Peer
  management rides the Studio draft -> publish lifecycle; this module
  only READS the published config and exposes it at
  `GET /federation/peers`.

- **Inbound** (`GET /federation/inbound/Patient`): the respond
  endpoint other OpenCare nodes call. The requesting peer identifies
  itself with the `X-OpenCare-Peer` header (or `?peer=`); the purpose
  rides `?purpose=` (default `treatment`). Consent is enforced HERE,
  on the responding side, against the patient's Consent resources in
  the FHIR store (read directly from the store, so Consent rows are
  honoured as data): an active Consent whose provision is a `permit`
  for this peer (actor) + purpose allows the query; a matching
  `deny` provision refuses it even if a permit also exists; NO
  matching permit -> 403, with a `federation.query.in` audit entry
  written (and committed) BEFORE the refusal is returned. Consent
  shapes tolerated: `provision.type` permit|deny,
  `provision.actor[].reference(.reference)` naming the peer by node
  name or `Organization/<name>`, `provision.purpose` as code strings
  or CodeableConcepts; a Consent naming no actor applies to any peer
  and one naming no purpose applies to any purpose.

- **Modes**: `full` returns the patient + every resource whose
  patient projection is that patient. `references` ("data behind
  glass") returns counts per resource type, bare references, and
  LIMITED demographics only (name / gender / birthDate — no
  identifiers, no clinical payload of any kind).

- **Outbound** (`POST /federation/query`): resolves the named peer
  from the published config and calls the peer's inbound endpoint
  with httpx (`trust_env=False`), forwarding the caller's bearer
  token and this node's identity as `X-OpenCare-Peer`. The peer's
  refusal is propagated (403 stays 403) — outbound NEVER bypasses
  the peer's consent rules. Every exchange is audited on both sides
  (`federation.query.out` here, `federation.query.in` there).

BD-37 — reconciliation:

- `POST /federation/pull` runs a full query and stages every returned
  record in this module's `ExternalRecord` table (source peer,
  external id, full payload, provenance) — NEVER directly in the
  chart. `GET /federation/reconciliation/{patient_id}` shows staged
  records beside the chart with a per-field side-by-side diff.
- Actions (all `federation.work`, all audited, NO auto-merge path
  exists anywhere in this module):
  `accept-new` creates the chart resource through app.fhir WITH a
  `urn:opencare:extension/provenance` extension naming the source
  peer + external id; `link-existing` records a link to an existing
  chart resource and leaves the chart byte-unchanged; `reject`
  discards the staged record from the worklist (the row is kept,
  marked rejected, for audit).
- Runtime degrade: `accept-new` for a resource type this node's
  generic FHIR surface does not support at runtime (e.g. `Consent`
  before WS-A1 lands it in `app.fhir.SUPPORTED`) is refused with a
  clear 409 — the record stays staged rather than being written to a
  chart that cannot serve it back.

Capabilities: `federation.read` (peers view, outbound query,
reconciliation view) and `federation.work` (pull + reconciliation
actions). The coordinator registers their role defaults centrally;
until then this module falls back to the defaults documented in the
integration note (read: admin/configurator/clinician/viewer; work:
admin/configurator/clinician), always honouring a published
permissions override or a centrally-registered default first.
"""
import copy
import json
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import (DateTime, Integer, JSON, String, Text,
                        UniqueConstraint, select)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import fhir
from .audit import append_audit
from .configstore import (CAPABILITY_DEFAULTS, PERMISSIONS_KEY,
                          published_config)
from .db import Base
from .models import (ConfigStatus, FhirResource, Role, StudioConfig,
                     User, utcnow)
from .security import get_current_user, get_db

router = APIRouter(prefix="/federation", tags=["federation"])

FEDERATION_KEY = "federation"  # integration/federation
PROVENANCE_URL = "urn:opencare:extension/provenance"

# Fallback role defaults, used ONLY while the capability is absent
# from app.configstore.CAPABILITY_DEFAULTS (the coordinator wires the
# central defaults at integration; a published permissions override
# always wins over both).
_LOCAL_CAPABILITY_DEFAULTS: dict[str, tuple[str, ...]] = {
    "federation.read": ("admin", "configurator", "clinician", "viewer"),
    "federation.work": ("admin", "configurator", "clinician"),
}


# ---------------------------------------------------------------------------
# This module's own table (BD-37 staging)
# ---------------------------------------------------------------------------

class ExternalRecord(Base):
    """One external record pulled from a peer, staged for human
    reconciliation — Phase 16 (WS-A2, BD-37).

    A staged record is NEVER part of the chart: it becomes chart data
    only through the explicit `accept-new` action (which writes a
    FHIR resource carrying the provenance extension). One row per
    (source peer, resource type, external id); re-pulling a record
    that is still `staged` refreshes its payload, re-pulling one a
    human already decided leaves the decision (and row) untouched.
    Status: staged | accepted-new | linked | rejected.
    """
    __tablename__ = "external_records"
    __table_args__ = (UniqueConstraint(
        "source_peer", "resource_type", "external_id"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_peer: Mapped[str] = mapped_column(String(120), nullable=False,
                                             index=True)
    resource_type: Mapped[str] = mapped_column(String(60), nullable=False,
                                               index=True)
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    patient_ref: Mapped[str | None] = mapped_column(String(200),
                                                    nullable=True, index=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, nullable=False,
                                             default=dict)
    status: Mapped[str] = mapped_column(String(20), nullable=False,
                                        default="staged", index=True)
    linked_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    acted_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    action_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                 default=utcnow,
                                                 onupdate=utcnow)


# ---------------------------------------------------------------------------
# Capability gate (central defaults / published overrides first)
# ---------------------------------------------------------------------------

def _capability_allowed(session: Session, role: Role, capability: str) -> bool:
    cfg = published_config(session, "permissions", PERMISSIONS_KEY)
    if cfg is not None:
        overrides = (cfg.payload or {}).get("capabilities", {}).get(
            capability, {})
        if isinstance(overrides, dict) and role.value in overrides:
            return bool(overrides[role.value])
    if capability in CAPABILITY_DEFAULTS:
        return role.value in CAPABILITY_DEFAULTS[capability]
    return role.value in _LOCAL_CAPABILITY_DEFAULTS.get(capability, ())


def _require_federation(capability: str):
    def dep(session: Session = Depends(get_db),
            user: User = Depends(get_current_user)) -> User:
        if not _capability_allowed(session, user.role, capability):
            raise HTTPException(
                status_code=403,
                detail=f"Role {user.role.value} lacks capability "
                       f"'{capability}'")
        return user
    return dep


# ---------------------------------------------------------------------------
# Seeded governed configuration: integration/federation
# ---------------------------------------------------------------------------

def default_federation_payload() -> dict:
    """Published federation config seed: this node's identity and an
    EMPTY peer list — a fresh deployment federates with nobody until
    a configurator publishes peers through Studio."""
    return {
        "node": {"name": "local-opencare",
                 "baseUrl": "http://127.0.0.1:8000",
                 "organization": "OpenCare (local node)"},
        "peers": [],
    }


def _seed_published(session: Session, kind: str, key: str,
                    payload: dict) -> None:
    existing = session.scalars(
        select(StudioConfig).where(StudioConfig.kind == kind,
                                   StudioConfig.key == key)).first()
    if existing is not None:
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    for action in ("studio.config.create", "studio.config.publish"):
        append_audit(session, actor="system", action=action,
                     entity=f"config:{kind}/{key}@v1",
                     payload={"kind": kind, "key": key, "version": 1,
                              "seeded_default": True})


def ensure_federation_defaults(session: Session) -> None:
    """Seed the published `integration/federation` config (idempotent)."""
    _seed_published(session, "integration", FEDERATION_KEY,
                    default_federation_payload())
    session.commit()


# ---------------------------------------------------------------------------
# Config accessors (READ published config only — peer management is Studio's)
# ---------------------------------------------------------------------------

def _federation_payload(session: Session) -> dict:
    cfg = published_config(session, "integration", FEDERATION_KEY)
    if cfg is not None and isinstance(cfg.payload, dict):
        return cfg.payload
    return default_federation_payload()


def _local_node(session: Session) -> dict:
    node = _federation_payload(session).get("node")
    if isinstance(node, dict) and node.get("name"):
        return {"name": str(node.get("name")),
                "baseUrl": str(node.get("baseUrl") or ""),
                "organization": str(node.get("organization") or "")}
    return dict(default_federation_payload()["node"])


def _peers(session: Session) -> list[dict]:
    peers = _federation_payload(session).get("peers")
    if not isinstance(peers, list):
        return []
    out = []
    for peer in peers:
        if isinstance(peer, dict) and peer.get("name"):
            out.append({"name": str(peer["name"]),
                        "baseUrl": str(peer.get("baseUrl") or ""),
                        "enabled": peer.get("enabled", True) is not False})
    return out


def _peer_by_name(session: Session, name: str) -> dict:
    for peer in _peers(session):
        if peer["name"] == name:
            return peer
    raise HTTPException(status_code=404,
                        detail=f"Federation peer '{name}' is not in the "
                               f"published integration/federation config")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _patient_ref_of(resource_type: str, body: dict, resource_id: str):
    ref = fhir._patient_ref(resource_type, body or {}, resource_id)
    if ref:
        return ref
    # Consent (and any type whose patient lives outside the generic
    # projections): read patient/subject references directly.
    for field in ("patient", "subject"):
        value = (body or {}).get(field)
        if isinstance(value, dict):
            ref = value.get("reference")
            if isinstance(ref, str) and ref.startswith("Patient/"):
                return ref
    return None


def _render(row: FhirResource) -> dict:
    return fhir._render(row)


def _find_patients(session: Session, params: dict) -> list[FhirResource]:
    rows = session.scalars(
        select(FhirResource).where(FhirResource.resource_type == "Patient")
        .order_by(FhirResource.resource_id.asc())).all()
    patient_id = params.get("patient_id") or params.get("id")
    if patient_id:
        patient_id = str(patient_id).split("/")[-1]
        return [r for r in rows if r.resource_id == patient_id]
    identifier = params.get("identifier")
    family = str(params.get("family") or "").lower()
    given = str(params.get("given") or "").lower()
    birth = params.get("birthDate") or params.get("birth_date")

    def matches(row: FhirResource) -> bool:
        body = row.body or {}
        if identifier:
            if not any(isinstance(i, dict)
                       and str(i.get("value") or "") == str(identifier)
                       for i in body.get("identifier") or []):
                return False
        if birth and body.get("birthDate") != birth:
            return False
        if family or given:
            names = body.get("name") or []
            fam = giv = ""
            if names and isinstance(names[0], dict):
                fam = str(names[0].get("family") or "").lower()
                giv = " ".join(names[0].get("given") or []).lower()
            if family and fam != family:
                return False
            if given and given not in giv:
                return False
        return True

    return [r for r in rows if matches(r)]


def _everything(session: Session, patient_ref: str) -> list[FhirResource]:
    """Every stored resource belonging to one patient: rows carrying
    the patient projection, PLUS Consent rows (whose patient link
    lives in the body and may not be projected), de-duplicated."""
    rows = session.scalars(
        select(FhirResource).where(FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.resource_type.asc(),
                  FhirResource.resource_id.asc())).all()
    seen = {(r.resource_type, r.resource_id) for r in rows}
    for row in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Consent")).all():
        key = (row.resource_type, row.resource_id)
        if key not in seen \
                and _patient_ref_of("Consent", row.body or {},
                                    row.resource_id) == patient_ref:
            rows.append(row)
            seen.add(key)
    return sorted(rows, key=lambda r: (r.resource_type != "Patient",
                                       r.resource_type, r.resource_id))


# ---------------------------------------------------------------------------
# Consent evaluation (BD-36: enforced on the RESPONDING node)
# ---------------------------------------------------------------------------

def _strings_under(value) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            out.extend(_strings_under(item))
    elif isinstance(value, list):
        for item in value:
            out.extend(_strings_under(item))
    return out


def _purpose_codes(value) -> list[str]:
    codes: list[str] = []
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                codes.append(item)
            elif isinstance(item, dict):
                if item.get("code"):
                    codes.append(str(item["code"]))
                for coding in item.get("coding") or []:
                    if isinstance(coding, dict) and coding.get("code"):
                        codes.append(str(coding["code"]))
    return codes


def _consent_decision(session: Session, patient_ref: str,
                      peer: str, purpose: str) -> tuple[bool, str]:
    """(allowed, reason). Permit requires an ACTIVE Consent for this
    patient whose provision permits this peer + purpose; a matching
    deny provision refuses outright."""
    consents = []
    for row in session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == "Consent")).all():
        if _patient_ref_of("Consent", row.body or {},
                           row.resource_id) == patient_ref:
            consents.append(row)
    if not consents:
        return False, "no Consent resources exist for this patient"
    permit_found = False
    for row in consents:
        body = row.body or {}
        if body.get("status") not in (None, "active"):
            continue
        provision = body.get("provision")
        provision = provision if isinstance(provision, dict) else {}
        ptype = str(provision.get("type") or "permit").lower()
        # Actor strings: provision.actor + top-level organisation /
        # performer fields. No actor named -> applies to any peer.
        actor_strings: list[str] = []
        for source in (provision.get("actor"), body.get("organization"),
                       body.get("performer"), provision.get("organization")):
            actor_strings.extend(_strings_under(source))
        if actor_strings:
            peer_ok = any(a == "*" or a == peer or a.endswith("/" + peer)
                          or peer in a for a in actor_strings)
            if not peer_ok:
                continue
        purposes = _purpose_codes(provision.get("purpose")) \
            or _purpose_codes(body.get("purpose"))
        if purposes and purpose not in purposes:
            continue
        if ptype == "deny":
            return False, (f"Consent/{row.resource_id} denies peer "
                           f"'{peer}' for purpose '{purpose}'")
        if ptype == "permit":
            permit_found = True
    if permit_found:
        return True, "permit found"
    return False, (f"no Consent permits peer '{peer}' for purpose "
                   f"'{purpose}'")


# ---------------------------------------------------------------------------
# Peers view
# ---------------------------------------------------------------------------

@router.get("/peers")
def list_peers(session: Session = Depends(get_db),
               user: User = Depends(_require_federation("federation.read"))):
    """The published federation config as this node sees it: its own
    identity + the peer list. Editing peers is a Studio draft ->
    publish act on `integration/federation`; there is deliberately no
    write endpoint here."""
    cfg = published_config(session, "integration", FEDERATION_KEY)
    return {"node": _local_node(session), "peers": _peers(session),
            "config": {"kind": "integration", "key": FEDERATION_KEY,
                       "version": cfg.version if cfg is not None else None}}


# ---------------------------------------------------------------------------
# Inbound: the QHIN-pattern respond endpoint
# ---------------------------------------------------------------------------

@router.get("/inbound/Patient")
def inbound_patient(request: Request,
                    patient_id: str | None = Query(default=None),
                    identifier: str | None = Query(default=None),
                    family: str | None = Query(default=None),
                    given: str | None = Query(default=None),
                    birthDate: str | None = Query(default=None),
                    mode: str = Query(default="full"),
                    purpose: str = Query(default="treatment"),
                    peer: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(
                        _require_federation("federation.read"))):
    """Answer a peer node's patient query.

    The requesting peer is the `X-OpenCare-Peer` header (falling back
    to `?peer=` and then the authenticated username). Consent for
    THAT peer + purpose is checked against the patient's Consent
    resources before any data leaves: no permit -> 403, and the
    refusal is audit-committed first (`federation.query.in`)."""
    if mode not in ("full", "references"):
        raise HTTPException(status_code=422,
                            detail="'mode' must be 'full' or 'references'")
    requesting_peer = (request.headers.get("x-opencare-peer")
                       or peer or user.username)
    params = {"patient_id": patient_id, "identifier": identifier,
              "family": family, "given": given, "birthDate": birthDate}
    matches = _find_patients(session, params)
    if not matches:
        append_audit(session, actor=user.username,
                     action="federation.query.in",
                     entity="Patient/unknown",
                     payload={"peer": requesting_peer, "purpose": purpose,
                              "mode": mode, "outcome": "not_found",
                              "params": {k: v for k, v in params.items()
                                         if v is not None}})
        session.commit()
        raise HTTPException(status_code=404,
                            detail="No patient matches the query parameters")
    patient = matches[0]
    patient_ref = f"Patient/{patient.resource_id}"
    allowed, reason = _consent_decision(session, patient_ref,
                                        requesting_peer, purpose)
    if not allowed:
        append_audit(session, actor=user.username,
                     action="federation.query.in",
                     entity=patient_ref,
                     payload={"peer": requesting_peer, "purpose": purpose,
                              "mode": mode, "outcome": "refused",
                              "reason": reason})
        session.commit()
        raise HTTPException(
            status_code=403,
            detail=f"Federation query refused: {reason}")
    resources = _everything(session, patient_ref)
    counts: dict[str, int] = {}
    for row in resources:
        counts[row.resource_type] = counts.get(row.resource_type, 0) + 1
    append_audit(session, actor=user.username,
                 action="federation.query.in", entity=patient_ref,
                 payload={"peer": requesting_peer, "purpose": purpose,
                          "mode": mode, "outcome": "permit",
                          "resource_count": len(resources),
                          "counts": counts})
    session.commit()
    node = _local_node(session)
    if mode == "references":
        body = patient.body or {}
        name = (body.get("name") or [{}])[0] \
            if isinstance(body.get("name"), list) and body.get("name") else {}
        limited = {"id": patient.resource_id,
                   "name": {"family": name.get("family"),
                            "given": name.get("given")} if isinstance(
                                name, dict) else None,
                   "gender": body.get("gender"),
                   "birthDate": body.get("birthDate")}
        return {"mode": "references", "purpose": purpose,
                "requesting_peer": requesting_peer, "node": node,
                "patient": limited, "patient_ref": patient_ref,
                "counts": counts,
                "references": [{"reference": f"{r.resource_type}/"
                                             f"{r.resource_id}",
                                "resourceType": r.resource_type}
                               for r in resources],
                "total": len(resources)}
    return {"mode": "full", "purpose": purpose,
            "requesting_peer": requesting_peer, "node": node,
            "patient": _render(patient), "patient_ref": patient_ref,
            "resources": [_render(r) for r in resources],
            "counts": counts, "total": len(resources)}


# ---------------------------------------------------------------------------
# Outbound query + pull (BD-36 client side, BD-37 staging)
# ---------------------------------------------------------------------------

def _match_params(body: dict) -> dict:
    params: dict = {}
    nested = {}
    for key in ("patient", "match", "params"):
        if isinstance(body.get(key), dict):
            nested.update(body[key])
    merged = {**nested, **body}
    for field in ("patient_id", "id", "identifier", "family", "given",
                  "birthDate", "birth_date"):
        if merged.get(field) is not None:
            params[field] = merged[field]
    if params.get("id") and not params.get("patient_id"):
        params["patient_id"] = params.pop("id")
    return params


def _call_peer(session: Session, user: User, auth_header: str | None,
               body: dict, *, mode: str) -> tuple[dict, dict, dict]:
    """Resolve the peer, call its inbound endpoint, audit the outbound
    exchange, and return (peer_cfg, params, peer_response). Peer
    refusals / misses / config problems are raised as HTTPExceptions
    AFTER the outbound audit row is committed."""
    peer_name = body.get("peer")
    if not isinstance(peer_name, str) or not peer_name.strip():
        raise HTTPException(status_code=422,
                            detail="Request requires 'peer' (a peer name "
                                   "from the published federation config)")
    peer_cfg = _peer_by_name(session, peer_name.strip())
    if not peer_cfg["enabled"]:
        raise HTTPException(status_code=409,
                            detail=f"Federation peer '{peer_cfg['name']}' "
                                   f"is disabled in the published config")
    if not peer_cfg["baseUrl"]:
        raise HTTPException(status_code=409,
                            detail=f"Federation peer '{peer_cfg['name']}' "
                                   f"has no baseUrl in the published config")
    purpose = str(body.get("purpose") or "treatment")
    params = _match_params(body)
    node = _local_node(session)
    url = peer_cfg["baseUrl"].rstrip("/") + "/federation/inbound/Patient"
    headers = {"X-OpenCare-Peer": node["name"]}
    if auth_header:
        headers["Authorization"] = auth_header
    query = {k: v for k, v in params.items() if v is not None}
    query.update({"mode": mode, "purpose": purpose})
    status_code: int | None = None
    response_payload: dict = {}
    error_detail = ""
    try:
        with httpx.Client(trust_env=False, timeout=15.0) as client:
            resp = client.get(url, params=query, headers=headers)
        status_code = resp.status_code
        try:
            parsed = resp.json()
            response_payload = parsed if isinstance(parsed, dict) else {}
        except Exception:
            response_payload = {}
        error_detail = str(response_payload.get("detail") or "")
    except httpx.HTTPError as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
    outcome = "success" if status_code == 200 else (
        "refused" if status_code == 403 else
        "not_found" if status_code == 404 else "error")
    append_audit(session, actor=user.username,
                 action="federation.query.out",
                 entity=f"peer/{peer_cfg['name']}",
                 payload={"peer": peer_cfg['name'], "purpose": purpose,
                          "mode": mode, "outcome": outcome,
                          "status_code": status_code,
                          "params": query,
                          "patient_ref": response_payload.get(
                              "patient_ref")})
    session.commit()
    if status_code is None:
        raise HTTPException(status_code=502,
                            detail=f"Federation peer '{peer_cfg['name']}' "
                                   f"could not be reached: {error_detail}")
    if status_code != 200:
        if status_code in (403, 404, 409):
            raise HTTPException(
                status_code=status_code,
                detail=error_detail or
                f"Peer '{peer_cfg['name']}' answered {status_code}")
        raise HTTPException(
            status_code=502,
            detail=f"Peer '{peer_cfg['name']}' answered {status_code}: "
                   f"{error_detail}")
    return peer_cfg, params, response_payload


@router.post("/query")
async def outbound_query(request: Request,
                         session: Session = Depends(get_db),
                         user: User = Depends(
                             _require_federation("federation.read"))):
    """Outbound federated patient query: {peer, patient_id | identifier
    | family/given/birthDate, mode: full|references, purpose?}. The
    peer enforces consent on ITS side; this node audits the exchange
    as `federation.query.out` and returns the peer's answer."""
    body = await _json_body(request)
    mode = str(body.get("mode") or "full")
    if mode not in ("full", "references"):
        raise HTTPException(status_code=422,
                            detail="'mode' must be 'full' or 'references'")
    peer_cfg, _params, response = _call_peer(
        session, user, request.headers.get("authorization"), body,
        mode=mode)
    return {**response, "peer": peer_cfg["name"], "via": "federation.query"}


@router.post("/pull")
async def pull_records(request: Request,
                       session: Session = Depends(get_db),
                       user: User = Depends(
                           _require_federation("federation.work"))):
    """BD-37 pull: run a FULL federated query against the peer and
    stage every returned record in ExternalRecord — never directly
    in the chart. Idempotent per (peer, type, external id) while a
    record is still staged; already-decided records are skipped."""
    body = await _json_body(request)
    peer_cfg, _params, response = _call_peer(
        session, user, request.headers.get("authorization"), body,
        mode="full")
    patient_ref = response.get("patient_ref")
    staged: list[ExternalRecord] = []
    skipped = 0
    for resource in response.get("resources") or []:
        if not isinstance(resource, dict):
            continue
        resource_type = resource.get("resourceType")
        external_id = resource.get("id")
        if not resource_type or not external_id:
            continue
        ref = _patient_ref_of(resource_type, resource, str(external_id)) \
            or patient_ref
        existing = session.scalars(
            select(ExternalRecord).where(
                ExternalRecord.source_peer == peer_cfg["name"],
                ExternalRecord.resource_type == resource_type,
                ExternalRecord.external_id == str(external_id))).first()
        provenance = {"peer": peer_cfg["name"],
                      "external_id": str(external_id),
                      "external_ref": f"{resource_type}/{external_id}",
                      "pulled_by": user.username,
                      "pulled_at": _now().isoformat()}
        if existing is not None:
            if existing.status != "staged":
                skipped += 1
                continue
            existing.payload = resource
            existing.patient_ref = ref
            existing.provenance = provenance
            staged.append(existing)
            continue
        row = ExternalRecord(source_peer=peer_cfg["name"],
                             resource_type=resource_type,
                             external_id=str(external_id),
                             patient_ref=ref, payload=resource,
                             provenance=provenance, status="staged")
        session.add(row)
        staged.append(row)
    session.flush()
    append_audit(session, actor=user.username, action="federation.pull",
                 entity=f"peer/{peer_cfg['name']}",
                 payload={"peer": peer_cfg["name"],
                          "patient_ref": patient_ref,
                          "staged": len(staged), "skipped_decided": skipped,
                          "external_refs": [f"{r.resource_type}/"
                                            f"{r.external_id}"
                                            for r in staged]})
    session.commit()
    return {"peer": peer_cfg["name"], "patient_ref": patient_ref,
            "staged_count": len(staged), "skipped_decided": skipped,
            "staged": [_record_out(r) for r in staged]}


# ---------------------------------------------------------------------------
# Reconciliation (BD-37)
# ---------------------------------------------------------------------------

def _record_out(row: ExternalRecord) -> dict:
    return {"id": row.id, "source_peer": row.source_peer,
            "resource_type": row.resource_type,
            "external_id": row.external_id,
            "external_ref": f"{row.resource_type}/{row.external_id}",
            "patient_ref": row.patient_ref, "status": row.status,
            "linked_ref": row.linked_ref, "payload": row.payload,
            "provenance": row.provenance, "acted_by": row.acted_by,
            "action_note": row.action_note,
            "created_at": row.created_at.isoformat()
            if row.created_at else None}


def _field_diff(external: dict, chart: dict | None) -> list[dict]:
    fields = sorted((set(external) | set(chart or {})) - {"meta"})
    diff = []
    for field in fields:
        ext_value = external.get(field)
        chart_value = (chart or {}).get(field)
        diff.append({"field": field, "external": ext_value,
                     "chart": chart_value,
                     "differs": json.dumps(ext_value, sort_keys=True,
                                           default=str)
                     != json.dumps(chart_value, sort_keys=True,
                                   default=str)})
    return diff


@router.get("/reconciliation/{patient_id}")
def reconciliation(patient_id: str,
                     session: Session = Depends(get_db),
                     user: User = Depends(
                         _require_federation("federation.read"))):
    """Staged external records for one patient beside the chart:
    per record, the same-type/same-id chart resource (if any), other
    same-type chart refs for the patient, and a field-by-field diff
    (external vs chart). Chart data is shown for comparison only —
    nothing here writes to it."""
    patient_ref = fhir._normalize_patient_ref(patient_id)
    bare_id = patient_ref.split("/")[-1]
    staged = session.scalars(
        select(ExternalRecord).where(
            ExternalRecord.patient_ref == patient_ref)
        .order_by(ExternalRecord.id.asc())).all()
    chart_rows = _everything(session, patient_ref) \
        if session.scalars(select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == bare_id)).first() is not None else []
    items = []
    for row in staged:
        same = session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == row.resource_type,
                FhirResource.resource_id == row.external_id)).first()
        same_type_refs = [f"{r.resource_type}/{r.resource_id}"
                          for r in chart_rows
                          if r.resource_type == row.resource_type]
        chart_body = _render(same) if same is not None else None
        items.append({**_record_out(row),
                      "chart_ref": (f"{row.resource_type}/"
                                    f"{row.external_id}"
                                    if same is not None else None),
                      "chart_exists": same is not None,
                      "chart_resource": chart_body,
                      "same_type_chart_refs": same_type_refs,
                      "fields": _field_diff(row.payload or {}, chart_body)})
    return {"patient_id": bare_id, "patient_ref": patient_ref,
            "items": items, "staged_count": len(items),
            "chart": [_render(r) for r in chart_rows],
            "chart_count": len(chart_rows)}


def _get_record(session: Session, record_id: int) -> ExternalRecord:
    row = session.get(ExternalRecord, record_id)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"External record {record_id} not found")
    return row


def _require_staged(row: ExternalRecord, action: str) -> None:
    if row.status != "staged":
        raise HTTPException(
            status_code=409,
            detail=f"External record {row.id} is '{row.status}' — only a "
                   f"staged record can be {action} (decisions are final; "
                   f"pull again only stages undecided records)")


@router.post("/reconciliation/items/{record_id}/accept-new")
@router.post("/external-records/{record_id}/accept-new")
def accept_new(record_id: int, session: Session = Depends(get_db),
               user: User = Depends(_require_federation("federation.work"))):
    """Accept a staged record as a NEW chart resource, written
    through app.fhir with the provenance extension naming the source
    peer + external id. Refused (409) when the type is unsupported
    by this node's FHIR surface at runtime, or when a chart resource
    with the same type/id already exists (use link-existing)."""
    row = _get_record(session, record_id)
    _require_staged(row, "accepted as new")
    supported = set(fhir.SUPPORTED) | {"QuestionnaireResponse"}
    if row.resource_type not in supported:
        raise HTTPException(
            status_code=409,
            detail=f"Resource type '{row.resource_type}' is not supported "
                   f"by this node's FHIR surface at runtime "
                   f"(supported: {', '.join(sorted(supported))}); the "
                   f"record stays staged — it cannot be accepted into a "
                   f"chart that cannot serve it back")
    existing = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == row.resource_type,
            FhirResource.resource_id == row.external_id)).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"Chart already holds {row.resource_type}/"
                   f"{row.external_id} — use link-existing instead of "
                   f"accept-new (accept-new never overwrites the chart)")
    body = copy.deepcopy(row.payload or {})
    body["resourceType"] = row.resource_type
    body["id"] = row.external_id
    extensions = [e for e in body.get("extension") or []
                  if isinstance(e, dict)]
    extensions.append({
        "url": PROVENANCE_URL,
        "valueString": (f"Federated from peer '{row.source_peer}', "
                        f"external id '{row.external_id}' "
                        f"({row.resource_type}/{row.external_id}) via "
                        f"OpenCare federation pull + reconciliation "
                        f"accept-new")})
    body["extension"] = extensions
    created = fhir.create_resource(row.resource_type, body, session, user)
    # create_resource committed; re-attach to the (still tracked) row.
    row = session.get(ExternalRecord, record_id)
    row.status = "accepted-new"
    row.linked_ref = f"{row.resource_type}/{row.external_id}"
    row.acted_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="federation.reconcile.accept_new",
                 entity=f"ExternalRecord/{row.id}",
                 payload={"peer": row.source_peer,
                          "external_ref": f"{row.resource_type}/"
                                          f"{row.external_id}",
                          "chart_ref": row.linked_ref,
                          "patient_ref": row.patient_ref,
                          "provenance_extension": PROVENANCE_URL})
    session.commit()
    return {"record": _record_out(row), "chart_resource": created}


@router.post("/reconciliation/items/{record_id}/link-existing")
@router.post("/external-records/{record_id}/link-existing")
async def link_existing(record_id: int, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(
                            _require_federation("federation.work"))):
    """Link a staged record to an EXISTING chart resource ({chart_ref}
    or {resource_type, resource_id}). The chart resource is NOT
    modified in any way — only the link is recorded."""
    row = _get_record(session, record_id)
    _require_staged(row, "linked")
    body = await _json_body(request)
    chart_ref = body.get("chart_ref") or body.get("linked_ref")
    if not chart_ref and body.get("resource_type") \
            and body.get("resource_id"):
        chart_ref = f"{body['resource_type']}/{body['resource_id']}"
    if not isinstance(chart_ref, str) or "/" not in chart_ref:
        raise HTTPException(status_code=422,
                            detail="Link requires 'chart_ref' "
                                   "(e.g. 'Observation/<id>')")
    resource_type, resource_id = chart_ref.split("/", 1)
    chart = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == resource_type,
            FhirResource.resource_id == resource_id)).first()
    if chart is None:
        raise HTTPException(status_code=404,
                            detail=f"Chart resource {chart_ref} not found")
    row.status = "linked"
    row.linked_ref = chart_ref
    row.acted_by = user.username
    session.flush()
    append_audit(session, actor=user.username,
                 action="federation.reconcile.link_existing",
                 entity=f"ExternalRecord/{row.id}",
                 payload={"peer": row.source_peer,
                          "external_ref": f"{row.resource_type}/"
                                          f"{row.external_id}",
                          "chart_ref": chart_ref,
                          "patient_ref": row.patient_ref,
                          "chart_version_unchanged": chart.version})
    session.commit()
    return {"record": _record_out(row), "chart_ref": chart_ref}


@router.post("/reconciliation/items/{record_id}/reject")
@router.post("/external-records/{record_id}/reject")
async def reject_record(record_id: int, request: Request,
                        session: Session = Depends(get_db),
                        user: User = Depends(
                            _require_federation("federation.work"))):
    """Reject a staged record: it leaves the reconciliation worklist
    (status rejected, reason kept) and never reaches the chart."""
    row = _get_record(session, record_id)
    _require_staged(row, "rejected")
    body = await _json_body(request)
    reason = body.get("reason")
    row.status = "rejected"
    row.acted_by = user.username
    row.action_note = reason if isinstance(reason, str) else None
    session.flush()
    append_audit(session, actor=user.username,
                 action="federation.reconcile.reject",
                 entity=f"ExternalRecord/{row.id}",
                 payload={"peer": row.source_peer,
                          "external_ref": f"{row.resource_type}/"
                                          f"{row.external_id}",
                          "patient_ref": row.patient_ref,
                          "reason": row.action_note})
    session.commit()
    return {"record": _record_out(row)}
