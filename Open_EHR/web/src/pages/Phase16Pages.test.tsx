import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import { AnalyticsPage } from "./AnalyticsPage";
import { DocumentsPage } from "./DocumentsPage";
import { EmpiTab } from "./EmpiTab";
import { FederationPage } from "./FederationPage";
import { ImagingAssistPanel } from "./ImagingAssistPanel";
import { MaintenanceTab } from "./MaintenanceTab";
import { ResearchPage } from "./ResearchPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 16 wave-2 pages (WS-K2): federation, research (+ e-signature +
// genomics), analytics, document intake, and the EMPI / maintenance /
// imaging-assist tabs — rendered through stub clients whose `request`
// routes by path, the generic-client pattern of the later page suites.

type Handler = (path: string, init?: RequestInit) => unknown;

function stubClient(handler: Handler, extra: Record<string, unknown> = {}): ApiClient {
  return {
    request: vi.fn(async (path: string, init?: RequestInit) => handler(path, init)),
    ...extra,
  } as unknown as ApiClient;
}

function bodyOf(init?: RequestInit): Record<string, unknown> {
  return init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {};
}

// ---------------------------------------------------------------------
// Federation
// ---------------------------------------------------------------------

const peersPayload = {
  node: { name: "local-opencare", baseUrl: "http://localhost:8010" },
  peers: [{ name: "peer-a", baseUrl: "http://peer-a:8010", enabled: true }],
  config: { kind: "integration", key: "federation", version: 1 },
};

describe("FederationPage", () => {
  it("renders peers from published config and labels a references answer as data behind glass", async () => {
    const client = stubClient((path, init) => {
      if (path === "/federation/peers") return peersPayload;
      if (path === "/federation/query") {
        expect(bodyOf(init)).toMatchObject({ peer: "peer-a", mode: "references" });
        return {
          mode: "references",
          peer: "peer-a",
          patient_ref: "Patient/p1",
          counts: { Observation: 3 },
          references: [{ reference: "Observation/o1", resourceType: "Observation" }],
          total: 1,
        };
      }
      if (path.startsWith("/federation/reconciliation/")) return { patient_ref: "Patient/p1", items: [], staged_count: 0 };
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<FederationPage client={client} role="clinician" />);
    // Coordinator adjustment (Phase 16 gate): "peer-a" renders both as
    // the peer <option> and in the results area — query the option by
    // role (the bare text query is ambiguous and race-dependent).
    expect(await screen.findByRole("option", { name: "peer-a" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Patient ID at the peer/), { target: { value: "p1" } });
    fireEvent.click(screen.getByText("Query peer"));
    expect(await screen.findByText(/data behind glass/)).toBeTruthy();
    expect(screen.getByText("Observation/o1")).toBeTruthy();
    expect(screen.getByText(/consent permit — peer answered/)).toBeTruthy();
  });

  it("shows the peer's consent refusal instead of data", async () => {
    const client = stubClient((path) => {
      if (path === "/federation/peers") return peersPayload;
      if (path === "/federation/query") {
        throw new ApiError(403, "no Consent permits peer 'local-opencare' for purpose 'treatment'");
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<FederationPage client={client} role="clinician" />);
    expect(await screen.findByRole("option", { name: "peer-a" })).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/Patient ID at the peer/), { target: { value: "p1" } });
    fireEvent.click(screen.getByText("Query peer"));
    expect(await screen.findByText(/Consent refused by the peer/)).toBeTruthy();
    expect(screen.getByText(/no Consent permits peer/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Research
// ---------------------------------------------------------------------

const studyPayload = {
  id: 1,
  protocol_id: "PROTO-1",
  title: "Synthetic diabetes study",
  phase: "II",
  status: "draft",
  pi_username: "dev-clinician",
  approved: false,
  approval_signature_id: null,
  snapshot_hash: "hash-from-study",
  procedures: [
    {
      id: 5,
      study_id: 1,
      procedure_code: "LAB-HBA1C",
      description: "HbA1c panel",
      determination: null,
      snapshot_hash: "proc-hash",
    },
  ],
};

describe("ResearchPage", () => {
  it("names the undetermined procedures blocking activation in plain words", async () => {
    const client = stubClient((path) => {
      if (path === "/research/studies") return { items: [studyPayload], total: 1 };
      if (path === "/research/studies/1") return studyPayload;
      if (path === "/research/studies/1/coverage") {
        return {
          study_id: 1,
          items: studyPayload.procedures,
          undetermined: [{ procedure_id: 5, procedure_code: "LAB-HBA1C" }],
          total: 1,
          coverage_complete: false,
        };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<ResearchPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByText(/PROTO-1 — Synthetic diabetes study/));
    expect(await screen.findByText(/Activation is blocked/)).toBeTruthy();
    expect(screen.getAllByText(/LAB-HBA1C/).length).toBeGreaterThan(0);
    expect(screen.getByText(/undetermined — blocks activation/)).toBeTruthy();
  });

  it("signs the shown snapshot hash and approves with the returned signature id", async () => {
    const calls: Array<{ path: string; body: Record<string, unknown> }> = [];
    const client = stubClient((path, init) => {
      if (path === "/research/studies") return { items: [studyPayload], total: 1 };
      if (path === "/research/studies/1") return studyPayload;
      if (path === "/research/studies/1/coverage") {
        return { study_id: 1, items: studyPayload.procedures, undetermined: [], total: 1, coverage_complete: true };
      }
      if (path === "/esign/snapshot/ResearchStudy/1") {
        return { record_type: "ResearchStudy", record_id: "1", snapshot: { title: "x" }, snapshot_hash: "snap-abc" };
      }
      if (path === "/esign/sign") {
        calls.push({ path, body: bodyOf(init) });
        return { id: 9 };
      }
      if (path === "/research/studies/1/approve") {
        calls.push({ path, body: bodyOf(init) });
        return { ...studyPayload, approved: true, approval_signature_id: 9 };
      }
      if (path === "/esign/manifest/ResearchStudy/1") {
        return { manifestation: { signer: "dev-clinician", meaning: "I approve this protocol", timestamp: "2026-10-19T00:00:00Z", binding: "bind-1" } };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<ResearchPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByText(/PROTO-1 — Synthetic diabetes study/));
    fireEvent.click(await screen.findByText("Approvals"));
    expect(await screen.findByText("snap-abc")).toBeTruthy();
    fireEvent.click(screen.getByText("Sign & approve protocol"));
    await waitFor(() => expect(calls.length).toBe(2));
    expect(calls[0]).toEqual({
      path: "/esign/sign",
      body: {
        recordType: "ResearchStudy",
        recordId: "1",
        meaning: "I approve this protocol",
        snapshotHash: "snap-abc",
      },
    });
    expect(calls[1]).toEqual({ path: "/research/studies/1/approve", body: { signatureId: 9 } });
  });

  it("surfaces a changed-record 409 from signing inline", async () => {
    const client = stubClient((path) => {
      if (path === "/research/studies") return { items: [studyPayload], total: 1 };
      if (path === "/research/studies/1") return studyPayload;
      if (path === "/research/studies/1/coverage") {
        return { study_id: 1, items: studyPayload.procedures, undetermined: [], total: 1, coverage_complete: true };
      }
      if (path === "/esign/snapshot/ResearchStudy/1") {
        return { record_type: "ResearchStudy", record_id: "1", snapshot: {}, snapshot_hash: "snap-abc" };
      }
      if (path === "/esign/sign") {
        throw new ApiError(409, "Snapshot mismatch: the record changed since the snapshot was taken");
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<ResearchPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByText(/PROTO-1 — Synthetic diabetes study/));
    fireEvent.click(await screen.findByText("Approvals"));
    fireEvent.click(await screen.findByText("Sign & approve protocol"));
    expect(await screen.findByText(/Snapshot mismatch: the record changed/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Analytics
// ---------------------------------------------------------------------

describe("AnalyticsPage", () => {
  it("shows the honest empty-vocabulary state and runs a published view", async () => {
    const client = stubClient((path, init) => {
      if (path === "/analytics/datasets") {
        return {
          items: [
            {
              dataset: "condition_occurrence",
              table: "condition_occurrence",
              columns: [{ name: "condition_concept_id", type: "INTEGER" }],
            },
          ],
          total: 1,
        };
      }
      if (path === "/analytics/vocabulary/coverage") {
        return {
          vocab_concept_total: 0,
          vocab_relationship_total: 0,
          by_vocabulary: [],
          by_domain: [],
          curated_concept_map_count: 12,
          concept_map_baseline: { table: "concept_map", count: 12, note: "curated subset" },
        };
      }
      if (path === "/studio/config/analytics_view/effective") {
        return [
          {
            key: "condition-counts-by-concept",
            version: 1,
            status: "published",
            payload: { title: "Condition counts", dataset: "condition_occurrence" },
          },
        ];
      }
      if (path === "/analytics/query") {
        expect(bodyOf(init)).toEqual({ key: "condition-counts-by-concept" });
        return {
          definition: {},
          source: "saved",
          key: "condition-counts-by-concept",
          dataset: "condition_occurrence",
          rows: [{ condition_concept_id: 38341003, value: 3 }],
          row_count: 1,
        };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<AnalyticsPage client={client} role="clinician" />);
    expect(await screen.findByText(/no ATHENA bundle loaded/)).toBeTruthy();
    expect(screen.getByText("Condition counts")).toBeTruthy();
    fireEvent.click(screen.getByText("Run view"));
    expect(await screen.findByText("38341003")).toBeTruthy();
    expect(screen.getByText("1 row(s)")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Documents
// ---------------------------------------------------------------------

const intakeDoc = {
  id: "intake-1",
  filename: "lab-result.txt",
  mediaType: "text/plain",
  status: "classified",
  extraction_error: null,
  classification_note: null,
  suggestedPatientRef: "Patient/p1",
  suggestedType: "lab-report",
  classifierRunId: "run-7",
  confirmedPatientRef: null,
  filedDocumentId: null,
  filedBy: null,
};

describe("DocumentsPage", () => {
  it("shows the suggestion as a suggestion and blocks a patient override without a reason", async () => {
    const confirmCalls: Array<Record<string, unknown>> = [];
    const client = stubClient((path, init) => {
      if (path === "/intake/documents") return { items: [intakeDoc], total: 1 };
      if (path === "/studio/config/terminology/effective/intake-doc-types") {
        return {
          payload: {
            concepts: [
              { code: "lab-report", display: "Lab report", active: true },
              { code: "other", display: "Other", active: true },
            ],
          },
        };
      }
      if (path === "/intake/documents/intake-1/confirm") {
        confirmCalls.push(bodyOf(init));
        return { ...intakeDoc, status: "filed", filedDocumentId: "doc-9", confirmedPatientRef: "Patient/p2" };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<DocumentsPage client={client} role="clinician" />);
    expect(await screen.findByText(/Classifier suggestion \(not filed\)/)).toBeTruthy();
    const patientInput = screen.getByLabelText(/Patient reference/);
    fireEvent.change(patientInput, { target: { value: "Patient/p2" } });
    fireEvent.click(screen.getByText("Confirm & file"));
    expect(await screen.findByText(/A reason is required to file against a different patient/)).toBeTruthy();
    expect(confirmCalls.length).toBe(0);
    fireEvent.change(screen.getByLabelText(/Reason \(required when the patient differs/), {
      target: { value: "Sender confirmed the correct patient by phone" },
    });
    fireEvent.click(screen.getByText("Confirm & file"));
    await waitFor(() => expect(confirmCalls.length).toBe(1));
    expect(confirmCalls[0]).toMatchObject({
      patientRef: "Patient/p2",
      docType: "lab-report",
      reason: "Sender confirmed the correct patient by phone",
    });
  });

  it("shows the extraction failure reason on a parked document", async () => {
    const client = stubClient((path) => {
      if (path === "/intake/documents") {
        return {
          items: [
            {
              ...intakeDoc,
              id: "intake-2",
              filename: "scan.pdf",
              mediaType: "application/pdf",
              status: "extraction-failed",
              extraction_error: "pypdf could not read the file",
              suggestedPatientRef: null,
              suggestedType: null,
              classifierRunId: null,
            },
          ],
          total: 1,
        };
      }
      if (path === "/studio/config/terminology/effective/intake-doc-types") {
        return { payload: { concepts: [] } };
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<DocumentsPage client={client} role="clinician" />);
    expect(await screen.findByText(/Text extraction failed: pypdf could not read the file/)).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// EMPI tab
// ---------------------------------------------------------------------

describe("EmpiTab", () => {
  it("renders candidates and requires the confirm step before executing a merge", async () => {
    const executeCalls: Array<Record<string, unknown>> = [];
    const client = stubClient((path, init) => {
      if (path === "/empi/candidates") {
        return {
          items: [
            {
              id: 1,
              patient_a: "a1",
              patient_b: "b2",
              score: 95,
              status: "new",
              reasons: { origin: "dedup-scan" },
            },
          ],
          total: 1,
        };
      }
      if (path === "/empi/requests") {
        return {
          items: [
            {
              id: 7,
              source_patient_id: "a1",
              survivor_patient_id: "b2",
              status: "approved",
              proposed_by: "steward-1",
              approved_by: "steward-2",
              executed_by: null,
              note: null,
            },
          ],
          total: 1,
        };
      }
      if (path === "/empi/requests/7/execute") {
        executeCalls.push(bodyOf(init));
        return {};
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<EmpiTab client={client} role="clinician" />);
    expect(await screen.findByText(/match score 95/)).toBeTruthy();
    const execute = screen.getByText("Execute merge") as HTMLButtonElement;
    expect(execute.disabled).toBe(true);
    // Coordinator adjustment (Phase 16 gate): the label regex in the
    // delivered test carried a stray space ("Patient/ b2") that can
    // never match the rendered label "Patient/b2".
    fireEvent.click(screen.getByLabelText(/I confirm: merge Patient\/a1 into Patient\/b2/));
    expect(execute.disabled).toBe(false);
    fireEvent.click(execute);
    await waitFor(() => expect(executeCalls.length).toBe(1));
    expect(executeCalls[0]).toEqual({ confirm: true });
  });
});

// ---------------------------------------------------------------------
// Maintenance tab
// ---------------------------------------------------------------------

const pmOrder = {
  work_order_id: "WO-1",
  device_id: "pump-1",
  type: "preventive",
  priority: "routine",
  status: "opened",
  assignee: null,
  downtime: false,
  reason: "Scheduled preventive maintenance — 180-day interval",
  opened_by: "dev-clinician",
  opened_at: "2026-10-01T00:00:00+00:00",
  completed_at: null,
  completion_note: null,
  pm_due_at: "2026-10-10T00:00:00+00:00",
  overdue: true,
};

describe("MaintenanceTab", () => {
  it("renders the worklist, the PM due list and downtime state on the affected device", async () => {
    const client = stubClient(
      (path) => {
        if (path === "/maintenance/worklist") return { items: [pmOrder], total: 1, overdue: 1, downtime: 1 };
        if (path === "/maintenance/work-orders") return { items: [pmOrder], total: 1 };
        if (path === "/maintenance/config") {
          return {
            downtime_status: "out-of-service",
            work_types: ["corrective", "preventive"],
            priorities: ["stat", "urgent", "routine"],
            statuses: ["opened", "assigned", "in-progress", "completed", "closed"],
          };
        }
        throw new Error(`Unexpected request: ${path}`);
      },
      {
        devices: vi.fn(async () => ({
          items: [
            { device_key: "pump-1", name: "Infusion pump 1", status: "out-of-service", maintenance_due: null },
          ],
          total: 1,
        })),
      },
    );
    render(<MaintenanceTab client={client} role="clinician" />);
    expect(await screen.findByText("Infusion pump 1")).toBeTruthy();
    expect(screen.getByText(/down — out-of-service/)).toBeTruthy();
    expect(screen.getByText(/next PM due 2026-10-10/)).toBeTruthy();
    expect(screen.getAllByText("PM overdue").length).toBeGreaterThan(0);
    expect(screen.getByText("WO-1")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------
// Imaging assist panel
// ---------------------------------------------------------------------

const draftPayload = {
  id: 3,
  studyId: "study-1",
  patientRef: "Patient/p1",
  findingsDraft: "Opacity in the right lower lobe.",
  impressionDraft: "Likely pneumonia — draft impression.",
  protocolSuggestion: null,
  triageSuggestion: null,
  measurements: [],
  priorStudyId: "study-0",
  agentRunId: "run-1",
  status: "draft",
  reportId: null,
  createdBy: "dev-clinician",
};

describe("ImagingAssistPanel", () => {
  it("renders the draft labelled as a draft with prior comparison and no finalise control", async () => {
    const client = stubClient((path) => {
      if (path === "/imaging-assist/studies/study-1/drafts") return { items: [draftPayload], total: 1 };
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<ImagingAssistPanel client={client} role="clinician" studyId="study-1" />);
    expect(await screen.findByText("Opacity in the right lower lobe.")).toBeTruthy();
    expect(screen.getAllByText(/Draft — not a report/).length).toBeGreaterThan(0);
    expect(screen.getByText(/prior study study-0/)).toBeTruthy();
    expect(screen.getByText("Accept into preliminary report")).toBeTruthy();
    // Coordinator adjustment (Phase 16 gate): the panel's own hint
    // copy says "there is no finalise control here", so a bare text
    // query matches the sentence asserting the property. The control
    // check is the button-role query:
    expect(screen.queryByRole("button", { name: /finalise|finalize|verify|final/i })).toBeNull();
  });

  it("shows the honest off state when no vision model is configured", async () => {
    const client = stubClient((path, init) => {
      if (path === "/imaging-assist/studies/study-1/drafts") return { items: [], total: 0 };
      if (path === "/imaging-assist/studies/study-1/draft" && init?.method === "POST") {
        throw new ApiError(503, "No model endpoint is configured for the radiology-vision-assistant agent");
      }
      throw new Error(`Unexpected request: ${path}`);
    });
    render(<ImagingAssistPanel client={client} role="clinician" studyId="study-1" />);
    fireEvent.click(await screen.findByText("Draft with vision assist"));
    expect(await screen.findByText(/Vision assist is off/)).toBeTruthy();
    expect(screen.getByText(/No model endpoint is configured/)).toBeTruthy();
  });
});
