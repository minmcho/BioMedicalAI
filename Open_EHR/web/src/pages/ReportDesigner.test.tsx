import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { saveDownloadedFile } from "../api/download";
import { ReportDesignerPanel } from "./ReportDesigner";

// Phase 15 (BD-32/BD-30): the Studio report designer composes a
// governed report-definition draft from the /designer/datasets
// palette (no SQL anywhere), validates/saves through the report
// engine's draft endpoints, runs only PUBLISHED definitions, and
// renders the /platform-depth/dashboard-indicators trace.
vi.mock("../api/download", () => ({ saveDownloadedFile: vi.fn() }));

afterEach(cleanup);

const PATIENT_FIELDS = [
  { name: "id", type: "string" },
  { name: "name", type: "string" },
  { name: "gender", type: "string" },
  { name: "birth_date", type: "string" },
];

const DATASETS = {
  items: [
    {
      key: "patients", title: "Patients", description: "Patient demographics",
      field_count: 4, fields: PATIENT_FIELDS,
    },
    {
      key: "appointments", title: "Appointments",
      description: "Scheduled appointments", field_count: 3,
      fields: [
        { name: "id", type: "string" },
        { name: "patient", type: "string" },
        { name: "status", type: "string" },
      ],
    },
  ],
  total: 2,
};

const PATIENTS_DETAIL = {
  key: "patients", title: "Patients", description: "Patient demographics",
  fields: PATIENT_FIELDS,
  filter_ops: ["eq", "ne", "in", "gte", "lte", "contains"],
  formats: ["json", "csv", "pdf"],
};

const TRACE = {
  dashboards: [
    {
      key: "operations-overview", version: 2, title: "Operations overview",
      widgets: [
        {
          id: "census", title: "Patients in beds", disposition: "indicator",
          indicator: "operations-census", applied: true,
          endpoint: "/indicators/operations-census/value",
        },
        {
          id: "bed-table", title: "Bed table", disposition: "live-endpoint",
          indicator: null, applied: false, endpoint: "/inpatient/beds",
          reason:
            "Table widget renders multiple rows; an indicator is a single scalar value — no one indicator can honestly supply it.",
        },
      ],
    },
  ],
  total: 1,
  widgets_total: 2,
  widgets_on_indicators: 1,
  widgets_on_live_endpoints: 1,
};

const RUN_RESULT = {
  key: "patients-by-gender", version: 3, title: "Patients by gender",
  dataset: "patients", format: "json", row_count: 2,
  fields: ["id", "gender"],
  rows: [
    { id: "pat-a", gender: "female" },
    { id: "pat-b", gender: "female" },
  ],
  groups: null,
  run: {
    id: 7, definition_key: "patients-by-gender", definition_version: 3,
    actor: "dev-admin", row_count: 2, format: "json",
    ran_at: "2026-10-18T00:00:00Z",
  },
};

type RequestFn = (path: string, init?: RequestInit) => Promise<unknown>;

function stubClient(overrides: { createDraft?: RequestFn } = {}) {
  const request = vi.fn(async (path: string, init: RequestInit = {}) => {
    const method = init.method ?? "GET";
    if (method === "GET" && path === "/designer/datasets") return DATASETS;
    if (method === "GET" && path === "/designer/datasets/patients") {
      return PATIENTS_DETAIL;
    }
    if (method === "GET" && path === "/platform-depth/dashboard-indicators") {
      return TRACE;
    }
    if (method === "POST" && path.startsWith("/report-engine/definitions/")) {
      if (overrides.createDraft) return overrides.createDraft(path, init);
      const key = decodeURIComponent(path.split("/").pop() ?? "");
      return {
        kind: "report-definition", key, version: 1, status: "draft",
        payload: JSON.parse(String(init.body)),
        created_by: "dev-admin", updated_by: "dev-admin",
        created_at: null, updated_at: null,
      };
    }
    if (method === "POST" && path === "/report-engine/patients-by-gender/run") {
      return RUN_RESULT;
    }
    throw new Error(`Unexpected request: ${method} ${path}`);
  });
  const requestBlob = vi.fn(async () => ({
    blob: new Blob(["id,gender"], { type: "text/csv" }),
    filename: "patients-by-gender.csv",
    rows: 2,
  }));
  const client = { request, requestBlob } as unknown as ApiClient;
  return { client, request, requestBlob };
}

/** Render and wait until the palette AND the patients detail schema
 *  (which resets the composition) have both landed. */
async function renderDesigner(overrides: { createDraft?: RequestFn } = {}) {
  const stub = stubClient(overrides);
  render(<ReportDesignerPanel client={stub.client} />);
  expect(await screen.findByText("Patient demographics")).toBeTruthy();
  await waitFor(() =>
    expect(stub.request).toHaveBeenCalledWith("/designer/datasets/patients"),
  );
  await waitFor(() =>
    expect((screen.getByLabelText("Field id") as HTMLInputElement).checked).toBe(true),
  );
  return stub;
}

describe("ReportDesignerPanel (BD-32/BD-30)", () => {
  it("renders the dataset palette with fields and inferred types", async () => {
    await renderDesigner();
    expect(screen.getByText("Report designer")).toBeTruthy();
    // draft-only banner is part of the surface, per Min's rule
    expect(screen.getByText(/Draft only/)).toBeTruthy();
    expect(screen.getByText(/a human publishes in Studio/)).toBeTruthy();
    // palette: datasets + fields with types from /designer/datasets
    expect(screen.getByText("Scheduled appointments")).toBeTruthy();
    expect(screen.getByLabelText("Field gender")).toBeTruthy();
    expect(screen.getByLabelText("Field birth_date")).toBeTruthy();
    expect(screen.getAllByText("string").length).toBeGreaterThanOrEqual(4);
    // no SQL field anywhere in the designer
    expect(screen.queryByLabelText(/sql/i)).toBeNull();
    expect(document.body.textContent).not.toMatch(/query editor/i);
  });

  it("filter-row composition produces the expected definition payload on save-draft", async () => {
    const { request } = await renderDesigner();
    fireEvent.change(screen.getByLabelText("Report title"), {
      target: { value: "Patients by gender" },
    });
    fireEvent.change(screen.getByLabelText("Report key"), {
      target: { value: "patients-by-gender" },
    });
    // field picks: keep id + gender only
    fireEvent.click(screen.getByLabelText("Field name"));
    fireEvent.click(screen.getByLabelText("Field birth_date"));
    fireEvent.click(screen.getByLabelText("Group by gender"));
    // one filter row: gender eq female
    fireEvent.click(screen.getByRole("button", { name: "Add filter" }));
    fireEvent.change(screen.getByLabelText("Filter 1 field"), {
      target: { value: "gender" },
    });
    fireEvent.change(screen.getByLabelText("Filter 1 operator"), {
      target: { value: "eq" },
    });
    fireEvent.change(screen.getByLabelText("Filter 1 value"), {
      target: { value: "female" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save as draft" }));

    await waitFor(() => {
      const saveCall = request.mock.calls.find(
        ([path, init]) =>
          path === "/report-engine/definitions/patients-by-gender" &&
          (init as RequestInit | undefined)?.method === "POST",
      );
      expect(saveCall).toBeTruthy();
      const body = JSON.parse(String((saveCall?.[1] as RequestInit).body));
      expect(body).toEqual({
        title: "Patients by gender",
        dataset: "patients",
        fields: ["id", "gender"],
        filters: [{ field: "gender", op: "eq", value: "female" }],
        grouping: ["gender"],
        formats: ["json", "csv", "pdf"],
      });
    });
    expect(
      await screen.findByText(/Draft saved: report-definition\/patients-by-gender v1/),
    ).toBeTruthy();
  });

  it("surfaces the server validation error verbatim", async () => {
    const detail =
      "Unknown field 'ssn' for dataset 'patients'; fields: ['birth_date', 'gender', 'id', 'name']";
    await renderDesigner({
      createDraft: async () => {
        throw new ApiError(422, detail);
      },
    });
    fireEvent.change(screen.getByLabelText("Report title"), {
      target: { value: "Bad report" },
    });
    fireEvent.change(screen.getByLabelText("Report key"), {
      target: { value: "bad-report" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Validate" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Server validation failed.");
    expect(alert.textContent).toContain(detail);
  });

  it("renders the BD-30 dashboard indicator trace table", async () => {
    await renderDesigner();
    // one dashboard cell per widget row (2 widgets in the fixture)
    expect((await screen.findAllByText(/Operations overview/)).length).toBe(2);
    expect(screen.getByText("Patients in beds")).toBeTruthy();
    expect(screen.getByText(/Indicator: operations-census/)).toBeTruthy();
    expect(screen.getByText(/Resolves through \/indicators\/operations-census\/value/)).toBeTruthy();
    expect(screen.getByText("Bed table")).toBeTruthy();
    expect(screen.getByText("Live endpoint")).toBeTruthy();
    expect(screen.getByText(/Table widget renders multiple rows/)).toBeTruthy();
    expect(screen.getByText(/1 of 2 widgets resolve through published indicators/)).toBeTruthy();
  });

  it("run preview renders the published definition's rows; CSV downloads via the helper", async () => {
    const { requestBlob } = await renderDesigner();
    fireEvent.change(screen.getByLabelText("Report key"), {
      target: { value: "patients-by-gender" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run preview" }));
    expect(await screen.findByText("pat-a")).toBeTruthy();
    expect(screen.getByText(/2 rows/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Download CSV" }));
    await waitFor(() =>
      expect(requestBlob).toHaveBeenCalledWith(
        "/report-engine/patients-by-gender/run",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    await waitFor(() => expect(saveDownloadedFile).toHaveBeenCalled());
  });
});
