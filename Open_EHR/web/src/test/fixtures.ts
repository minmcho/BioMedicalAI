// Test fixtures only — obviously synthetic FHIR data used by unit/component
// tests. Nothing here is real or real-looking personal data, and nothing in
// shipped code paths imports this file.

import type {
  Appointment,
  Claim,
  Condition,
  DiagnosticReport,
  DispenseQueueItem,
  EdBoard,
  Encounter,
  FormularyResponse,
  LabCatalogResponse,
  MedicationRequest,
  ModalitiesResponse,
  Observation,
  Patient,
  Procedure,
  QcRun,
  RadiologyStudyDetail,
  RadiologyStudyItem,
  RegistryListEntry,
  RegistrySummary,
  RoomsResponse,
  ServiceRequest,
  Specimen,
  StockItem,
  SurgeryBoard,
  VerificationItem,
  WarehouseStatus,
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

// ---- Phase 2 lab fixtures (synthetic) ----

export const fixtureLabCatalog: LabCatalogResponse = {
  catalog: {
    name: "Synthetic lab catalog",
    concepts: [
      {
        code: "2823-3",
        display: "Potassium (synthetic)",
        active: true,
        unit: "mmol/L",
        referenceRange: { low: 3.5, high: 5.1, criticalLow: 2.8, criticalHigh: 6.0, unit: "mmol/L" },
      },
      {
        code: "51990-0",
        display: "Basic metabolic panel (synthetic)",
        active: true,
        panel: true,
        components: ["2823-3"],
      },
    ],
  },
  catalog_version: 1,
  workflow: { states: ["ordered", "collected"], transitions: [{ from: "ordered", to: "collected" }] },
  workflow_version: 1,
};

export const fixtureLabOrder: ServiceRequest = {
  resourceType: "ServiceRequest",
  id: "ord-lab-1",
  status: "ordered",
  intent: "order",
  code: { text: "Basic metabolic panel (synthetic)", coding: [{ code: "51990-0" }] },
  subject: { reference: "Patient/pat-1" },
};

export const fixtureSpecimen: Specimen = {
  resourceType: "Specimen",
  id: "spec-1",
  status: "received",
  accessionIdentifier: { system: "urn:opencare:accession", value: "ACC-20260115-0001" },
  type: { text: "Blood specimen" },
  subject: { reference: "Patient/pat-1" },
  request: [{ reference: "ServiceRequest/ord-lab-1" }],
  receivedDateTime: "2026-01-15T10:05:00Z",
};

export const fixtureQcRun: QcRun = {
  id: 7,
  analyte_code: "2823-3",
  control_material: "SYN-QC-LOT-1",
  expected_low: 3.8,
  expected_high: 4.2,
  measured_value: 5.9,
  unit: "mmol/L",
  passed: false,
  performed_by: "dev-clinician",
  created_at: "2026-01-15T09:00:00Z",
};

export const fixtureVerificationItem: VerificationItem = {
  report: {
    resourceType: "DiagnosticReport",
    id: "rep-lab-1",
    status: "preliminary",
    code: { text: "Basic metabolic panel (synthetic)" },
    subject: { reference: "Patient/pat-1" },
  },
  order_id: "ord-lab-1",
  order_status: "resulted",
  stage: "awaiting-verification",
  critical: true,
  qc_blocked: [{ analyte_code: "2823-3", qc_run: fixtureQcRun }],
  results: [
    {
      observation_id: "obs-lab-1",
      code: "2823-3",
      display: "Potassium (synthetic)",
      value: 6.4,
      unit: "mmol/L",
      interpretation: "HH",
      status: "preliminary",
    },
  ],
};

// ---- Phase 3: population health fixtures (synthetic aggregates) ----

export const fixtureRegistryList: RegistryListEntry[] = [
  {
    key: "diabetes",
    name: "Diabetes registry",
    definition: {
      name: "Diabetes registry",
      description: "Synthetic demo registry.",
      conditionCodes: [{ system: "http://snomed.info/sct", code: "44054006" }],
      lab: { system: "http://loinc.org", code: "4548-4", comparator: "<", threshold: 7.0, windowDays: 365 },
      smallCellThreshold: 5,
    },
  },
];

export const fixtureDiabetesSummary: RegistrySummary = {
  registry: "diabetes",
  name: "Diabetes registry",
  member_count: 12,
  lab: {
    code: "4548-4",
    comparator: "<",
    threshold: 7.0,
    window_days: 365,
    members_with_current_result: 10,
    pct_in_range: 50.0,
  },
  stratification: {
    by_gender: [
      { value: "female", count: 7, suppressed: false },
      { value: "male", count: "<5", suppressed: true },
    ],
    by_age_band: [{ value: "45-64", count: 6, suppressed: false }],
    as_of: "2025-10-15",
  },
  small_cell_threshold: 5,
};

// ---- Phase 4A: billing + pharmacy fixtures (synthetic) ----

export const fixtureClaim: Claim = {
  resourceType: "Claim",
  id: "claim-1",
  status: "submitted",
  use: "claim",
  patient: { reference: "Patient/pat-1" },
  encounter: [{ reference: "Encounter/enc-1" }],
  created: "2026-01-10T10:05:00Z",
  diagnosis: [
    {
      sequence: 1,
      diagnosisCodeableConcept: {
        coding: [{ system: "http://snomed.info/sct", code: "44054006", display: "Type 2 diabetes mellitus" }],
        text: "Type 2 diabetes mellitus",
      },
    },
  ],
  item: [
    {
      sequence: 1,
      productOrService: {
        coding: [{ system: "http://snomed.info/sct", code: "44054006", display: "Type 2 diabetes mellitus" }],
        text: "Type 2 diabetes mellitus — problem evaluation",
      },
      quantity: { value: 1 },
      unitPrice: { value: 55, currency: "USD" },
      net: { value: 55, currency: "USD" },
      extension: [{ url: "urn:opencare:extension/priced", valueBoolean: true }],
    },
    {
      sequence: 2,
      productOrService: {
        coding: [{ system: "http://www.ama-assn.org/go/cpt", code: "93005", display: "Electrocardiogram" }],
        text: "Electrocardiogram",
      },
      quantity: { value: 1 },
      unitPrice: { value: 45, currency: "USD" },
      net: { value: 45, currency: "USD" },
      extension: [{ url: "urn:opencare:extension/priced", valueBoolean: true }],
    },
    {
      sequence: 3,
      productOrService: {
        coding: [{ system: "http://loinc.org", code: "9999-9", display: "Synthetic unscheduled test" }],
        text: "Synthetic unscheduled test",
      },
      quantity: { value: 1 },
      unitPrice: { value: 0, currency: "USD" },
      net: { value: 0, currency: "USD" },
      extension: [{ url: "urn:opencare:extension/priced", valueBoolean: false }],
    },
  ],
  total: { value: 100, currency: "USD" },
  appealCount: 0,
  statusHistory: [
    { from: null, to: "draft", at: "2026-01-10T10:05:00Z", by: "dev-admin", note: "Claim generated" },
    { from: "draft", to: "ready", at: "2026-01-10T10:06:00Z", by: "dev-admin", note: null },
    { from: "ready", to: "submitted", at: "2026-01-10T10:07:00Z", by: "dev-admin", note: null },
  ],
};

export const fixtureStock: StockItem[] = [
  {
    code: "1049221", display: "Lisinopril 10 MG Oral Tablet (synthetic)",
    system: "http://www.nlm.nih.gov/research/umls/rxnorm",
    active: true, stock_tracked: true, quantity: 120,
  },
  {
    code: "245314", display: "Albuterol inhaler (synthetic)",
    system: "http://www.nlm.nih.gov/research/umls/rxnorm",
    active: true, stock_tracked: false, quantity: null,
  },
];

export const fixtureDispenseQueue: DispenseQueueItem[] = [
  {
    request: {
      resourceType: "MedicationRequest",
      id: "med-1",
      status: "active",
      intent: "order",
      medicationCodeableConcept: {
        coding: [{ system: "http://www.nlm.nih.gov/research/umls/rxnorm", code: "1049221", display: "Lisinopril 10 MG Oral Tablet" }],
        text: "Lisinopril 10 MG Oral Tablet",
      },
      subject: { reference: "Patient/pat-1" },
    },
    medication_code: "1049221",
    in_formulary: true,
    stock_tracked: true,
    stock_quantity: 120,
  },
];

export const fixtureFormulary: FormularyResponse = {
  formulary: {
    name: "Synthetic formulary",
    system: "http://www.nlm.nih.gov/research/umls/rxnorm",
    concepts: [
      { code: "1049221", display: "Lisinopril 10 MG Oral Tablet", stockTracked: true, initialStock: 120 },
    ],
  },
  version: 1,
};

export const fixtureWarehouseStatus: WarehouseStatus = {
  outbox: { pending: 3, processed: 80, dead_letter: 1 },
  dead_letters: [
    {
      event_id: 91,
      resource: "Observation/obs-poison",
      version: 1,
      attempts: 1,
      error: "ValueError: Observation has no Patient subject reference",
    },
  ],
  warehouse: {
    person: 6,
    visit_occurrence: 12,
    condition_occurrence: 10,
    measurement: 32,
    observation: 0,
    drug_exposure: 9,
    procedure_occurrence: 5,
  },
  mapping_coverage: {
    tables: {
      person: { rows: 6, mapped: 6, unmapped: 0, coverage_pct: 100.0 },
    },
    total_rows: 74,
    mapped_rows: 73,
    unmapped_rows: 1,
    coverage_pct: 98.6,
  },
};

// ---- Phase 4B: specialty boards ----

export const fixtureEdBoard: EdBoard = {
  generated_at: "2026-10-15T17:00:00Z",
  counts: { waiting: 1, triage: 1, "in-treatment": 0, disposition: 1, total: 3 },
  groups: {
    waiting: [
      {
        encounter: {
          resourceType: "Encounter",
          id: "enc-ed-1",
          status: "waiting",
          class: { code: "EMER", display: "emergency" },
          subject: { reference: "Patient/pat-1" },
          period: { start: "2026-10-15T15:35:00Z" },
        },
        patient_ref: "Patient/pat-1",
        arrived_at: "2026-10-15T15:35:00Z",
        wait_minutes: 85,
        acuity: null,
        chief_complaint: "Chest pain",
        state: "waiting",
      },
    ],
    triage: [
      {
        encounter: {
          resourceType: "Encounter",
          id: "enc-ed-2",
          status: "triage",
          class: { code: "EMER", display: "emergency" },
          subject: { reference: "Patient/pat-2" },
          period: { start: "2026-10-15T16:10:00Z" },
        },
        patient_ref: "Patient/pat-2",
        arrived_at: "2026-10-15T16:10:00Z",
        wait_minutes: 50,
        acuity: 2,
        chief_complaint: "Fractured wrist",
        state: "triage",
      },
    ],
    "in-treatment": [],
    disposition: [
      {
        encounter: {
          resourceType: "Encounter",
          id: "enc-ed-3",
          status: "disposition",
          class: { code: "EMER", display: "emergency" },
          subject: { reference: "Patient/pat-3" },
          period: { start: "2026-10-15T14:00:00Z" },
        },
        patient_ref: "Patient/pat-3",
        arrived_at: "2026-10-15T14:00:00Z",
        wait_minutes: 180,
        acuity: 4,
        chief_complaint: "Migraine",
        state: "disposition",
      },
    ],
  },
};

export const fixtureModalities: ModalitiesResponse = {
  modalities: [
    { code: "CT", display: "Computed Tomography", active: true },
    { code: "MR", display: "Magnetic Resonance Imaging", active: true },
    { code: "US", display: "Ultrasound", active: true },
  ],
  version: 1,
};

export const fixtureRadiologyItem: RadiologyStudyItem = {
  study: {
    resourceType: "ImagingStudy",
    id: "study-1",
    status: "available",
    identifier: [{ system: "urn:dicom:uid", value: "2.25.123456789" }],
    modality: [{ system: "http://dicom.nema.org/resources/ontology/DCM", code: "CT", display: "Computed Tomography" }],
    subject: { reference: "Patient/pat-1" },
    started: "2026-10-15T16:00:00Z",
    description: "Chest X-ray, 2 views",
    numberOfSeries: 0,
    numberOfInstances: 0,
  },
  study_uid: "2.25.123456789",
  viewer_url: "https://viewer.ohif.org/viewer?StudyInstanceUIDs=2.25.123456789",
  report: null,
  has_images: false,
};

export const fixtureRadiologyDetail: RadiologyStudyDetail = {
  ...fixtureRadiologyItem,
  report: { id: "rep-img-1", status: "preliminary" },
  reports: [
    {
      resourceType: "DiagnosticReport",
      id: "rep-img-1",
      status: "preliminary",
      code: { text: "Chest X-ray, 2 views" },
      subject: { reference: "Patient/pat-1" },
      conclusion: "No acute findings. (synthetic)",
    },
  ],
};

export const fixtureRooms: RoomsResponse = {
  rooms: [
    { code: "OR-1", display: "Operating Room 1", active: true },
    { code: "OR-2", display: "Operating Room 2", active: true },
  ],
  version: 1,
};

export const fixtureSurgeryBoard: SurgeryBoard = {
  date: "2026-10-20",
  total: 1,
  rooms: [
    {
      code: "OR-1",
      display: "Operating Room 1",
      cases: [
        {
          case: {
            resourceType: "Procedure",
            id: "case-1",
            status: "scheduled",
            code: {
              coding: [{ system: "http://www.ama-assn.org/go/cpt", code: "27447", display: "Total knee arthroplasty" }],
              text: "Total knee arthroplasty",
            },
            subject: { reference: "Patient/pat-1" },
          },
          appointment: {
            resourceType: "Appointment",
            id: "appt-case-1",
            status: "booked",
            start: "2026-10-20T08:00:00Z",
            end: "2026-10-20T10:00:00Z",
          },
          room: "OR-1",
          state: "scheduled",
          start: "2026-10-20T08:00:00Z",
          end: "2026-10-20T10:00:00Z",
          patient_ref: "Patient/pat-1",
        },
      ],
    },
    { code: "OR-2", display: "Operating Room 2", cases: [] },
  ],
};
