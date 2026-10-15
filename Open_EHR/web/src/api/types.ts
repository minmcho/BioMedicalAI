// FHIR-lite types matching the OpenCare backend contract
// (contracts/fhir-resources.md). Resources are open JSON objects; these
// interfaces cover the fields the web app actually reads.

export type Role = "admin" | "configurator" | "clinician" | "viewer" | "patient";

export interface CodeableConcept {
  text?: string;
  coding?: Array<{ system?: string; code?: string; display?: string }>;
}

export interface Reference {
  reference?: string;
  display?: string;
}

export interface FhirResource {
  resourceType: string;
  id?: string;
  meta?: { versionId?: string; profile?: string[] };
  [key: string]: unknown;
}

export interface Patient extends FhirResource {
  resourceType: "Patient";
  name?: Array<{ family?: string; given?: string[]; text?: string }>;
  identifier?: Array<{ system?: string; value?: string }>;
  gender?: string;
  birthDate?: string;
}

export interface Observation extends FhirResource {
  resourceType: "Observation";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  effectiveDateTime?: string;
  valueQuantity?: { value?: number; unit?: string };
  valueString?: string;
  valueCodeableConcept?: CodeableConcept;
}

export interface Condition extends FhirResource {
  resourceType: "Condition";
  code?: CodeableConcept;
  subject?: Reference;
  clinicalStatus?: CodeableConcept;
}

export interface MedicationRequest extends FhirResource {
  resourceType: "MedicationRequest";
  status?: string;
  intent?: string;
  medicationCodeableConcept?: CodeableConcept;
  medicationReference?: Reference;
  subject?: Reference;
}

export interface DiagnosticReport extends FhirResource {
  resourceType: "DiagnosticReport";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  effectiveDateTime?: string;
  conclusion?: string;
}

export interface Procedure extends FhirResource {
  resourceType: "Procedure";
  status?: string;
  code?: CodeableConcept;
  subject?: Reference;
  performedDateTime?: string;
}

export interface Encounter extends FhirResource {
  resourceType: "Encounter";
  status?: string;
  class?: { code?: string; display?: string };
  subject?: Reference;
  period?: { start?: string; end?: string };
}

export interface AppointmentParticipant {
  actor?: Reference;
  status?: string;
  type?: CodeableConcept[];
}

export interface Appointment extends FhirResource {
  resourceType: "Appointment";
  status?: string;
  start?: string;
  end?: string;
  description?: string;
  participant?: AppointmentParticipant[];
}

export interface Bundle<T extends FhirResource = FhirResource> {
  resourceType: "Bundle";
  type: string;
  total?: number;
  entry?: Array<{ resource: T }>;
}

// Studio (module-pack lifecycle) shapes — backend/app/studio.py
export interface PackManifest {
  name: string;
  version: string;
  description?: string;
  modules?: Array<{ id: string; name: string; enabled: boolean }>;
  forms?: Array<{ id: string; name: string; fields: string[] }>;
  api_endpoints?: Array<{ method: string; path: string; enabled: boolean }>;
  [key: string]: unknown;
}

export interface ModulePack {
  id: number;
  name: string;
  version: string;
  manifest: PackManifest;
}

export type InstallationStatus =
  | "installed_draft"
  | "approved"
  | "attached"
  | "enabled"
  | "disabled";

export interface PackInstallation {
  id: number;
  pack_id: number;
  pack_name: string;
  version: string;
  status: InstallationStatus;
  installed_by: string;
}

export type StudioAction = "approve" | "attach" | "enable" | "disable";
