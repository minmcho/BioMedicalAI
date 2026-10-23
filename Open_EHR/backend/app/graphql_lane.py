"""GraphQL aggregation lane — Phase 17 (BD-68, ARC-08).

A bounded GraphQL read-aggregation lane over the FHIR store, built
with Strawberry GraphQL (graphql-core) — the lane the Phase 0 schema
stub (contracts/schema.graphql) promised: frontend/mobile
aggregation only, resolving through the same store and the SAME
auth/data-scope enforcement as REST, never a second data path.

Schema (deliberately bounded — one aggregate, no mutations):

    type Query {
      patientChart(patientId: ID!, recentLimit: Int = 10): PatientChart
      health: String!
    }
    type PatientChart {
      patient: PatientSummary!
      conditions: [ConditionSummary!]!
      medications: [MedicationSummary!]!
      recentResults: [ResultSummary!]!
    }

Enforcement parity with REST (app.fhir), by construction:

- Authentication is the REST dependency itself
  (:func:`app.security.get_current_user`): anonymous callers get the
  same 401 REST gives.
- Patient-role scoping is the REST rule: a patient caller naming any
  patient other than their own ``patient_ref`` is refused 403 (the
  REST search rule — ``search_resources`` refuses a foreign patient
  filter with 403), and their own chart excludes unreleased lab
  results (the ``app.fhir.has_unreleased_tag`` rule every patient
  read path applies). Staff roles read any chart, as in REST.
- The aggregation reads the same ``FhirResource`` rows REST renders
  (via ``app.fhir._render``), so GraphQL and REST answers for the
  same synthetic patient agree field-for-field on the projected
  summary fields — proven by the parity tests.

Studio surface: kind ``integration``, key ``graphql`` —
``{"enabled": bool}`` (default enabled). Disabling the published
config closes the lane (503), the downtime-guard pattern.

Endpoints: ``POST /graphql`` (execute), ``GET /graphql/schema``
(the SDL, for client generation/inspection). Both authenticated.
"""
from __future__ import annotations

import strawberry
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import fhir as fhir_module
from . import observability
from .configstore import published_config
from .models import FhirResource, Role, User
from .security import get_current_user, get_db

router = APIRouter(tags=["graphql"])

GRAPHQL_KIND = "integration"
GRAPHQL_KEY = "graphql"


def graphql_enabled(session: Session) -> bool:
    cfg = published_config(session, GRAPHQL_KIND, GRAPHQL_KEY)
    if cfg is None or not isinstance(cfg.payload, dict):
        return True
    enabled = cfg.payload.get("enabled")
    return enabled if isinstance(enabled, bool) else True


# ---------------------------------------------------------------------------
# Summary projections (the bounded schema's shapes)
# ---------------------------------------------------------------------------

def _display_name(patient_body: dict) -> str | None:
    for name in patient_body.get("name") or []:
        if isinstance(name, dict):
            text = name.get("text")
            if isinstance(text, str) and text:
                return text
            parts = []
            given = name.get("given") or []
            if isinstance(given, list):
                parts.extend(str(g) for g in given)
            if name.get("family"):
                parts.append(str(name["family"]))
            if parts:
                return " ".join(parts)
    return None


def _code_display(codeable: dict | None) -> str | None:
    if not isinstance(codeable, dict):
        return None
    if isinstance(codeable.get("text"), str):
        return codeable["text"]
    for coding in codeable.get("coding") or []:
        if isinstance(coding, dict) and coding.get("display"):
            return str(coding["display"])
        if isinstance(coding, dict) and coding.get("code"):
            return str(coding["code"])
    return None


def _result_value(body: dict) -> str | None:
    vq = body.get("valueQuantity")
    if isinstance(vq, dict) and vq.get("value") is not None:
        unit = vq.get("unit") or vq.get("code") or ""
        return f"{vq['value']} {unit}".strip()
    if isinstance(body.get("valueString"), str):
        return body["valueString"]
    if body.get("resourceType") == "DiagnosticReport":
        conclusion = body.get("conclusion")
        if isinstance(conclusion, str):
            return conclusion
        presented = body.get("presentedForm") or []
        return f"{len(presented)} presented form(s)"
    return None


@strawberry.type
class PatientSummary:
    id: strawberry.ID
    display_name: str | None
    birth_date: str | None
    gender: str | None


@strawberry.type
class ConditionSummary:
    id: strawberry.ID
    code_display: str | None
    clinical_status: str | None


@strawberry.type
class MedicationSummary:
    id: strawberry.ID
    medication_display: str | None
    status: str | None
    intent: str | None


@strawberry.type
class ResultSummary:
    id: strawberry.ID
    resource_type: str
    code_display: str | None
    value: str | None
    status: str | None
    effective_date_time: str | None


@strawberry.type
class PatientChart:
    patient: PatientSummary
    conditions: list[ConditionSummary]
    medications: list[MedicationSummary]
    recent_results: list[ResultSummary]


# ---------------------------------------------------------------------------
# Aggregation (shared with the tests' REST-parity assertions)
# ---------------------------------------------------------------------------

def build_patient_chart(session: Session, user: User, patient_id: str,
                        recent_limit: int = 10) -> dict:
    """The chart aggregate over the FHIR store. Raises HTTPException
    with REST-identical semantics: 403 for a patient caller naming
    another patient (REST search rule), 404 for an unknown patient."""
    patient_ref = (patient_id if "/" in patient_id
                   else f"Patient/{patient_id}")
    if user.role == Role.patient:
        if patient_ref != user.patient_ref:
            raise HTTPException(
                status_code=403,
                detail="Patient callers may only access their own record")
    patient_row = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type == "Patient",
            FhirResource.resource_id == patient_ref.split("/", 1)[1])).first()
    if patient_row is None:
        raise HTTPException(status_code=404,
                            detail=f"Patient/{patient_ref} not found")
    patient_body = fhir_module._render(patient_row)

    def rows(resource_type: str):
        return session.scalars(
            select(FhirResource).where(
                FhirResource.resource_type == resource_type,
                FhirResource.patient_ref == patient_ref)
            .order_by(FhirResource.created_at.desc(),
                      FhirResource.id.desc())).all()

    is_patient = user.role == Role.patient
    conditions = []
    for row in rows("Condition"):
        body = fhir_module._render(row)
        clinical = body.get("clinicalStatus") or {}
        conditions.append({
            "id": row.resource_id,
            "code_display": _code_display(body.get("code")),
            "clinical_status": _code_display(clinical),
        })
    medications = []
    for row in rows("MedicationRequest"):
        body = fhir_module._render(row)
        medications.append({
            "id": row.resource_id,
            "medication_display": _code_display(
                body.get("medicationCodeableConcept")),
            "status": body.get("status"),
            "intent": body.get("intent"),
        })
    results = []
    result_rows = session.scalars(
        select(FhirResource).where(
            FhirResource.resource_type.in_(
                ("Observation", "DiagnosticReport")),
            FhirResource.patient_ref == patient_ref)
        .order_by(FhirResource.created_at.desc(),
                  FhirResource.id.desc())).all()
    for row in result_rows:
        if is_patient and fhir_module.has_unreleased_tag(row.body):
            continue  # REST patient paths hide unreleased lab drafts
        body = fhir_module._render(row)
        results.append({
            "id": row.resource_id,
            "resource_type": row.resource_type,
            "code_display": _code_display(body.get("code")),
            "value": _result_value(body),
            "status": body.get("status"),
            "effective_date_time": (body.get("effectiveDateTime")
                                    or body.get("effectiveInstant")),
        })
        if len(results) >= max(0, recent_limit):
            break
    return {
        "patient": {
            "id": patient_row.resource_id,
            "display_name": _display_name(patient_body),
            "birth_date": patient_body.get("birthDate"),
            "gender": patient_body.get("gender"),
        },
        "conditions": conditions,
        "medications": medications,
        "recent_results": results,
    }


@strawberry.type
class Query:
    @strawberry.field
    def health(self) -> str:
        return "ok"

    @strawberry.field
    def patient_chart(self, info: strawberry.Info, patient_id: strawberry.ID,
                      recent_limit: int = 10) -> PatientChart | None:
        ctx = info.context
        chart = build_patient_chart(ctx["session"], ctx["user"],
                                    patient_id, recent_limit)
        return PatientChart(
            patient=PatientSummary(
                id=chart["patient"]["id"],
                display_name=chart["patient"]["display_name"],
                birth_date=chart["patient"]["birth_date"],
                gender=chart["patient"]["gender"]),
            conditions=[ConditionSummary(**c) for c in chart["conditions"]],
            medications=[MedicationSummary(**m)
                         for m in chart["medications"]],
            recent_results=[ResultSummary(
                id=r["id"], resource_type=r["resource_type"],
                code_display=r["code_display"], value=r["value"],
                status=r["status"],
                effective_date_time=r["effective_date_time"])
                for r in chart["recent_results"]])


schema = strawberry.Schema(query=Query)


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@router.get("/graphql/schema", response_class=PlainTextResponse)
def graphql_schema(session: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    if not graphql_enabled(session):
        raise HTTPException(status_code=503,
                            detail="GraphQL lane disabled by config")
    return str(schema)


@router.post("/graphql")
async def graphql_execute(request: Request,
                          session: Session = Depends(get_db),
                          user: User = Depends(get_current_user)):
    if not graphql_enabled(session):
        raise HTTPException(status_code=503,
                            detail="GraphQL lane disabled by config")
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("query"), str):
        raise HTTPException(status_code=422,
                            detail="GraphQL request requires a 'query' string")
    variables = body.get("variables")
    if variables is not None and not isinstance(variables, dict):
        raise HTTPException(status_code=422,
                            detail="'variables' must be an object")
    variables = variables or {}
    # Pre-execution scope gate: when the operation names a patientId,
    # the REST refusal (403 for a patient caller naming another
    # patient) is applied BEFORE execution, exactly as REST refuses —
    # the resolver re-checks as defence in depth (and covers queries
    # whose variable arrives under the operation's own naming, since
    # the schema's argument is fixed as patientId).
    named = variables.get("patientId")
    if isinstance(named, str) and user.role == Role.patient:
        ref = named if "/" in named else f"Patient/{named}"
        if ref != user.patient_ref:
            observability.record_graphql(ok=False)
            raise HTTPException(
                status_code=403,
                detail="Patient callers may only access their own record")
    result = await schema.execute(
        body["query"], variable_values=variables,
        context_value={"session": session, "user": user})
    observability.record_graphql(ok=not result.errors)
    payload: dict = {}
    if result.data is not None:
        payload["data"] = result.data
    if result.errors:
        payload["errors"] = [
            {"message": str(e)} for e in result.errors]
    return payload
