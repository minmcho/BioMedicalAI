"""Data lake — raw zone (Phase 14, Track H).

Zone model
----------
The lake has two zones, and this module is the RAW zone:

- **Raw zone (this module):** every inbound payload — HL7 messages off
  the MLLP listener, FHIR Observations posted to the device gateway,
  sensor readings (HTTP or MQTT), offline store-and-forward batches —
  lands here AS-RECEIVED: the exact payload text, its SHA-256 content
  hash, the source it arrived by, the device/interface it came from,
  and the receive timestamp. Raw rows are IMMUTABLE: there is no
  update or delete function in this module and no endpoint anywhere
  mutates or removes a raw row. A correction is a new raw entry whose
  `supersedes_id` names the original — the original stays, byte for
  byte, so an auditor can always replay what actually arrived.
- **Curated zone:** the clinical store (FHIR resources) and the OMOP
  warehouse. Curated records are built FROM raw entries by ingestion
  (app.devices / app.interop) and by pipeline replay (app.pipelines),
  and each curated record is linked back to its raw entry by an
  append-only LakeLineageLink row — so every number in the curated
  zone can answer "where did this value come from" (the lineage
  endpoint in app.devices).

Storage is the existing application database with a dedicated table
set (`lake_raw_entries`, `lake_lineage_links`). No object-storage
infrastructure exists in this deployment to build on; the zone model
is storage-agnostic — payloads are text rows today, and the same
contract (immutable, hashed, linked) is what an object-store-backed
raw zone would keep. High-rate vital-sign streams are stored as
ingested FHIR Observations + one raw entry per posted payload (a
posted payload may carry a batch of readings), not as one row per
sample — the store decision the roadmap left to phase design time.

Offline-first store-and-forward (deployment mode)
-------------------------------------------------
The raw zone IS the store-and-forward mechanism: an edge capture that
cannot reach the curated zone still lands payloads in the raw zone
(the offline-queue ingestion endpoint in app.devices accepts batches
and marks them held), and the pipeline `replay-lake` step later
reprocesses the held raw entries into the curated zone, in receive
order, idempotently. There is deliberately no second sync engine.
"""
import hashlib

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import LakeLineageLink, LakeRawEntry

# Extension URL stamped on curated FHIR resources (DiagnosticReport,
# Observation) naming the raw entry they were built from. FHIR-native
# lineage: the pointer travels with the resource itself, and the link
# table makes it queryable in the other direction too.
RAW_ENTRY_EXTENSION = "http://opencare.org/fhir/raw-entry-id"


def content_hash(payload_text: str) -> str:
    return hashlib.sha256(payload_text.encode("utf-8")).hexdigest()


def record_raw(session: Session, *, payload_type: str, payload_text: str,
               source: str, source_label: str = "simulated",
               device_key: str | None = None, interface: str | None = None,
               patient_ref: str | None = None, disposition: str = "received",
               supersedes_id: int | None = None) -> LakeRawEntry:
    """Append one raw entry. The ONLY way raw rows come into being.
    Callers commit with their own transaction."""
    row = LakeRawEntry(
        payload_type=payload_type, payload_text=payload_text,
        content_hash=content_hash(payload_text), source=source,
        source_label=source_label, device_key=device_key,
        interface=interface, patient_ref=patient_ref,
        disposition=disposition, supersedes_id=supersedes_id)
    session.add(row)
    session.flush()
    return row


def link_curated(session: Session, raw_entry_id: int, curated_ref: str,
                 link_kind: str = "ingested-as") -> LakeLineageLink | None:
    """Append a raw -> curated lineage link (idempotent per pair:
    replaying the same ingest does not duplicate the link)."""
    existing = session.scalars(
        select(LakeLineageLink).where(
            LakeLineageLink.raw_entry_id == raw_entry_id,
            LakeLineageLink.curated_ref == curated_ref)).first()
    if existing is not None:
        return existing
    row = LakeLineageLink(raw_entry_id=raw_entry_id, curated_ref=curated_ref,
                          link_kind=link_kind)
    session.add(row)
    session.flush()
    return row


def links_for_raw(session: Session, raw_entry_id: int) -> list[LakeLineageLink]:
    return session.scalars(
        select(LakeLineageLink).where(
            LakeLineageLink.raw_entry_id == raw_entry_id)
        .order_by(LakeLineageLink.id.asc())).all()


def links_for_curated(session: Session, curated_ref: str) -> list[LakeLineageLink]:
    return session.scalars(
        select(LakeLineageLink).where(
            LakeLineageLink.curated_ref == curated_ref)
        .order_by(LakeLineageLink.id.asc())).all()


def raw_entry_out(row: LakeRawEntry, *, include_payload: bool = False) -> dict:
    out = {
        "id": row.id,
        "payload_type": row.payload_type,
        "content_hash": row.content_hash,
        "source": row.source,
        "source_label": row.source_label,
        "device_key": row.device_key,
        "interface": row.interface,
        "patient_ref": row.patient_ref,
        "disposition": row.disposition,
        "supersedes_id": row.supersedes_id,
        "received_at": row.received_at.isoformat() if row.received_at else None,
        "payload_bytes": len((row.payload_text or "").encode("utf-8")),
    }
    if include_payload:
        out["payload"] = row.payload_text
    return out
