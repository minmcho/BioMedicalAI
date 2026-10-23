import type { CodeableConcept, FhirResource, Patient, Reference } from "./types";

// Small, honest display helpers for FHIR-lite resources. Anything absent in
// the resource renders as an em dash rather than an invented value.

export function codeLabel(code: CodeableConcept | undefined): string {
  if (!code) return "—";
  if (code.text) return code.text;
  const coding = code.coding?.find((c) => c.display) ?? code.coding?.[0];
  return coding?.display ?? coding?.code ?? "—";
}

export function patientName(patient: Patient | undefined): string {
  if (!patient) return "—";
  const name = patient.name?.[0];
  if (!name) return patient.id ? `Patient ${patient.id}` : "—";
  if (name.text) return name.text;
  const parts = [...(name.given ?? []), name.family ?? ""].filter(Boolean);
  return parts.length ? parts.join(" ") : (patient.id ?? "—");
}

export function refLabel(ref: Reference | undefined): string {
  if (!ref) return "—";
  return ref.display ?? ref.reference ?? "—";
}

/** Label for a plain reference string ("Patient/pat-1") — the
 *  operations APIs flatten references server-side, so pages render
 *  the string itself. */
export function refText(ref: string | null | undefined): string {
  return ref ?? "—";
}

export function observationValue(resource: FhirResource): string {
  const q = resource.valueQuantity as { value?: number; unit?: string } | undefined;
  if (q && typeof q.value === "number") return `${q.value}${q.unit ? ` ${q.unit}` : ""}`;
  if (typeof resource.valueString === "string") return resource.valueString;
  const vcc = resource.valueCodeableConcept as CodeableConcept | undefined;
  if (vcc) return codeLabel(vcc);
  return "—";
}

export function resourceDate(resource: FhirResource): string {
  const raw =
    (resource.effectiveDateTime as string | undefined) ??
    (resource.performedDateTime as string | undefined) ??
    (resource.start as string | undefined) ??
    ((resource.period as { start?: string } | undefined)?.start);
  if (!raw) return "—";
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? raw : d.toLocaleString();
}

export function resourceStatus(resource: FhirResource): string {
  return typeof resource.status === "string" ? resource.status : "—";
}
