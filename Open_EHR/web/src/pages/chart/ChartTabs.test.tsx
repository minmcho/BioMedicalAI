import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// This project's vitest setup has no global RTL cleanup; each render
// in this file mounts a full chart, so unmount explicitly per test.
afterEach(cleanup);
import type { ApiClient } from "../../api/client";
import {
  fixtureAdmission,
  fixtureAdtEvents,
  fixtureCarePlan,
  fixtureCondition,
  fixtureEncounter,
  fixtureFlowsheetView,
  fixtureMar,
  fixtureMedication,
  fixtureNotes,
  fixtureNoteTypes,
  fixtureObservation,
  fixtureOrders,
  fixtureOrderSets,
  fixtureOrderTypes,
  fixturePatient,
  fixtureProcedure,
  fixtureProgressForm,
  fixtureReport,
  fixtureDispositions,
  fixtureVitalsForm,
} from "../../test/fixtures";
import { ChartPage } from "../ChartPage";

// Chart tabs (Phase 5) against a stub client at the network boundary —
// shipped code always uses the real ApiClient. The stub mirrors the
// /orders, /notes, /flowsheets and MAR response shapes.
function stubClient() {
  const byType: Record<string, unknown[]> = {
    Condition: [fixtureCondition],
    MedicationRequest: [fixtureMedication],
    Observation: [fixtureObservation],
    DiagnosticReport: [fixtureReport],
    Procedure: [fixtureProcedure],
    Encounter: [fixtureEncounter],
  };
  const forms: Record<string, unknown> = {
    "note-progress": fixtureProgressForm,
    "flowsheet-vitals": fixtureVitalsForm,
  };
  return {
    get: vi.fn(async () => fixturePatient),
    search: vi.fn(async (type: string) => byType[type] ?? []),
    orderTypes: vi.fn(async () => fixtureOrderTypes),
    orderSets: vi.fn(async () => fixtureOrderSets),
    orders: vi.fn(async () => ({ items: fixtureOrders, total: fixtureOrders.length })),
    placeOrder: vi.fn(async () => ({ order: fixtureOrders[0], order_type: "procedure", warnings: [] })),
    placeOrderSet: vi.fn(async () => ({ orders: fixtureOrders, total: 2, warnings: [] })),
    transitionOrder: vi.fn(async () => fixtureOrders[0]),
    noteTypes: vi.fn(async () => fixtureNoteTypes),
    notes: vi.fn(async () => ({ items: fixtureNotes, total: fixtureNotes.length })),
    createNote: vi.fn(async () => fixtureNotes[0]),
    signNote: vi.fn(async () => ({ ...fixtureNotes[0], docStatus: "final" })),
    amendNote: vi.fn(async () => ({ ...fixtureNotes[0], docStatus: "amended" })),
    effectiveConfig: vi.fn(async (_kind: string, key: string) => ({
      payload: forms[key] ?? {},
    })),
    effectiveConfigs: vi.fn(async () => [{ key: "flowsheet-vitals" }]),
    flowsheetView: vi.fn(async () => fixtureFlowsheetView),
    submitFlowsheet: vi.fn(async () => ({ resourceType: "QuestionnaireResponse", id: "qr-new" })),
    marDue: vi.fn(async () => fixtureMar),
    patientAdmission: vi.fn(async () => ({ admission: fixtureAdmission })),
    carePlan: vi.fn(async () => fixtureCarePlan),
    adtEvents: vi.fn(async () => fixtureAdtEvents),
    dischargeDispositions: vi.fn(async () => fixtureDispositions),
    addCarePlanItem: vi.fn(async () => ({ task: fixtureCarePlan.tasks[0] })),
    transitionCarePlanItem: vi.fn(async () => ({ task: fixtureCarePlan.tasks[0] })),
    setDischargePlan: vi.fn(async () => ({})),
    create: vi.fn(async () => ({ resourceType: "QuestionnaireResponse", id: "qr-9" })),
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

async function openTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

describe("ChartPage Phase 5 tabs", () => {
  it("orders tab lists orders and signs a draft through the client", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Orders");

    expect(await screen.findByText(/ECG 12-lead/)).toBeTruthy();
    expect(screen.getByText(/Lisinopril 10 mg/)).toBeTruthy();
    expect(screen.getByText("draft")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Sign" }));
    await vi.waitFor(() =>
      expect(client.transitionOrder).toHaveBeenCalledWith("ord-ecg", "signed"),
    );
  });

  it("orders tab hides placement and actions from a viewer role", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="viewer" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Orders");
    expect(await screen.findByText(/ECG 12-lead/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Place order" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign" })).toBeNull();
  });

  it("notes tab renders the published template form and submits answers", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Notes");

    expect(await screen.findByText("preliminary")).toBeTruthy();
    // The template form (form/note-progress) is rendered from config.
    const assessment = await screen.findByLabelText(/Assessment/);
    fireEvent.change(assessment, { target: { value: "Stable" } });
    fireEvent.change(screen.getByLabelText(/Plan/), {
      target: { value: "Continue" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create note" }));

    await vi.waitFor(() => expect(client.createNote).toHaveBeenCalledTimes(1));
    const arg = (client.createNote as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(arg.noteType).toBe("progress");
    expect(arg.answers).toContainEqual({
      linkId: "assessment",
      answer: [{ valueString: "Stable" }],
    });
    expect(arg.answers).toContainEqual({
      linkId: "plan",
      answer: [{ valueString: "Continue" }],
    });
  });

  it("flowsheet tab renders the time-series grid from the view API", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Flowsheet");

    expect(await screen.findByText("Systolic BP")).toBeTruthy();
    expect(screen.getByText("121 mmHg")).toBeTruthy();
    expect(screen.getByText("99 %")).toBeTruthy();
    // The entry form is the same published form, rendered for input.
    expect(screen.getByLabelText(/Systolic BP/)).toBeTruthy();
  });

  it("MAR tab shows computed given / missed doses and the summary", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("MAR");

    expect(await screen.findByText(/Given 1 · Due 1 · Missed 1 · Scheduled 1/)).toBeTruthy();
    expect(screen.getByText("Given")).toBeTruthy();
    expect(screen.getByText("Missed")).toBeTruthy();
    expect(screen.getAllByText(/Lisinopril 10 mg/).length).toBeGreaterThan(0);
  });

  it("admission tab shows bed, plan, care plan and ADT history (Phase 6)", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="clinician" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Admission");

    expect(await screen.findByText(/Bed/)).toBeTruthy();
    expect(screen.getByText("MS-101A")).toBeTruthy();
    expect(screen.getByText(/Target 2026-10-20/)).toBeTruthy();
    expect(await screen.findByText(/Arrange SNF placement/)).toBeTruthy();
    expect(screen.getByText(/A01/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "in-progress" }));
    await vi.waitFor(() =>
      expect(client.transitionCarePlanItem).toHaveBeenCalledWith("task-1", "in-progress"),
    );
  });

  it("admission tab hides work actions from a viewer role (Phase 6)", async () => {
    const client = stubClient();
    render(<ChartPage client={client} patientId="pat-1" role="viewer" />);
    await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ });
    await openTab("Admission");
    expect(await screen.findByText("MS-101A")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Add item" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Save plan" })).toBeNull();
  });
});
