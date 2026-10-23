import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { RadiologyPage } from "./RadiologyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 15: the page talks to the department endpoints through
// web/src/api/radiology.ts, so the stub is keyed by request path.

const worklistItem = {
  study_id: "study-1",
  study_uid: "2.25.123456789",
  patient_ref: "Patient/pat-1",
  modality: "CT",
  description: "CT chest with contrast — rule out pulmonary embolism (synthetic demo)",
  priority: "STAT",
  tat_target_minutes: 60,
  state: "ordered",
  protocol_code: null,
  scheduled_at: null,
  station_ae: null,
  ordered_at: "2026-10-18T09:00:00Z",
  screening: {},
  report: { id: "rep-img-1", status: "preliminary" },
  state_history: [{ state: "ordered", at: "2026-10-18T09:00:00Z", by: "system" }],
};

const studyDetail = {
  study: {
    resourceType: "ImagingStudy",
    id: "study-1",
    status: "available",
    identifier: [{ system: "urn:dicom:uid", value: "2.25.123456789" }],
    modality: [{ system: "http://dicom.nema.org/resources/ontology/DCM", code: "CT", display: "Computed Tomography" }],
    subject: { reference: "Patient/pat-1" },
    started: "2026-10-18T09:05:00Z",
    description: "CT chest with contrast — rule out pulmonary embolism (synthetic demo)",
    numberOfSeries: 1,
    numberOfInstances: 1,
  },
  study_uid: "2.25.123456789",
  viewer_url: "https://viewer.ohif.org/viewer?StudyInstanceUIDs=2.25.123456789",
  report: { id: "rep-img-1", status: "preliminary" },
  has_images: true,
  reports: [
    {
      resourceType: "DiagnosticReport",
      id: "rep-img-1",
      status: "preliminary",
      code: { text: "CT chest with contrast" },
      subject: { reference: "Patient/pat-1" },
      conclusion: "No acute findings. (synthetic)",
    },
  ],
};

const criticalItem = {
  report_id: "rep-img-1",
  study_id: "study-1",
  patient_ref: "Patient/pat-1",
  ordering_clinician: "Dr Ordering",
  state: "notified",
  task_id: "task-1",
  flagged_by: "radiologist",
  flagged_at: "2026-10-18T08:00:00Z",
  sla_minutes: 30,
  overdue: true,
  minutes_overdue: 95,
  readback_by: null,
  readback_text: null,
  readback_at: null,
  closed_by: null,
  closed_at: null,
};

function stubClient(): ApiClient {
  const request = vi.fn(async (path: string, _init?: unknown) => {
    if (path.startsWith("/radiology/worklist")) return { items: [worklistItem], total: 1 };
    if (path === "/radiology/stats/tat") {
      return {
        generated_at: "2026-10-18T10:00:00Z", total: 1, breaches: 0, unreported_stat: 1,
        by_priority: { STAT: { target_minutes: 60, studies: 1, breaches: 0, total_minutes: 42, avg_minutes: 42 } },
      };
    }
    if (path === "/radiology/critical") return { items: [criticalItem], total: 1 };
    if (path === "/radiology/modalities") {
      return { modalities: [{ code: "CT", display: "Computed Tomography", active: true }], version: 1 };
    }
    if (path === "/radiology/studies/study-1") return studyDetail;
    if (path === "/radiology/dicomweb/studies/2.25.123456789/series") {
      return [{
        "0020000D": { vr: "UI", Value: ["2.25.123456789"] },
        "0020000E": { vr: "UI", Value: ["2.25.series-1"] },
        "00080060": { vr: "CS", Value: ["CT"] },
        "00201209": { vr: "IS", Value: [1] },
      }];
    }
    if (path === "/radiology/dicomweb/studies/2.25.123456789/series/2.25.series-1/instances") {
      return [{
        "0020000D": { vr: "UI", Value: ["2.25.123456789"] },
        "0020000E": { vr: "UI", Value: ["2.25.series-1"] },
        "00080018": { vr: "UI", Value: ["2.25.sop-1"] },
        "00200013": { vr: "IS", Value: [1] },
      }];
    }
    if (path === "/studio/config/terminology/effective/imaging-protocols") {
      return {
        payload: {
          concepts: [{
            code: "CT-CHEST-CONTRAST", display: "CT chest with IV contrast",
            active: true, modality: "CT", screeningForms: ["radiology-screening-contrast"],
          }],
        },
        version: 1,
      };
    }
    if (path === "/studio/config/terminology/effective/radiology-ops") {
      return { payload: { doseQuantities: { CT: [{ code: "CTDIvol", unit: "mGy" }, { code: "DLP", unit: "mGy.cm" }] } }, version: 1 };
    }
    if (path === "/radiology/patients/pat-1/dose-history") {
      return {
        patient_ref: "Patient/pat-1",
        items: [{
          id: 1, study_id: "study-1", modality: "CT", quantity: "CTDIvol",
          value: 8.5, unit: "mGy", recorded_by: "tech", recorded_at: "2026-10-18T09:30:00Z",
        }],
        total: 1,
        totals: [{ quantity: "CTDIvol", unit: "mGy", total: 8.5, count: 1 }],
      };
    }
    if (path === "/radiology/mwl") {
      return [{
        "00100010": { vr: "PN", Value: [{ Alphabetic: "Doe^Jane" }] },
        "00100020": { vr: "LO", Value: ["pat-1"] },
        "00080050": { vr: "SH", Value: ["study-1"] },
        "0020000D": { vr: "UI", Value: ["2.25.123456789"] },
        "00080060": { vr: "CS", Value: ["CT"] },
        "00400100": {
          vr: "SQ",
          Value: [{
            "00080060": { vr: "CS", Value: ["CT"] },
            "00400001": { vr: "AE", Value: ["CTRAD1"] },
            "00400002": { vr: "DA", Value: ["20261019"] },
            "00400003": { vr: "TM", Value: ["093000"] },
            "00400009": { vr: "SH", Value: ["study-1"] },
          }],
        },
      }];
    }
    if (path === "/radiology/dicom/ingest") {
      return {
        id: 1, study_id: "study-1", study_uid: "2.25.123456789",
        series_uid: "2.25.series-1", sop_uid: "2.25.sop-1",
        sop_class_uid: "1.2.840.10008.5.1.4.1.1.7", modality: "CT",
        patient_id: "pat-1", series_number: 1, instance_number: 1,
        cold_ref: "cold://sha256/deadbeef",
        content_hash: "aaaaaaaabbbbbbbbccccccccdddddddd",
        size_bytes: 5, already_archived: false,
      };
    }
    if (path === "/radiology/reports/rep-img-1/verify") return { ...studyDetail.reports[0], status: "final" };
    if (path === "/radiology/reports/rep-img-1/critical/read-back") {
      return { ...criticalItem, state: "read-back", readback_by: "Dr Ordering" };
    }
    return {};
  });
  return { request } as unknown as ApiClient;
}

function requestCalls(client: ApiClient): Array<[string, unknown]> {
  return (client.request as unknown as ReturnType<typeof vi.fn>).mock.calls as Array<[string, unknown]>;
}

describe("RadiologyPage (Phase 15 department)", () => {
  it("renders the worklist with state + STAT badges and filters by state", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    expect(await screen.findByText("CT chest with contrast — rule out pulmonary embolism (synthetic demo)")).toBeTruthy();
    expect(screen.getAllByText("STAT").length).toBeGreaterThan(0);
    expect(screen.getAllByText("ordered").length).toBeGreaterThan(0);
    // Header TAT tiles come from /radiology/stats/tat.
    expect(screen.getByText("STAT not yet reported")).toBeTruthy();

    fireEvent.change(screen.getByLabelText("Worklist state filter"), { target: { value: "arrived" } });
    await waitFor(() => {
      expect(requestCalls(client).some(([path]) => path === "/radiology/worklist?state=arrived")).toBe(true);
    });
  });

  it("opens study detail with series/instances, the external viewer link, and verifies the report", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText(/Series 2\.25\.series-1/)).toBeTruthy();
    expect(screen.getByText(/SOP 2\.25\.sop-1/)).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open in external viewer" });
    expect(link.getAttribute("href")).toBe(
      "https://viewer.ohif.org/viewer?StudyInstanceUIDs=2.25.123456789");
    expect(screen.getByText(/external viewer — leaves OpenCare/)).toBeTruthy();
    expect(screen.getByText("No acute findings. (synthetic)")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Verify report" }));
    await waitFor(() => {
      expect(requestCalls(client).some(([path]) => path === "/radiology/reports/rep-img-1/verify")).toBe(true);
    });
  });

  it("submits a critical-result read-back with reader + text", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    await screen.findByText("CT chest with contrast — rule out pulmonary embolism (synthetic demo)");
    fireEvent.click(screen.getByRole("tab", { name: "Critical results" }));
    expect(await screen.findByText(/Overdue by 95 min/)).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Reader for report rep-img-1"), {
      target: { value: "Dr Ordering" },
    });
    fireEvent.change(screen.getByLabelText("Read-back text for report rep-img-1"), {
      target: { value: "Critical finding read back verbatim" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Submit read-back" }));
    await waitFor(() => {
      const call = requestCalls(client).find(([path]) =>
        path === "/radiology/reports/rep-img-1/critical/read-back");
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as { body: string }).body) as Record<string, string>;
      expect(body.reader).toBe("Dr Ordering");
      expect(body.read_back_text).toBe("Critical finding read back verbatim");
    });
  });

  it("renders the modality worklist tab from DICOM JSON with RIS status", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    await screen.findByText("CT chest with contrast — rule out pulmonary embolism (synthetic demo)");
    fireEvent.click(screen.getByRole("tab", { name: "Modality worklist" }));
    // Patient name renders from the PN Alphabetic component (^ -> space).
    expect(await screen.findByText("Doe Jane")).toBeTruthy();
    expect(screen.getByText("pat-1")).toBeTruthy();
    expect(screen.getByText("CTRAD1")).toBeTruthy();
    // Scheduled DA/TM render as an ISO date + time.
    expect(screen.getByText(/2026-10-19 09:30:00/)).toBeTruthy();
    // Status is the study's live RIS worklist state (the stub's item
    // is 'ordered'), shown as a text+colour badge.
    expect(screen.getByText("ordered")).toBeTruthy();
    expect(requestCalls(client).some(([path]) => path === "/radiology/mwl")).toBe(true);
  });

  it("ingests a DICOM file through the ingest panel and shows the archive result", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    await screen.findByText("CT chest with contrast — rule out pulmonary embolism (synthetic demo)");
    fireEvent.click(screen.getByRole("tab", { name: "DICOM ingest" }));
    expect(await screen.findByRole("heading", { name: "DICOM ingest" })).toBeTruthy();
    const input = screen.getByLabelText("DICOM file") as HTMLInputElement;
    const bytes = new Uint8Array([0x00, 0x01, 0x02, 0x03, 0xff]);
    const file = new File([bytes], "study.dcm", { type: "application/dicom" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(await screen.findByText(/Selected: study\.dcm \(5 bytes\)/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Ingest DICOM file" }));
    // The archive result renders study/series/instance ids + the hash.
    expect(await screen.findByText("2.25.sop-1")).toBeTruthy();
    expect(screen.getByText("Archived")).toBeTruthy();
    expect(screen.getByText(/a{8}b{8}/)).toBeTruthy();
    expect(screen.getByText("cold://sha256/deadbeef")).toBeTruthy();
    // The request went to the ingest endpoint as base64-in-JSON of the
    // exact file bytes (the backend contract), not multipart.
    const call = requestCalls(client).find(([path]) => path === "/radiology/dicom/ingest");
    expect(call).toBeTruthy();
    const body = JSON.parse((call![1] as { body: string }).body) as { dicom_base64: string };
    expect(body.dicom_base64).toBe(btoa("\x00\x01\x02\x03\xff"));
  });

  it("drafts a report conclusion through the rich-text editor (WS2b)", async () => {
    const client = stubClient();
    // Same stub, but the study has no report yet, so the structured
    // reporting section offers the free conclusion editor.
    const request = client.request as unknown as ReturnType<typeof vi.fn>;
    const baseImpl = request.getMockImplementation() as (
      path: string,
      init?: unknown,
    ) => Promise<unknown>;
    request.mockImplementation(async (path: string, init?: unknown) => {
      if (path === "/radiology/studies/study-1") {
        return { ...studyDetail, reports: [], report: null };
      }
      if (path === "/radiology/studies/study-1/report") {
        return { ...studyDetail.reports[0], conclusion: "<p>Draft</p>" };
      }
      return baseImpl(path, init);
    });
    render(<RadiologyPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    const editor = await screen.findByRole("textbox", { name: "Report conclusion" });
    expect(screen.getByRole("button", { name: "Bold" })).toBeTruthy();
    editor.innerHTML = "<p>No <strong>acute</strong> findings</p>";
    fireEvent.input(editor);
    fireEvent.click(screen.getByRole("button", { name: "Save report draft" }));
    await waitFor(() => {
      const call = requestCalls(client).find(
        ([path]) => path === "/radiology/studies/study-1/report",
      );
      expect(call).toBeTruthy();
      const body = JSON.parse((call![1] as { body: string }).body) as { conclusion: string };
      expect(body.conclusion).toBe("<p>No <strong>acute</strong> findings</p>");
    });
  });

  it("renders the dose tab and a patient's cumulative dose history", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    await screen.findByText("CT chest with contrast — rule out pulmonary embolism (synthetic demo)");
    fireEvent.click(screen.getByRole("tab", { name: "Dose" }));
    expect(await screen.findByText("Radiation dose")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Patient id for dose history"), {
      target: { value: "pat-1" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Look up dose history" }));
    expect(await screen.findByText("Cumulative dose — Patient/pat-1")).toBeTruthy();
    expect(screen.getByText("CTDIvol")).toBeTruthy();
    expect(screen.getByText(/Total CTDIvol:/)).toBeTruthy();
  });
});
