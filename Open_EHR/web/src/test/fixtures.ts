// Test fixtures only — obviously synthetic FHIR data used by unit/component
// tests. Nothing here is real or real-looking personal data, and nothing in
// shipped code paths imports this file.

import type {
  AdmissionDetail,
  AdtEventsResponse,
  Appointment,
  CarePlanResponse,
  Census,
  Claim,
  Condition,
  DiagnosticReport,
  DispenseQueueItem,
  DispositionsResponse,
  SurgeryCaseRecord,
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
  CohortExport,
  CohortSnapshotsResponse,
  MessageThreadDetail,
  MessageThreadSummary,
  QualityMeasureEntry,
  QualityMeasureResult,
  TelehealthWorkspace,
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
  notification: {
    state: "pending",
    notified_to: null,
    read_back: null,
    notified_by: null,
    notified_at: null,
    acknowledged_by: null,
    acknowledged_at: null,
  },
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

// ---- Phase 9: quality measures, research workbench, engagement ----

export const fixtureQualityMeasures: QualityMeasureEntry[] = [
  {
    key: "hba1c-control",
    name: "Diabetes HbA1c control",
    title: "Diabetes — HbA1c control (< 8.0)",
    description: "Synthetic demo measure.",
    definition_version: 1,
    small_cell_threshold: 5,
    definition: {},
  },
  {
    key: "asthma-review",
    name: "Asthma review",
    title: "Asthma — annual review",
    description: "Small synthetic cohort — counts suppress.",
    definition_version: 1,
    small_cell_threshold: 5,
    definition: {},
  },
];

export const fixtureMeasureResultVisible: QualityMeasureResult = {
  measure: "hba1c-control",
  name: "Diabetes HbA1c control",
  title: "Diabetes — HbA1c control (< 8.0)",
  description: "Synthetic demo measure.",
  denominator: 10,
  numerator: 6,
  rate: 60.0,
  suppressed: false,
  small_cell_threshold: 5,
  as_of: "2026-01-10",
};

export const fixtureMeasureResultSuppressed: QualityMeasureResult = {
  measure: "asthma-review",
  name: "Asthma review",
  title: "Asthma — annual review",
  description: "Small synthetic cohort — counts suppress.",
  denominator: "<5",
  numerator: "<5",
  rate: null,
  suppressed: true,
  small_cell_threshold: 5,
  as_of: "2026-01-10",
};

export const fixtureCohortSnapshots: CohortSnapshotsResponse = {
  cohort: "diabetes",
  snapshots: [
    {
      id: 1,
      cohort_key: "diabetes",
      member_count: 10,
      definition_version: 1,
      taken_by: "dev-admin",
      taken_at: "2026-01-01T09:00:00Z",
    },
    {
      id: 2,
      cohort_key: "diabetes",
      member_count: 12,
      definition_version: 1,
      taken_by: "dev-admin",
      taken_at: "2026-02-01T09:00:00Z",
    },
  ],
  total: 2,
};

export const fixtureCohortExport: CohortExport = {
  registry: "diabetes",
  definition: fixtureRegistryList[0].definition,
  definition_version: 1,
  summary: fixtureDiabetesSummary,
  exported_by: "dev-configurator",
  notice:
    "De-identified aggregate export: counts and distributions only, " +
    "small-cell suppressed. No patient-level rows exist in this export.",
};

export const fixtureMessageThreads: MessageThreadSummary[] = [
  {
    thread_id: "thread-1",
    patient: "Patient/pat-1",
    topic: null,
    message_count: 2,
    unread_count: 1,
    last_message_at: "2026-01-05T10:00:00Z",
    last_text: "Your result is ready",
    last_sender_role: "clinician",
  },
];

export const fixtureThreadDetail: MessageThreadDetail = {
  thread_id: "thread-1",
  patient: "Patient/pat-1",
  total: 2,
  messages: [
    {
      id: "msg-1",
      resourceType: "Communication",
      status: "completed",
      sent: "2026-01-05T09:00:00Z",
      threadId: "thread-1",
      senderRole: "patient",
      text: "Question about my lab",
      read: true,
      mine: false,
    },
    {
      id: "msg-2",
      resourceType: "Communication",
      status: "completed",
      sent: "2026-01-05T10:00:00Z",
      threadId: "thread-1",
      senderRole: "clinician",
      text: "Your result is ready",
      read: false,
      mine: false,
    },
  ],
};

export const fixtureTelehealthWorkspace: TelehealthWorkspace = {
  appointment: {
    resourceType: "Appointment",
    id: "appt-tele-1",
    status: "booked",
    appointmentType: {
      coding: [{ system: "urn:opencare:slot-type", code: "telehealth" }],
    },
    start: "2026-11-02T15:00:00Z",
  } as TelehealthWorkspace["appointment"],
  patient: "Patient/pat-1",
  provider_name: "Synthetic Video (configured provider)",
  join_url: "https://video.example.invalid/opencare/appt-tele-1",
  disclaimer:
    "OpenCare does not provide video. This visit is recorded in OpenCare; " +
    "the video call itself runs on your organisation's configured provider.",
  intake_form: "telehealth-intake",
  intake_form_definition: {
    resourceType: "Questionnaire",
    title: "Telehealth pre-visit intake",
    item: [
      { linkId: "chief-concern", text: "Chief concern", type: "text", required: true },
      { linkId: "technology-check", text: "Camera and microphone working", type: "boolean", required: true },
    ],
  },
  intake_submitted: false,
  intake_response_id: null,
  note_type: "telehealth",
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
          timeline: [
            { event: "scheduled", state: "scheduled", at: "2026-10-19T09:00:00Z", by: "dev-clinician" },
          ],
        },
      ],
    },
    { code: "OR-2", display: "Operating Room 2", cases: [] },
  ],
};

// ---- Phase 5 fixtures: orders, notes, forms, flowsheet, MAR ----

import type {
  ClinicalNote,
  FlowsheetView,
  FormDefinition,
  MarDueResponse,
  NoteTypesResponse,
  OrderSetsResponse,
  OrderTypesResponse,
  PlacedOrder,
} from "../api/types";

export const fixtureOrderTypes: OrderTypesResponse = {
  order_types: {
    name: "Synthetic order types",
    concepts: [
      { code: "medication", display: "Medication", resourceType: "MedicationRequest", requiredFields: ["code"] },
      { code: "procedure", display: "Procedure", resourceType: "ServiceRequest", requiredFields: ["code"] },
      { code: "consult", display: "Consult", resourceType: "ServiceRequest", requiredFields: ["code", "performerRole"] },
      { code: "imaging", display: "Imaging", resourceType: "ServiceRequest", requiredFields: ["code", "modality"] },
    ],
  },
};

export const fixtureOrderSets: OrderSetsResponse = {
  total: 1,
  sets: [
    {
      key: "admission-general",
      name: "Admission — general",
      orders: [
        { type: "procedure", code: "93005", display: "ECG 12-lead" },
        { type: "nursing", code: "vital-signs", display: "Vital signs" },
      ],
    },
  ],
};

export const fixtureOrders: PlacedOrder[] = [
  {
    resourceType: "ServiceRequest",
    id: "ord-ecg",
    status: "draft",
    intent: "order",
    code: { coding: [{ code: "93005", display: "ECG 12-lead" }], text: "ECG 12-lead" },
    subject: { reference: "Patient/pat-1" },
  },
  {
    resourceType: "MedicationRequest",
    id: "ord-lisinopril",
    status: "signed",
    intent: "order",
    medicationCodeableConcept: {
      coding: [{ code: "1049221", display: "Lisinopril" }],
      text: "Lisinopril 10 mg",
    },
    subject: { reference: "Patient/pat-1" },
  },
];

export const fixtureNoteTypes: NoteTypesResponse = {
  note_types: {
    name: "Synthetic note types",
    concepts: [
      { code: "progress", display: "Progress note", templateForm: "note-progress" },
      { code: "discharge", display: "Discharge summary (draft)", templateForm: "note-discharge" },
    ],
  },
};

export const fixtureProgressForm: FormDefinition = {
  resourceType: "Questionnaire",
  title: "Progress note",
  item: [
    { linkId: "subjective", text: "Subjective", type: "text" },
    { linkId: "assessment", text: "Assessment", type: "string", required: true },
    { linkId: "plan", text: "Plan", type: "string", required: true },
  ],
};

export const fixtureVitalsForm: FormDefinition = {
  resourceType: "Questionnaire",
  title: "Flowsheet — vitals",
  item: [
    { linkId: "systolic", text: "Systolic BP", type: "integer", required: true },
    { linkId: "heart-rate", text: "Heart rate", type: "integer", required: true },
    { linkId: "spo2", text: "SpO2", type: "integer", required: true },
  ],
};

export const fixtureNotes: ClinicalNote[] = [
  {
    resourceType: "DocumentReference",
    id: "note-1",
    status: "current",
    docStatus: "preliminary",
    type: { coding: [{ code: "progress", display: "Progress note" }], text: "Progress note" },
    subject: { reference: "Patient/pat-1" },
    date: "2026-10-16T09:00:00Z",
    noteType: "progress",
    noteContent: {
      templateForm: "note-progress",
      questionnaireResponseId: "qr-1",
      sections: [],
      text: null,
    },
  },
];

export const fixtureFlowsheetView: FlowsheetView = {
  patient: "Patient/pat-1",
  total: 1,
  form: {
    title: "Flowsheet — vitals",
    items: [
      { linkId: "systolic", text: "Systolic BP", type: "integer", required: true },
      { linkId: "heart-rate", text: "Heart rate", type: "integer", required: true },
      { linkId: "spo2", text: "SpO2", type: "integer", required: true },
    ],
  },
  entries: [
    {
      id: "qr-vitals-1",
      authored: "2026-10-16T09:05:00Z",
      answers: {
        systolic: { value: 121, unit: "mmHg" },
        "heart-rate": { value: 71, unit: "/min" },
        spo2: { value: 99, unit: "%" },
      },
    },
  ],
};

export const fixtureMar: MarDueResponse = {
  patient: "Patient/pat-1",
  from: "2026-10-15T09:00:00Z",
  to: "2026-10-17T09:00:00Z",
  administrations: [],
  summary: { given: 1, due: 1, missed: 1, scheduled: 1 },
  doses: [
    {
      order_id: "ord-lisinopril",
      medication_code: "1049221",
      medication_display: "Lisinopril 10 mg",
      due_at: "2026-10-16T08:00:00Z",
      window_start: "2026-10-16T07:00:00Z",
      window_end: "2026-10-16T09:00:00Z",
      administration_id: "admin-1",
      status: "given",
    },
    {
      order_id: "ord-lisinopril",
      medication_code: "1049221",
      medication_display: "Lisinopril 10 mg",
      due_at: "2026-10-15T20:00:00Z",
      window_start: "2026-10-15T19:00:00Z",
      window_end: "2026-10-15T21:00:00Z",
      administration_id: null,
      status: "missed",
    },
  ],
};

// ---- Phase 6: inpatient operations ----

export const fixtureCensus: Census = {
  generated_at: "2026-10-20T12:00:00Z",
  units: [
    {
      code: "unit-medsurg",
      display: "Medical-Surgical Unit",
      beds_total: 3,
      available: 1,
      occupied: 1,
      cleaning: 1,
      blocked: 0,
      occupancy_pct: 33.3,
      beds: [
        {
          code: "MS-101A", display: "Med-Surg Room 101 Bed A", room: "101",
          bed: "A", unit: "unit-medsurg", status: "occupied",
          patient_ref: "Patient/pat-1", encounter_id: "enc-1",
          admitted_at: "2026-10-18T12:00:00Z", los_days: 2.0,
        },
        {
          code: "MS-101B", display: "Med-Surg Room 101 Bed B", room: "101",
          bed: "B", unit: "unit-medsurg", status: "cleaning",
          patient_ref: null, encounter_id: null,
          admitted_at: null, los_days: null,
        },
        {
          code: "MS-102A", display: "Med-Surg Room 102 Bed A", room: "102",
          bed: "A", unit: "unit-medsurg", status: "available",
          patient_ref: null, encounter_id: null,
          admitted_at: null, los_days: null,
        },
      ],
    },
    {
      code: "unit-icu",
      display: "Intensive Care Unit",
      beds_total: 1,
      available: 0,
      occupied: 1,
      cleaning: 0,
      blocked: 0,
      occupancy_pct: 100.0,
      beds: [
        {
          code: "ICU-201", display: "ICU Room 201", room: "201",
          bed: "1", unit: "unit-icu", status: "occupied",
          patient_ref: "Patient/pat-2", encounter_id: "enc-2",
          admitted_at: "2026-10-19T12:00:00Z", los_days: 1.0,
        },
      ],
    },
  ],
  totals: {
    beds_total: 4, available: 1, occupied: 2, cleaning: 1, blocked: 0,
    occupancy_pct: 50.0,
  },
  expected_discharges_today: {
    date: "2026-10-20",
    total: 1,
    discharges: [
      {
        encounter_id: "enc-1", patient_ref: "Patient/pat-1",
        bed_code: "MS-101A", target_date: "2026-10-20",
        disposition: "home", barriers: null,
        admitted_at: "2026-10-18T12:00:00Z", los_days: 2.0,
      },
    ],
  },
};

export const fixtureDischargeChecklistForm: FormDefinition = {
  resourceType: "Questionnaire",
  title: "Discharge checklist",
  item: [
    {
      linkId: "disposition", text: "Discharge disposition", type: "choice",
      required: true,
      answerOption: [
        { valueCoding: { code: "home", display: "Home" } },
        { valueCoding: { code: "snf", display: "Skilled nursing facility" } },
      ],
    },
    { linkId: "target-discharge-date", text: "Target discharge date", type: "date", required: true },
    { linkId: "meds-reconciled", text: "Medications reconciled", type: "boolean", required: true },
    { linkId: "barriers", text: "Barriers to discharge", type: "text" },
  ],
};

export const fixtureDispositions: DispositionsResponse = {
  dispositions: [
    { code: "home", display: "Home", active: true },
    { code: "snf", display: "Skilled nursing facility", active: true },
  ],
  version: 1,
};

export const fixtureAnaesthesiaForm: FormDefinition = {
  resourceType: "Questionnaire",
  title: "Anaesthesia record",
  item: [
    {
      linkId: "anaesthesia-type", text: "Anaesthesia type", type: "choice",
      required: true,
      answerOption: [
        { valueCoding: { code: "general", display: "General" } },
        { valueCoding: { code: "regional", display: "Regional" } },
      ],
    },
    {
      linkId: "airway", text: "Airway", type: "choice", required: true,
      answerOption: [
        { valueCoding: { code: "ett", display: "Endotracheal tube" } },
        { valueCoding: { code: "lma", display: "Laryngeal mask airway" } },
      ],
    },
    { linkId: "agents", text: "Agents administered", type: "text", required: true },
  ],
};

export const fixtureSurgeryCaseRecord: SurgeryCaseRecord = {
  case: fixtureSurgeryBoard.rooms[0].cases[0],
  preop_checklist: {
    meta: { responseId: "qr-preop-1", formVersion: 1, completedAt: "2026-10-20T07:30:00Z" },
    response: null,
  },
  anaesthesia: null,
  anaesthesia_vitals: {
    form_key: "anaesthesia-vitals",
    form: {
      title: "Anaesthesia vitals",
      items: [
        { linkId: "systolic", text: "Systolic BP (mmHg)", type: "integer", required: true },
        { linkId: "heart-rate", text: "Heart rate (bpm)", type: "integer", required: true },
        { linkId: "spo2", text: "SpO2 (%)", type: "integer", required: true },
      ],
    },
    entries: [
      {
        id: "qr-vitals-1",
        authored: "2026-10-20T08:05:00Z",
        answers: {
          systolic: { value: 118 },
          "heart-rate": { value: 72 },
          spo2: { value: 99 },
        },
      },
    ],
    total: 1,
    patient: "Patient/pat-1",
  },
};

export const fixtureAdmission: AdmissionDetail = {
  encounter: {
    resourceType: "Encounter",
    id: "enc-1",
    status: "in-progress",
    class: { code: "IMP", display: "inpatient encounter" },
    subject: { reference: "Patient/pat-1" },
    period: { start: "2026-10-18T12:00:00Z" },
  },
  bed_code: "MS-101A",
  bed: null,
  admitted_at: "2026-10-18T12:00:00Z",
  los_days: 2.0,
  discharge_plan: {
    targetDate: "2026-10-20",
    disposition: "home",
    updatedBy: "dev-clinician",
    updatedAt: "2026-10-19T09:00:00Z",
  },
};

export const fixtureCarePlan: CarePlanResponse = {
  tasks: [
    {
      resourceType: "Task",
      id: "task-1",
      status: "requested",
      intent: "plan",
      description: "Arrange SNF placement",
      for: { reference: "Patient/pat-1" },
      encounter: { reference: "Encounter/enc-1" },
    },
  ],
  total: 1,
};

export const fixtureAdtEvents: AdtEventsResponse = {
  events: [
    {
      id: 1, event_type: "A01", event_display: "Admit / visit notification",
      encounter_id: "enc-1", patient_ref: "Patient/pat-1",
      from_location: null, to_location: "MS-101A",
      actor: "dev-clinician", detail: {}, at: "2026-10-18T12:00:00Z",
    },
  ],
  total: 1,
};
