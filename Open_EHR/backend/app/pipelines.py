"""Pipeline platform — Phase 14, Track I.

Pipeline DEFINITIONS are governed Studio configuration (kind
`pipeline`, validated in app.configstore): a name, a source label, an
ordered list of steps drawn from a governed step vocabulary
(extract-table / terminology-normalise / dedup-scan / load-warehouse /
replay-lake), an optional cron `schedule` string, and an `enabled`
flag. There is no code in a definition and no expression language —
the Track B bounded-blast-radius rule applied to data processing.

Run discipline (the Phase 8 patterns, generalised):

- **Idempotency.** Every step execution carries an idempotency key
  (pipeline@version : window : step index : type). Re-running a
  pipeline over the same scope FINDS the succeeded step row by key
  and reuses its recorded result instead of re-executing; a failed
  step row is re-executed in place (its history rides in its detail).
- **Retry + dead-letter.** A step that raises is retried up to its
  definition's `retry.maxAttempts` (default 3) with the configured
  backoff; on exhaustion the step is marked failed, a
  PipelineDeadLetter row is written with the error, and the run
  stops. Replaying a dead letter starts a FRESH run (history is
  never rewritten).
- **Data-quality gates.** A step may declare checks (row-count floor,
  null-rate ceiling, value-set membership). A failed check QUARANTINES
  the batch: a DataQuarantine hold is written with the batch snapshot
  and the run stops BEFORE anything reaches the curated zone /
  warehouse. A human releases (the run resumes from the held step
  with the held data) or rejects (the run is closed as rejected) —
  see app.devices' quarantine endpoints, which share the workqueue.
- **Backfill / replay.** A run may carry a window; the replay-lake
  step reprocesses raw-zone entries in the window (or only the
  held-offline ones, in `offline` mode), skipping entries already
  linked to curated records — replay is therefore safe to repeat.

The `schedule` in a definition is configuration data (a cron string
an external scheduler or operator works from). There is NO
in-process scheduler daemon in this build: runs are triggered
through the API, and the run history is the observability surface.
That boundary is stated here rather than implied.

Steps:
- extract-table: a named server-side extract (patients / appointments
  / orders / lab-results / observations) into the run's dataset.
- terminology-normalise: map the dataset's `code` values through the
  published `terminology/code-map` config; unmapped codes are
  REPORTED in the step detail, never silently passed.
- dedup-scan: MPI probable-duplicate scan over Patient resources
  using the deterministic weights in `terminology/mpi-matching`;
  produces DedupCandidate review items. NEVER merges.
- load-warehouse: drain the transactional outbox into the OMOP
  warehouse via the existing Phase 3 ETL (idempotent by design).
- replay-lake: reprocess raw-zone entries (see above).
"""
import json
import subprocess
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import lake
from .audit import append_audit
from .configstore import published_config, published_configs
from .models import (DataQuarantine, DedupCandidate, Device, FhirResource,
                     LakeRawEntry, PipelineDeadLetter, PipelineRun,
                     PipelineStepRun, StudioConfig, User)
from .security import get_db, require_capability

router = APIRouter(prefix="/pipelines", tags=["pipelines"])

CODE_MAP_KEY = "code-map"            # terminology/code-map
MPI_KEY = "mpi-matching"             # terminology/mpi-matching
DATASET_CAP = 1000                   # rows kept in step detail / quarantine
HOLD_DATASET_CAP = 200


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _json_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Request body must be valid JSON")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Request body must be a JSON object")
    return body


class _QuarantineSignal(Exception):
    def __init__(self, failures: list[dict], dataset):
        super().__init__("data-quality checks failed")
        self.failures = failures
        self.dataset = dataset


# ---------------------------------------------------------------------------
# Extracts (named, server-side — the reports.py pattern)
# ---------------------------------------------------------------------------

def _first_code(body: dict) -> str:
    codeable = body.get("code") or body.get("medicationCodeableConcept") or {}
    codings = codeable.get("coding") or []
    for c in codings:
        if isinstance(c, dict) and c.get("code"):
            return str(c["code"])
    return ""


def _name_parts(body: dict) -> tuple[str, str]:
    names = body.get("name") or []
    if names and isinstance(names[0], dict):
        given = names[0].get("given") or []
        return str(names[0].get("family") or ""), \
            str(given[0]) if given else ""
    return "", ""


def _phones(body: dict) -> list[str]:
    out = []
    for entry in body.get("telecom") or []:
        if isinstance(entry, dict) and entry.get("value"):
            digits = "".join(ch for ch in str(entry["value"]) if ch.isdigit())
            if digits:
                out.append(digits)
    return out


def _extract_rows(session: Session, table: str) -> list[dict]:
    def rows_of(resource_type):
        return session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == resource_type)
            .order_by(FhirResource.id.asc())).all()
    if table == "patients":
        out = []
        for row in rows_of("Patient"):
            family, given = _name_parts(row.body or {})
            out.append({"id": row.resource_id, "family": family,
                        "given": given,
                        "birth_date": (row.body or {}).get("birthDate"),
                        "gender": (row.body or {}).get("gender"),
                        "phone": (_phones(row.body or {}) or [None])[0]})
        return out
    if table == "appointments":
        return [{"id": r.resource_id, "status": (r.body or {}).get("status"),
                 "start": (r.body or {}).get("start"),
                 "patient_ref": r.patient_ref}
                for r in rows_of("Appointment")]
    if table == "orders":
        return [{"id": r.resource_id, "status": (r.body or {}).get("status"),
                 "code": _first_code(r.body or {}),
                 "patient_ref": r.patient_ref}
                for r in rows_of("ServiceRequest")]
    if table == "lab-results":
        return [{"id": r.resource_id, "status": (r.body or {}).get("status"),
                 "code": _first_code(r.body or {}),
                 "patient_ref": r.patient_ref}
                for r in rows_of("DiagnosticReport")]
    if table == "observations":
        out = []
        for r in rows_of("Observation"):
            qty = (r.body or {}).get("valueQuantity") or {}
            out.append({"id": r.resource_id,
                        "code": _first_code(r.body or {}),
                        "value": qty.get("value"),
                        "unit": qty.get("unit"),
                        "patient_ref": r.patient_ref})
        return out
    raise ValueError(f"unknown extract table '{table}'")


# ---------------------------------------------------------------------------
# Step executors. Each returns {rows_in, rows_out, detail, dataset?}.
# ---------------------------------------------------------------------------

def _step_extract(session, step, ctx, run, session_factory):
    table = (step.get("params") or {}).get("table")
    rows = _extract_rows(session, table)
    return {"rows_in": 0, "rows_out": len(rows),
            "detail": {"table": table}, "dataset": rows}


def _step_normalise_rust(dataset, mappings, params):
    """Phase 16 (BD-50): the Rust terminology service path.

    Sends the whole batch ONCE to the `opencare-term` binary
    (services/rust, `opencare-term normalise --format json`) with
    {codeMap, rows} on stdin and reads {rows, unmapped} from stdout
    — the high-throughput tier for large batches, with result parity
    against the core implementation (proven by
    tests/test_phase16_term_parity.py). FAILS CLOSED with the same
    posture as _step_deidentify: a missing binary, a timeout, a
    non-zero exit, or unparseable/misshapen output raises, so the
    run's retry / dead-letter machinery engages — the step NEVER
    silently falls back to the core engine.
    """
    command = params.get("command") or "opencare-term"
    timeout = float(params.get("timeoutSeconds") or 120)
    payload_in = {"codeMap": {"mappings": mappings}, "rows": dataset}
    try:
        proc = subprocess.run(
            [command, "normalise", "--format", "json"],
            input=json.dumps(payload_in), capture_output=True,
            text=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' not "
            "found — install services/rust (cargo build) on the app "
            "PATH or set params.command to its path; step fails "
            "closed (no fallback to the core engine)")
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            f"timed out after {timeout:g}s; step fails closed "
            "(no fallback to the core engine)")
    if proc.returncode != 0:
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            f"exited {proc.returncode}: "
            f"{(proc.stderr or '').strip()[:300]}")
    try:
        parsed = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            f"returned non-JSON output ({exc}); step fails closed")
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            "returned output without a 'rows' list; step fails closed")
    out = parsed["rows"]
    if len(out) != len(dataset):
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            f"returned {len(out)} rows for {len(dataset)} input "
            "rows; step fails closed")
    unmapped_codes = parsed.get("unmapped_codes")
    if unmapped_codes is None:
        unmapped_codes = parsed.get("unmapped")
    if not isinstance(unmapped_codes, list):
        raise RuntimeError(
            f"terminology-normalise (rust): binary '{command}' "
            "returned output without an 'unmapped' code list; "
            "step fails closed")
    mapped = parsed.get("mapped")
    if mapped is None:
        mapped = sum(1 for r in out
                     if isinstance(r, dict) and r.get("normalised") is True)
    unmapped_rows = parsed.get("unmapped_rows")
    if unmapped_rows is None:
        unmapped_rows = sum(
            1 for r in out
            if isinstance(r, dict) and r.get("code")
            and r.get("normalised") is not True)
    return {"rows_in": len(dataset), "rows_out": len(out),
            "detail": {"mapped": int(mapped),
                       "unmapped_rows": int(unmapped_rows),
                       "unmapped_codes": sorted(
                           str(c) for c in unmapped_codes),
                       "code_map_entries": len(mappings)},
            "dataset": out}


def _step_normalise(session, step, ctx, run, session_factory):
    dataset = ctx.get("dataset")
    if dataset is None:
        raise ValueError("terminology-normalise requires an upstream "
                         "extract-table step in the same run")
    cfg_row = published_config(session, "terminology", CODE_MAP_KEY)
    mappings = ((cfg_row.payload or {}).get("mappings") or []) \
        if cfg_row is not None else []
    # Phase 16 (BD-50): governed engine selection. `params.engine` is
    # "core" (default — the in-core implementation below, unchanged)
    # or "rust" (the services/rust opencare-term binary, fail-closed).
    # The pipeline validator (app.configstore._validate_pipeline_step)
    # places no per-param restriction on this step type, so `engine`
    # (and the rust path's `command` / `timeoutSeconds`) need no
    # allowlist change; an unknown engine value is refused HERE, at
    # run time, so the step fails rather than guessing.
    params = step.get("params") or {}
    engine = params.get("engine") or "core"
    if engine not in ("core", "rust"):
        raise ValueError(
            f"terminology-normalise engine must be 'core' or 'rust', "
            f"got '{engine}'")
    if engine == "rust":
        return _step_normalise_rust(dataset, mappings, params)
    lookup = {}
    for m in mappings:
        if isinstance(m, dict) and m.get("fromCode"):
            lookup[(str(m.get("fromSystem") or ""), str(m["fromCode"]))] = m
            lookup.setdefault(("", str(m["fromCode"])), m)
    mapped = 0
    unmapped_rows = 0
    unmapped_codes: set[str] = set()
    out = []
    for row in dataset:
        row = dict(row)
        code = row.get("code")
        if code:
            hit = lookup.get((str(row.get("system") or ""), str(code))) \
                or lookup.get(("", str(code)))
            if hit:
                row["source_code"] = code
                row["code"] = str(hit.get("toCode") or code)
                if hit.get("toSystem"):
                    row["system"] = str(hit["toSystem"])
                row["normalised"] = True
                mapped += 1
            else:
                unmapped_rows += 1
                unmapped_codes.add(str(code))
        out.append(row)
    return {"rows_in": len(dataset), "rows_out": len(out),
            "detail": {"mapped": mapped, "unmapped_rows": unmapped_rows,
                       "unmapped_codes": sorted(unmapped_codes),
                       "code_map_entries": len(mappings)},
            "dataset": out}


def _norm_name(value: str) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalpha())


def scan_duplicate_candidates(session: Session, run_id: str | None = None) -> dict:
    """The MPI dedup scan (also callable directly). Deterministic
    scoring from the published mpi-matching config; pairs at/above
    the threshold become review candidates. Existing pairs (any
    disposition) are never duplicated and never re-opened."""
    cfg_row = published_config(session, "terminology", MPI_KEY)
    cfg = (cfg_row.payload or {}) if cfg_row is not None else {}
    weights = cfg.get("weights") or {"dob": 40, "nameExact": 40,
                                     "nameFamily": 20, "phone": 20}
    threshold = int(cfg.get("threshold") or 70)
    patients = []
    for row in session.scalars(
            select(FhirResource).where(FhirResource.resource_type == "Patient")
            .order_by(FhirResource.resource_id.asc())).all():
        body = row.body or {}
        family, given = _name_parts(body)
        patients.append({
            "id": row.resource_id, "family": _norm_name(family),
            "given": _norm_name(given), "dob": body.get("birthDate") or "",
            "phones": set(_phones(body))})
    new_count = 0
    existing_count = 0
    considered = 0
    for i in range(len(patients)):
        for j in range(i + 1, len(patients)):
            a, b = patients[i], patients[j]
            score = 0
            reasons: dict = {}
            if a["dob"] and a["dob"] == b["dob"]:
                score += int(weights.get("dob", 40))
                reasons["dob"] = "match"
            if a["family"] and a["family"] == b["family"]:
                if a["given"] and a["given"] == b["given"]:
                    score += int(weights.get("nameExact", 40))
                    reasons["name"] = "exact"
                else:
                    score += int(weights.get("nameFamily", 20))
                    reasons["name"] = "family-only"
            if a["phones"] & b["phones"]:
                score += int(weights.get("phone", 20))
                reasons["phone"] = "match"
            if score < threshold:
                continue
            considered += 1
            pair = tuple(sorted((a["id"], b["id"])))
            found = session.scalars(
                select(DedupCandidate).where(
                    DedupCandidate.patient_a == pair[0],
                    DedupCandidate.patient_b == pair[1])).first()
            if found is not None:
                existing_count += 1
                continue
            session.add(DedupCandidate(patient_a=pair[0], patient_b=pair[1],
                                       score=score, reasons=reasons,
                                       run_id=run_id))
            session.flush()
            new_count += 1
    return {"scanned_patients": len(patients), "pairs_at_threshold": considered,
            "new_candidates": new_count, "existing_candidates": existing_count,
            "threshold": threshold}


def _step_dedup(session, step, ctx, run, session_factory):
    stats = scan_duplicate_candidates(session, run_id=run.run_id)
    return {"rows_in": stats["scanned_patients"],
            "rows_out": stats["new_candidates"], "detail": stats}


def _step_load_warehouse(session, step, ctx, run, session_factory):
    if session_factory is None:
        raise ValueError("load-warehouse requires the application "
                         "session factory (runs execute through the API)")
    from . import etl
    from . import warehouse as wh
    stats = etl.run_etl(session_factory, wh.warehouse_session_factory())
    return {"rows_in": stats["processed"] + stats["dead_letter"],
            "rows_out": stats["processed"],
            "detail": {"etl": stats}}


def _step_replay(session, step, ctx, run, session_factory):
    from . import devices as device_mod
    from . import hl7 as hl7_mod
    from . import lab as lab_mod
    params = step.get("params") or {}
    payload_type = params.get("payloadType", "hl7")
    mode = params.get("mode", "backfill")
    if mode not in ("backfill", "offline"):
        raise ValueError(f"replay-lake mode must be backfill|offline, got '{mode}'")
    stmt = select(LakeRawEntry).where(LakeRawEntry.payload_type == payload_type)
    if run.window_start:
        stmt = stmt.where(LakeRawEntry.received_at >=
                          datetime.fromisoformat(run.window_start))
    if run.window_end:
        stmt = stmt.where(LakeRawEntry.received_at <=
                          datetime.fromisoformat(run.window_end))
    if mode == "offline":
        stmt = stmt.where(LakeRawEntry.disposition == "held-offline")
    entries = session.scalars(stmt.order_by(LakeRawEntry.id.asc())
                              .limit(int(params.get("limit") or 500))).all()
    processed = skipped = 0
    failures: list[dict] = []
    for entry in entries:
        if mode == "backfill" and lake.links_for_raw(session, entry.id):
            skipped += 1
            continue
        try:
            if payload_type == "hl7":
                segments = hl7_mod.parse_segments(entry.payload_text)
                sender_device = None
                msh = hl7_mod.segment_fields(segments, "MSH")
                sender = hl7_mod._hl7_field(msh, 3) if msh else ""
                if sender:
                    sender_device = device_mod.resolve_sender_device(session, sender)
                    if sender_device is None:
                        raise ValueError(f"unknown device for sender '{sender}'")
                    block = device_mod.gate_block_reason(
                        device_mod.devices_config(session), sender_device)
                    if block:
                        raise ValueError(block)
                group = lab_mod.parse_hl7_oru(entry.payload_text)
                gateway = device_mod._gateway_user(session)
                created = lab_mod.ingest_hl7_groups(
                    session, gateway, [group], source_format="hl7",
                    instrument_id=sender_device.device_key if sender_device else None)
                device_mod.link_oru_ingest(session, {"raw_entry_id": entry.id},
                                           created)
            elif payload_type == "device-reading":
                data = json.loads(entry.payload_text)
                device = session.scalars(
                    select(Device).where(
                        Device.device_key == data.get("device_key"))).first()
                if device is None:
                    raise ValueError(f"unknown device '{data.get('device_key')}'")
                device_mod.ingest_reading(
                    session, device=device, metric=str(data.get("metric") or ""),
                    value=float(data.get("value")),
                    unit=str(data.get("unit") or ""), source="replay",
                    existing_raw=entry)
            elif payload_type == "fhir-observation":
                obs = json.loads(entry.payload_text)
                device_mod.ingest_observation(session, obs=obs, source="replay",
                                               force=True, existing_raw=entry)
            processed += 1
        except Exception as exc:  # per-entry failure: reported, replay continues
            session.rollback()
            failures.append({"raw_entry_id": entry.id,
                             "error": f"{type(exc).__name__}: {exc}"[:300]})
    if entries and processed == 0 and failures:
        raise RuntimeError(
            f"replay-lake: all {len(failures)} candidate entries failed; "
            f"first: {failures[0]['error']}")
    return {"rows_in": len(entries), "rows_out": processed,
            "detail": {"mode": mode, "payload_type": payload_type,
                       "skipped_already_curated": skipped,
                       "failures": failures[:20],
                       "failure_count": len(failures)}}


def _step_deidentify(session, step, ctx, run, session_factory):
    """Platform pass: pass every dataset row through the Rust
    de-identification binary (services/rust, crate ``opencare-deid``).

    The binary is invoked once per row as
    ``<command> [--config PATH] deid --format json [--record-key KEY]``
    with the row's JSON on stdin and the de-identified JSON on stdout
    (rule-based HIPAA Safe Harbor-style removal/generalisation — see
    services/rust/README.md for the exact method statement; this step
    claims no certification). FAILS CLOSED: a missing binary, a
    non-zero exit, a timeout, or unparseable output raises, so the
    run's retry / dead-letter machinery engages — rows are NEVER
    passed through un-deidentified on an error.
    """
    dataset = ctx.get("dataset")
    if dataset is None:
        raise ValueError("deidentify requires an upstream "
                         "dataset-producing step in the same run")
    params = step.get("params") or {}
    command = params.get("command") or "opencare-deid"
    config = params.get("config")
    record_key_field = params.get("recordKeyField")
    timeout = float(params.get("timeoutSeconds") or 120)
    out = []
    for row in dataset:
        argv = [command]
        if config:
            argv += ["--config", str(config)]
        argv += ["deid", "--format", "json"]
        if record_key_field and row.get(record_key_field) is not None:
            argv += ["--record-key", str(row[record_key_field])]
        try:
            proc = subprocess.run(
                argv, input=json.dumps(row), capture_output=True,
                text=True, timeout=timeout)
        except FileNotFoundError:
            raise RuntimeError(
                f"deidentify: binary '{command}' not found — install "
                "services/rust (cargo build --release) on the app PATH "
                "or set params.command to its path; step fails closed")
        except subprocess.TimeoutExpired:
            raise RuntimeError(
                f"deidentify: binary '{command}' timed out after "
                f"{timeout:g}s on a row; step fails closed")
        if proc.returncode != 0:
            raise RuntimeError(
                f"deidentify: binary '{command}' exited "
                f"{proc.returncode}: {(proc.stderr or '').strip()[:300]}")
        try:
            out.append(json.loads(proc.stdout))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"deidentify: binary '{command}' returned non-JSON "
                f"output ({exc}); step fails closed")
    return {"rows_in": len(dataset), "rows_out": len(out),
            "detail": {"binary": command,
                       "config": config or "built-in defaults",
                       "record_key_field": record_key_field,
                       "engine": "rust opencare-deid (rule-based, "
                                 "HIPAA Safe Harbor-style)"},
            "dataset": out}


_STEP_IMPLS = {
    "extract-table": _step_extract,
    "terminology-normalise": _step_normalise,
    "dedup-scan": _step_dedup,
    "load-warehouse": _step_load_warehouse,
    "replay-lake": _step_replay,
    "deidentify": _step_deidentify,
}


# ---------------------------------------------------------------------------
# Data-quality checks
# ---------------------------------------------------------------------------

def _evaluate_checks(step: dict, dataset, rows_out: int) -> list[dict]:
    failures = []
    for check in step.get("checks") or []:
        ctype = check.get("type")
        if ctype == "row-count-floor":
            if rows_out < int(check["value"]):
                failures.append({"check": ctype, "expected_min": check["value"],
                                 "actual": rows_out})
        elif ctype in ("null-rate-ceiling", "value-set-membership"):
            if dataset is None:
                failures.append({"check": ctype, "field": check.get("field"),
                                 "error": "check requires a dataset-producing step"})
                continue
            field = check["field"]
            if ctype == "null-rate-ceiling":
                if dataset:
                    nulls = sum(1 for r in dataset
                                if r.get(field) is None or r.get(field) == "")
                    rate = nulls / len(dataset)
                    if rate > float(check["value"]):
                        failures.append({"check": ctype, "field": field,
                                         "expected_max": check["value"],
                                         "actual": round(rate, 4)})
            else:
                allowed = set(check["values"])
                bad = sorted({str(r.get(field)) for r in dataset
                              if r.get(field) is not None
                              and str(r.get(field)) not in allowed})
                if bad:
                    failures.append({"check": ctype, "field": field,
                                     "disallowed_values": bad[:20]})
    return failures


# ---------------------------------------------------------------------------
# The runner
# ---------------------------------------------------------------------------

def _step_out(row: PipelineStepRun) -> dict:
    return {"step_index": row.step_index, "step_type": row.step_type,
            "status": row.status, "attempts": row.attempts,
            "rows_in": row.rows_in, "rows_out": row.rows_out,
            "idempotency_key": row.idempotency_key,
            "detail": row.detail or {}, "error": row.error,
            "started_at": row.started_at.isoformat() if row.started_at else None,
            "finished_at": row.finished_at.isoformat() if row.finished_at else None}


def _run_out(session: Session, run: PipelineRun) -> dict:
    steps = session.scalars(
        select(PipelineStepRun).where(PipelineStepRun.run_id == run.run_id)
        .order_by(PipelineStepRun.step_index.asc())).all()
    # Steps REUSED by this run (idempotent scope hit) keep the original
    # executing run's id on their row; surface them here too, flagged.
    seen = {s.id for s in steps}
    for step_id in (run.detail or {}).get("reused_step_ids") or []:
        row = session.scalars(
            select(PipelineStepRun).where(PipelineStepRun.id == step_id)).first()
        if row is not None and row.id not in seen:
            steps.append(row)
    steps.sort(key=lambda s: s.step_index)
    return {"run_id": run.run_id, "pipeline_key": run.pipeline_key,
            "pipeline_version": run.pipeline_version, "trigger": run.trigger,
            "window_start": run.window_start, "window_end": run.window_end,
            "status": run.status, "actor": run.actor,
            "detail": run.detail or {},
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "steps": [_step_out(s) for s in steps]}


def _claim_step(session: Session, *, run: PipelineRun, index: int,
                step_type: str, scope: str) -> tuple[PipelineStepRun, dict | None]:
    """Find-or-create the step row for this idempotency scope. A
    SUCCEEDED row means the step already ran for this scope: the
    caller reuses its recorded result. Any other prior row is
    re-executed in place (history preserved in its detail)."""
    key = f"{run.pipeline_key}@v{run.pipeline_version}:{scope}:{index}:{step_type}"
    row = session.scalars(
        select(PipelineStepRun).where(
            PipelineStepRun.idempotency_key == key)).first()
    if row is not None and row.status == "succeeded":
        return row, {"rows_in": row.rows_in, "rows_out": row.rows_out,
                     "detail": row.detail or {}}
    if row is None:
        row = PipelineStepRun(run_id=run.run_id, step_index=index,
                              step_type=step_type, idempotency_key=key,
                              status="running")
        session.add(row)
    else:
        detail = dict(row.detail or {})
        history = list(detail.get("previous_runs") or [])
        history.append({"run_id": row.run_id, "status": row.status})
        detail["previous_runs"] = history[-5:]
        row.detail = detail
        row.run_id = run.run_id
        row.status = "running"
        row.error = None
    row.started_at = _now()
    row.finished_at = None
    session.flush()
    return row, None


def _execute_steps(session: Session, run: PipelineRun, payload: dict,
                   start_index: int, ctx: dict, actor: str,
                   session_factory) -> str:
    """Execute steps[start_index:] of the definition. Returns the
    run's final status. Commits progressively (each step's outcome is
    durable before the next begins — the same discipline as the
    outbox dispatcher)."""
    steps = payload.get("steps") or []
    scope = f"{run.window_start or ''}..{run.window_end or ''}"
    for index in range(start_index, len(steps)):
        step = steps[index]
        step_type = step.get("type")
        row, reused = _claim_step(session, run=run, index=index,
                                  step_type=step_type, scope=scope)
        session.commit()
        if reused is not None:
            # Idempotent scope hit: the step already succeeded for
            # this exact scope — reuse its recorded result (dataset
            # included) instead of re-executing, and say so on the run.
            detail = dict(row.detail or {})
            detail["reused_by_run"] = run.run_id
            row.detail = detail
            session.commit()
            run_detail = dict(run.detail or {})
            reused_ids = list(run_detail.get("reused_step_ids") or [])
            if row.id not in reused_ids:
                reused_ids.append(row.id)
            run_detail["reused_step_ids"] = reused_ids
            run.detail = run_detail
            session.commit()
            if step_type in ("extract-table", "terminology-normalise"):
                ctx["dataset"] = (reused["detail"] or {}).get("dataset")
            continue
        retry = step.get("retry") or {}
        max_attempts = int(retry.get("maxAttempts") or 3)
        backoff = float(retry.get("backoffSeconds") or 0)
        impl = _STEP_IMPLS[step_type]
        while True:
            row.attempts += 1
            session.commit()
            try:
                result = impl(session, step, ctx, run, session_factory)
                session.flush()
            except Exception as exc:  # noqa: BLE001 — retry / dead-letter
                session.rollback()
                row = session.scalars(
                    select(PipelineStepRun).where(
                        PipelineStepRun.id == row.id)).first()
                row.error = f"{type(exc).__name__}: {exc}"[:2000]
                session.commit()
                if row.attempts >= max_attempts:
                    row.status = "failed"
                    row.finished_at = _now()
                    session.add(PipelineDeadLetter(
                        run_id=run.run_id, pipeline_key=run.pipeline_key,
                        step_index=index, step_type=step_type,
                        error=row.error or "",
                        detail={"attempts": row.attempts,
                                "window": scope}))
                    session.flush()
                    append_audit(session, actor=actor,
                                 action="pipelines.step.dead_letter",
                                 entity=f"pipeline:{run.pipeline_key}",
                                 payload={"run_id": run.run_id,
                                          "step_index": index,
                                          "step_type": step_type,
                                          "error": row.error})
                    session.commit()
                    return "failed"
                if backoff:
                    time.sleep(min(backoff * row.attempts, 5.0))
                row.status = "running"
                session.commit()
                continue
            # DQ gates on the produced result, BEFORE anything is
            # treated as landed.
            dataset = result.get("dataset")
            failures = _evaluate_checks(step, dataset, result["rows_out"])
            if failures:
                row.status = "quarantined"
                row.rows_in = result["rows_in"]
                row.rows_out = result["rows_out"]
                row.detail = {**(result.get("detail") or {}),
                              "dq_failures": failures}
                row.finished_at = _now()
                held_dataset = (dataset or [])[:HOLD_DATASET_CAP]
                hold = DataQuarantine(
                    source="pipeline",
                    reason=(f"Pipeline '{run.pipeline_key}' step {index} "
                            f"({step_type}) failed data-quality checks: "
                            + "; ".join(f["check"] for f in failures)),
                    detail={"pipeline_key": run.pipeline_key,
                            "run_id": run.run_id, "step_index": index,
                            "step_type": step_type, "failures": failures,
                            "dataset": held_dataset,
                            "dataset_rows_total": len(dataset or []),
                            "dataset_truncated": len(dataset or []) > HOLD_DATASET_CAP},
                    pipeline_run_id=run.run_id)
                session.add(hold)
                session.flush()
                append_audit(session, actor=actor,
                             action="pipelines.quarantine",
                             entity=f"pipeline:{run.pipeline_key}",
                             payload={"run_id": run.run_id, "step_index": index,
                                      "failures": failures,
                                      "quarantine_id": hold.id})
                session.commit()
                return "quarantined"
            row.status = "succeeded"
            row.rows_in = result["rows_in"]
            row.rows_out = result["rows_out"]
            detail = dict(result.get("detail") or {})
            if dataset is not None:
                detail["dataset"] = dataset[:DATASET_CAP]
                detail["dataset_rows_total"] = len(dataset)
                detail["dataset_truncated"] = len(dataset) > DATASET_CAP
                ctx["dataset"] = dataset
            row.detail = detail
            row.finished_at = _now()
            session.commit()
            break
    return "succeeded"


def run_pipeline(session: Session, *, key: str, actor: str,
                 trigger: str = "manual", window_start: str | None = None,
                 window_end: str | None = None,
                 session_factory=None) -> dict:
    cfg_row = published_config(session, "pipeline", key)
    if cfg_row is None:
        raise HTTPException(status_code=404,
                            detail=f"Pipeline '{key}' has no published definition")
    payload = cfg_row.payload or {}
    if payload.get("enabled") is False:
        raise HTTPException(
            status_code=409,
            detail=(f"Pipeline '{key}' is disabled in its published "
                    "definition — publishing an enabled version is a "
                    "governance act, runs are refused meanwhile"))
    for label, value in (("window_start", window_start),
                         ("window_end", window_end)):
        if value is not None:
            try:
                datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            except ValueError:
                raise HTTPException(status_code=422,
                                    detail=f"'{label}' must be an ISO datetime")
    run = PipelineRun(run_id=uuid.uuid4().hex, pipeline_key=key,
                      pipeline_version=cfg_row.version, trigger=trigger,
                      window_start=window_start, window_end=window_end,
                      status="running", actor=actor)
    session.add(run)
    session.commit()
    status = _execute_steps(session, run, payload, 0, {}, actor,
                            session_factory)
    run = session.scalars(
        select(PipelineRun).where(PipelineRun.run_id == run.run_id)).first()
    run.status = status
    run.finished_at = _now()
    session.flush()
    append_audit(session, actor=actor, action="pipelines.run",
                 entity=f"pipeline:{key}",
                 payload={"run_id": run.run_id, "status": status,
                          "trigger": trigger, "version": cfg_row.version,
                          "window": {"start": window_start, "end": window_end}})
    session.commit()
    return _run_out(session, run)


def resume_quarantined_run(session: Session, hold: DataQuarantine,
                           actor: str, session_factory=None) -> dict:
    """Resume a pipeline run whose step was quarantined, with the
    HELD dataset (the human released it). Called by the quarantine
    decision endpoint in app.devices."""
    run = session.scalars(
        select(PipelineRun).where(
            PipelineRun.run_id == hold.pipeline_run_id)).first()
    if run is None:
        raise HTTPException(status_code=409,
                            detail="Quarantine entry names a pipeline run that no longer exists")
    cfg_row = published_config(session, "pipeline", run.pipeline_key)
    if cfg_row is None:
        raise HTTPException(status_code=409,
                            detail="The pipeline definition is no longer published")
    payload = cfg_row.payload or {}
    step_index = int((hold.detail or {}).get("step_index") or 0)
    dataset = (hold.detail or {}).get("dataset")
    step_row = session.scalars(
        select(PipelineStepRun).where(
            PipelineStepRun.run_id == run.run_id,
            PipelineStepRun.step_index == step_index)).first()
    if step_row is not None:
        detail = dict(step_row.detail or {})
        detail["released_from_quarantine"] = hold.id
        detail["released_by"] = actor
        step_row.detail = detail
        step_row.status = "succeeded"
        step_row.rows_out = len(dataset or [])
        step_row.finished_at = _now()
    run.status = "running"
    session.commit()
    ctx = {"dataset": dataset}
    status = _execute_steps(session, run, payload, step_index + 1, ctx,
                            actor, session_factory)
    run = session.scalars(
        select(PipelineRun).where(PipelineRun.run_id == run.run_id)).first()
    run.status = status
    run.finished_at = _now()
    session.flush()
    append_audit(session, actor=actor, action="pipelines.quarantine.resumed",
                 entity=f"pipeline:{run.pipeline_key}",
                 payload={"run_id": run.run_id, "quarantine_id": hold.id,
                          "resumed_from_step": step_index,
                          "final_status": status})
    session.commit()
    return {"run_id": run.run_id, "status": status,
            "resumed_from_step": step_index}


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

def _definition_out(session: Session, row: StudioConfig) -> dict:
    payload = row.payload or {}
    last = session.scalars(
        select(PipelineRun).where(PipelineRun.pipeline_key == row.key)
        .order_by(PipelineRun.id.desc())).first()
    return {"key": row.key, "version": row.version,
            "name": payload.get("name"), "description": payload.get("description"),
            "source": payload.get("source"), "schedule": payload.get("schedule"),
            "enabled": payload.get("enabled") is not False,
            "steps": [{"type": s.get("type"), "name": s.get("name"),
                       "checks": len(s.get("checks") or [])}
                      for s in payload.get("steps") or []],
            "last_run": ({"run_id": last.run_id, "status": last.status,
                          "finished_at": last.finished_at.isoformat()
                          if last.finished_at else None}
                         if last is not None else None)}


@router.get("/definitions")
def list_definitions(session: Session = Depends(get_db),
                     user: User = Depends(require_capability("devices.read"))):
    rows = published_configs(session, "pipeline")
    return {"items": [_definition_out(session, r) for r in rows],
            "total": len(rows)}


@router.get("/definitions/{key}")
def get_definition(key: str, session: Session = Depends(get_db),
                   user: User = Depends(require_capability("devices.read"))):
    row = published_config(session, "pipeline", key)
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Pipeline '{key}' has no published definition")
    out = _definition_out(session, row)
    out["payload"] = row.payload
    return out


@router.post("/{key}/run", status_code=201)
async def run_pipeline_endpoint(key: str, request: Request,
                                session: Session = Depends(get_db),
                                user: User = Depends(require_capability("pipelines.run"))):
    raw_body = await request.body()
    payload: dict = {}
    if raw_body.strip():
        try:
            parsed = json.loads(raw_body)
        except Exception:
            raise HTTPException(status_code=422, detail="Request body must be valid JSON")
        if not isinstance(parsed, dict):
            raise HTTPException(status_code=422, detail="Request body must be a JSON object")
        payload = parsed
    outcome = run_pipeline(
        session, key=key, actor=user.username,
        trigger=str(payload.get("trigger") or "manual"),
        window_start=payload.get("window_start"),
        window_end=payload.get("window_end"),
        session_factory=request.app.state.session_factory)
    return outcome


@router.get("/runs")
def list_runs(pipeline_key: str | None = Query(default=None),
              status: str | None = Query(default=None),
              limit: int = Query(default=50),
              session: Session = Depends(get_db),
              user: User = Depends(require_capability("devices.read"))):
    stmt = select(PipelineRun).order_by(PipelineRun.id.desc())
    if pipeline_key:
        stmt = stmt.where(PipelineRun.pipeline_key == pipeline_key)
    if status:
        stmt = stmt.where(PipelineRun.status == status)
    rows = session.scalars(stmt.limit(min(limit, 200))).all()
    items = []
    for run in rows:
        out = _run_out(session, run)
        out["steps"] = [{"step_index": s["step_index"],
                         "step_type": s["step_type"],
                         "status": s["status"]} for s in out["steps"]]
        items.append(out)
    return {"items": items, "total": len(items)}


@router.get("/runs/{run_id}")
def get_run(run_id: str, session: Session = Depends(get_db),
            user: User = Depends(require_capability("devices.read"))):
    run = session.scalars(
        select(PipelineRun).where(PipelineRun.run_id == run_id)).first()
    if run is None:
        raise HTTPException(status_code=404, detail=f"Pipeline run '{run_id}' not found")
    return _run_out(session, run)


@router.get("/dead-letters")
def list_dead_letters(status: str | None = Query(default=None),
                      session: Session = Depends(get_db),
                      user: User = Depends(require_capability("devices.read"))):
    stmt = select(PipelineDeadLetter).order_by(PipelineDeadLetter.id.desc())
    if status:
        stmt = stmt.where(PipelineDeadLetter.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [{"id": r.id, "run_id": r.run_id,
                       "pipeline_key": r.pipeline_key,
                       "step_index": r.step_index, "step_type": r.step_type,
                       "error": r.error, "detail": r.detail or {},
                       "status": r.status,
                       "created_at": r.created_at.isoformat()
                       if r.created_at else None} for r in rows],
            "total": len(rows)}


@router.post("/dead-letters/{letter_id}/replay", status_code=201)
async def replay_dead_letter(letter_id: int, request: Request,
                             session: Session = Depends(get_db),
                             user: User = Depends(require_capability("pipelines.run"))):
    """Replay = a FRESH run of the same pipeline (same window as the
    failed run) — history is never rewritten; the letter is marked
    replayed and points at the new run."""
    letter = session.scalars(
        select(PipelineDeadLetter).where(
            PipelineDeadLetter.id == letter_id)).first()
    if letter is None:
        raise HTTPException(status_code=404,
                            detail=f"Dead letter {letter_id} not found")
    if letter.status != "open":
        raise HTTPException(status_code=409,
                            detail=f"Dead letter {letter_id} is already {letter.status}")
    failed_run = session.scalars(
        select(PipelineRun).where(
            PipelineRun.run_id == letter.run_id)).first()
    outcome = run_pipeline(
        session, key=letter.pipeline_key, actor=user.username,
        trigger="dead-letter-replay",
        window_start=failed_run.window_start if failed_run else None,
        window_end=failed_run.window_end if failed_run else None,
        session_factory=request.app.state.session_factory)
    letter.status = "replayed"
    detail = dict(letter.detail or {})
    detail["replayed_as_run"] = outcome["run_id"]
    letter.detail = detail
    session.flush()
    append_audit(session, actor=user.username,
                 action="pipelines.dead_letter.replay",
                 entity=f"pipeline:{letter.pipeline_key}",
                 payload={"dead_letter_id": letter.id,
                          "new_run_id": outcome["run_id"],
                          "new_status": outcome["status"]})
    session.commit()
    return outcome


# ---------------------------------------------------------------------------
# MPI dedup candidates (human review — merge is out of scope)
# ---------------------------------------------------------------------------

def _candidate_out(row: DedupCandidate) -> dict:
    return {"id": row.id, "patient_a": row.patient_a,
            "patient_b": row.patient_b, "score": row.score,
            "reasons": row.reasons or {}, "status": row.status,
            "reviewed_by": row.reviewed_by,
            "reviewed_at": row.reviewed_at.isoformat() if row.reviewed_at else None,
            "review_note": row.review_note, "run_id": row.run_id,
            "created_at": row.created_at.isoformat() if row.created_at else None}


@router.get("/dedup/candidates")
def list_candidates(status: str | None = Query(default=None),
                    session: Session = Depends(get_db),
                    user: User = Depends(require_capability("devices.read"))):
    stmt = select(DedupCandidate).order_by(DedupCandidate.score.desc(),
                                           DedupCandidate.id.asc())
    if status:
        stmt = stmt.where(DedupCandidate.status == status)
    rows = session.scalars(stmt).all()
    return {"items": [_candidate_out(r) for r in rows], "total": len(rows),
            "note": ("Candidates are review items for a human. The "
                     "system never merges patient records on its own; "
                     "a confirmed duplicate records the disposition "
                     "only — merging is a separate governed action "
                     "outside this phase.")}


@router.post("/dedup/candidates/{candidate_id}/review")
async def review_candidate(candidate_id: int, request: Request,
                           session: Session = Depends(get_db),
                           user: User = Depends(require_capability("pipelines.run"))):
    payload = await _json_body(request)
    row = session.scalars(
        select(DedupCandidate).where(DedupCandidate.id == candidate_id)).first()
    if row is None:
        raise HTTPException(status_code=404,
                            detail=f"Dedup candidate {candidate_id} not found")
    if row.status != "pending":
        raise HTTPException(status_code=409,
                            detail=f"Candidate {candidate_id} is already {row.status}")
    decision = payload.get("decision")
    if decision not in ("not-duplicates", "confirmed-duplicate"):
        raise HTTPException(
            status_code=422,
            detail="'decision' must be 'not-duplicates' or 'confirmed-duplicate'")
    row.status = decision
    row.reviewed_by = user.username
    row.reviewed_at = _now()
    row.review_note = str(payload.get("note") or "")
    session.flush()
    append_audit(session, actor=user.username, action="pipelines.dedup.review",
                 entity=f"DedupCandidate/{row.id}",
                 payload={"pair": [row.patient_a, row.patient_b],
                          "score": row.score, "decision": decision,
                          "note": row.review_note,
                          "merge_performed": False})
    session.commit()
    return {"candidate": _candidate_out(row), "merge_performed": False,
            "note": ("Disposition recorded. No merge was performed — "
                     "merging records is a separate governed human "
                     "action, out of scope for the dedup workqueue.")}


# ---------------------------------------------------------------------------
# Seeds: pipeline definitions + terminology (code map, MPI matching)
# ---------------------------------------------------------------------------

def default_pipeline_payloads() -> dict[str, dict]:
    return {
        "nightly-warehouse-load": {
            "name": "Nightly warehouse load",
            "description": ("Drain the transactional outbox into the "
                            "OMOP warehouse (the Phase 3 ETL as a "
                            "governed pipeline step). Idempotent: "
                            "re-running with no new events changes "
                            "nothing."),
            "source": "outbox",
            "schedule": "15 2 * * *",
            "enabled": True,
            "steps": [{"type": "load-warehouse", "name": "ETL drain"}],
        },
        "terminology-normalisation": {
            "name": "Terminology normalisation",
            "description": ("Extract Observation codes and map local "
                            "codes to standard codes through the "
                            "published terminology/code-map. Unmapped "
                            "codes are reported in the run detail, "
                            "never silently passed."),
            "source": "fhir-observations",
            "schedule": "40 2 * * *",
            "enabled": True,
            "steps": [
                {"type": "extract-table", "name": "Extract observations",
                 "params": {"table": "observations"}},
                {"type": "terminology-normalise",
                 "name": "Map local codes to LOINC"},
            ],
        },
        "deidentify-export": {
            "name": "De-identified observation export",
            "description": ("Extract Observation rows and pass every "
                            "row through the Rust de-identification "
                            "step (services/rust, rule-based HIPAA "
                            "Safe Harbor-style rules) producing a "
                            "de-identified dataset as the run output. "
                            "Ships DISABLED: the opencare-deid binary "
                            "must be installed on the app PATH first — "
                            "the step fails closed when it is missing, "
                            "and a failed run never emits "
                            "un-deidentified rows."),
            "source": "fhir-observations",
            "schedule": "30 2 * * *",
            "enabled": False,
            "steps": [
                {"type": "extract-table", "name": "Extract observations",
                 "params": {"table": "observations"}},
                {"type": "deidentify", "name": "De-identify rows (Rust)",
                 "params": {"command": "opencare-deid"}},
            ],
        },
        "mpi-dedup-scan": {
            "name": "MPI duplicate scan",
            "description": ("Scan Patient resources for probable "
                            "duplicates with the deterministic rules "
                            "in terminology/mpi-matching; candidates "
                            "go to human review. Never merges."),
            "source": "fhir-patients",
            "schedule": "0 3 * * 0",
            "enabled": True,
            "steps": [{"type": "dedup-scan", "name": "Scan for duplicate pairs"}],
        },
        "lake-replay": {
            "name": "Lake replay (backfill)",
            "description": ("Reprocess raw-zone HL7 entries that have "
                            "no curated record yet (backfill after an "
                            "outage or a gate change). Ships DISABLED: "
                            "enabling it is a governance act."),
            "source": "lake-raw-zone",
            "schedule": "*/30 * * * *",
            "enabled": False,
            "steps": [{"type": "replay-lake", "name": "Replay HL7 raw entries",
                       "params": {"payloadType": "hl7", "mode": "backfill"}}],
        },
    }


def default_code_map_payload() -> dict:
    return {
        "description": ("Local -> standard code mappings consumed by "
                        "the terminology-normalise pipeline step. A "
                        "curated seed subset (the full terminology "
                        "load is site content, loaded as config)."),
        # The standard-code value set rides as `concepts` so the
        # document stays a valid, republishable terminology config
        # through the governed API (the Phase 12/13 pattern).
        "concepts": [
            {"code": "2345-7", "display": "Glucose [Mass/volume] in Serum or Plasma", "active": True},
            {"code": "718-7", "display": "Hemoglobin [Mass/volume] in Blood", "active": True},
            {"code": "6690-2", "display": "Leukocytes [#/volume] in Blood", "active": True},
            {"code": "2160-0", "display": "Creatinine [Mass/volume] in Serum or Plasma", "active": True},
            {"code": "2823-3", "display": "Potassium [Moles/volume] in Serum or Plasma", "active": True},
        ],
        "mappings": [
            {"fromCode": "GLU", "fromSystem": "http://opencare.org/local/lab",
             "toCode": "2345-7", "toSystem": "http://loinc.org",
             "display": "Glucose [Mass/volume] in Serum or Plasma"},
            {"fromCode": "HGB", "fromSystem": "http://opencare.org/local/lab",
             "toCode": "718-7", "toSystem": "http://loinc.org",
             "display": "Hemoglobin [Mass/volume] in Blood"},
            {"fromCode": "WBC", "fromSystem": "http://opencare.org/local/lab",
             "toCode": "6690-2", "toSystem": "http://loinc.org",
             "display": "Leukocytes [#/volume] in Blood"},
            {"fromCode": "CRE", "fromSystem": "http://opencare.org/local/lab",
             "toCode": "2160-0", "toSystem": "http://loinc.org",
             "display": "Creatinine [Mass/volume] in Serum or Plasma"},
            {"fromCode": "K", "fromSystem": "http://opencare.org/local/lab",
             "toCode": "2823-3", "toSystem": "http://loinc.org",
             "display": "Potassium [Moles/volume] in Serum or Plasma"},
        ],
    }


def default_mpi_payload() -> dict:
    return {
        "description": ("Deterministic MPI matching rules for the "
                        "dedup-scan pipeline step. Scores are weights "
                        "summed over independent deterministic signals; "
                        "pairs at/above the threshold become human "
                        "review candidates. No probabilistic model, "
                        "no auto-merge."),
        "concepts": [
            {"code": "dob", "display": "Date-of-birth match signal", "active": True},
            {"code": "name", "display": "Name match signal", "active": True},
            {"code": "phone", "display": "Phone match signal", "active": True},
        ],
        "weights": {"dob": 40, "nameExact": 40, "nameFamily": 20,
                    "phone": 20},
        "threshold": 70,
    }


def _seed_published(session: Session, kind: str, key: str, payload: dict) -> None:
    from .configstore import list_config_versions
    from .models import ConfigStatus, StudioConfig
    if list_config_versions(session, kind, key):
        return
    row = StudioConfig(kind=kind, key=key, version=1,
                       status=ConfigStatus.published, payload=payload,
                       created_by="system", updated_by="system")
    session.add(row)
    session.flush()
    append_audit(session, actor="system", action="studio.config.create",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "seeded_default": True})
    append_audit(session, actor="system", action="studio.config.publish",
                 entity=f"config:{kind}/{key}@v1",
                 payload={"kind": kind, "key": key, "version": 1,
                          "superseded_version": None, "seeded_default": True})


def ensure_pipelines_defaults(session: Session) -> None:
    """Seed the five pipeline definitions + the code-map and MPI
    terminology configs (all published v1). Idempotent."""
    for key, payload in default_pipeline_payloads().items():
        _seed_published(session, "pipeline", key, payload)
    _seed_published(session, "terminology", CODE_MAP_KEY,
                    default_code_map_payload())
    _seed_published(session, "terminology", MPI_KEY, default_mpi_payload())
    session.commit()
