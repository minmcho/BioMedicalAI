// Test fixtures only — obviously synthetic FHIR data used by unit/component
// tests. Nothing here is real or real-looking personal data, and nothing in
// shipped code paths imports this file.

import type {
  Appointment,
  Condition,
  DiagnosticReport,
  Encounter,
  MedicationRequest,
  Observation,
  Patient,
  Procedure,
} from "../api/types";

export const fixturePatient: Patient = {
  resourceType: "Patient",
  id: "pat-1",
  name: [{ family: "Testworth", given: ["Synthetic", "Sample"] }],
  gender: "other",
  birthDate: "2000-01-01",
  identifier: [{ system: "http://example.org/synthetic", value: "SYNTH-0001" }],
};

export const fixtureCondition: Condition = {
  resourceType: "Condition",
  id: "cond-1",
  code: { text: "Synthetic example condition" },
  subject: { reference: "Patient/pat-1" },
};

export const fixtureMedication: MedicationRequest = {
  resourceType: "MedicationRequest",
  id: "med-1",
  status: "active",
  intent: "order",
  medicationCodeableConcept: { text: "Synthetic example medication 10 mg" },
  subject: { reference: "Patient/pat-1" },
};

export const fixtureObservation: Observation = {
  resourceType: "Observation",
  id: "obs-1",
  status: "final",
  code: { text: "Synthetic example measurement" },
  subject: { reference: "Patient/pat-1" },
  effectiveDateTime: "2026-01-15T09:30:00Z",
  valueQuantity: { value: 42, unit: "units" },
};

export const fixtureReport: DiagnosticReport = {
  resourceType: "DiagnosticReport",
  id: "rep-1",
  status: "final",
  code: { text: "Synthetic example panel" },
  subject: { reference: "Patient/pat-1" },
  effectiveDateTime: "2026-01-15T10:00:00Z",
  conclusion: "Synthetic conclusion for testing.",
};

export const fixtureProcedure: Procedure = {
  resourceType: "Procedure",
  id: "proc-1",
  status: "completed",
  code: { text: "Synthetic example procedure" },
  subject: { reference: "Patient/pat-1" },
  performedDateTime: "2026-01-10T12:00:00Z",
};

export const fixtureEncounter: Encounter = {
  resourceType: "Encounter",
  id: "enc-1",
  status: "finished",
  subject: { reference: "Patient/pat-1" },
  period: { start: "2026-01-10T11:00:00Z", end: "2026-01-10T12:30:00Z" },
};

export const fixtureAppointmentBooked: Appointment = {
  resourceType: "Appointment",
  id: "appt-1",
  status: "booked",
  start: "2026-02-01T09:00:00Z",
  description: "Synthetic example appointment",
  participant: [
    { actor: { reference: "Patient/pat-1" }, status: "accepted" },
    { actor: { reference: "Practitioner/dr-1" }, status: "accepted" },
  ],
};

export const fixtureAppointmentFulfilled: Appointment = {
  ...fixtureAppointmentBooked,
  id: "appt-2",
  status: "fulfilled",
  description: "Synthetic completed appointment",
};
