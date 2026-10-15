/**
 * Hand-made SYNTHETIC fixtures shaped like the resources a public SMART
 * sandbox returns for its published test patients (Bundle-wrapped search
 * results, LOINC-coded labs). Names/data are invented for these tests.
 */

export const patientResource = {
  resourceType: "Patient",
  id: "sandbox-1001",
  name: [{ family: "Sandbox", given: ["Camila", "Maria"] }],
  gender: "female",
  birthDate: "1959-06-11",
};

export const conditionBundle = {
  resourceType: "Bundle",
  type: "searchset",
  total: 2,
  entry: [
    {
      resource: {
        resourceType: "Condition",
        id: "c1",
        code: {
          coding: [
            {
              system: "http://hl7.org/fhir/sid/icd-10-cm",
              code: "I10",
              display: "Essential (primary) hypertension",
            },
          ],
        },
        subject: { reference: "Patient/sandbox-1001" },
      },
    },
    {
      resource: {
        resourceType: "Condition",
        id: "c2",
        code: {
          coding: [
            {
              system: "http://hl7.org/fhir/sid/icd-10-cm",
              code: "E11.9",
              display: "Type 2 diabetes mellitus without complications",
            },
          ],
        },
        subject: { reference: "Patient/sandbox-1001" },
      },
    },
  ],
};

export const medicationBundle = {
  resourceType: "Bundle",
  type: "searchset",
  total: 1,
  entry: [
    {
      resource: {
        resourceType: "MedicationRequest",
        id: "m1",
        status: "active",
        intent: "order",
        medicationCodeableConcept: {
          coding: [
            {
              system: "http://www.nlm.nih.gov/research/umls/rxnorm",
              code: "1049221",
              display: "Lisinopril 10 MG Oral Tablet",
            },
          ],
        },
        subject: { reference: "Patient/sandbox-1001" },
      },
    },
  ],
};

function labObs(id: string, date: string, value: number) {
  return {
    resource: {
      resourceType: "Observation",
      id,
      status: "final",
      code: {
        coding: [{ system: "http://loinc.org", code: "4548-4", display: "Hemoglobin A1c" }],
      },
      subject: { reference: "Patient/sandbox-1001" },
      effectiveDateTime: `${date}T08:00:00Z`,
      valueQuantity: { value, unit: "%", system: "http://unitsofmeasure.org" },
    },
  };
}

export const observationBundle = {
  resourceType: "Bundle",
  type: "searchset",
  total: 5,
  entry: [
    labObs("o3", "2025-07-01", 7.1), // intentionally unordered
    labObs("o1", "2025-01-01", 7.9),
    labObs("o2", "2025-04-01", 7.4),
    {
      resource: {
        resourceType: "Observation",
        id: "o4",
        status: "final",
        code: {
          coding: [{ system: "http://loinc.org", code: "2345-7", display: "Glucose" }],
        },
        subject: { reference: "Patient/sandbox-1001" },
        effectiveDateTime: "2025-04-01T08:00:00Z",
        valueQuantity: { value: 141, unit: "mg/dL", system: "http://unitsofmeasure.org" },
      },
    },
    {
      // A text-valued observation: must be skipped by trend grouping (it
      // cannot be charted).
      resource: {
        resourceType: "Observation",
        id: "o5",
        status: "final",
        code: {
          coding: [{ system: "http://loinc.org", code: "11506-3", display: "Progress note" }],
        },
        subject: { reference: "Patient/sandbox-1001" },
        effectiveDateTime: "2025-04-02T08:00:00Z",
        valueString: "Patient reports feeling well.",
      },
    },
  ],
};
