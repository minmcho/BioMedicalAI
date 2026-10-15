import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureCondition,
  fixtureEncounter,
  fixtureMedication,
  fixtureObservation,
  fixturePatient,
  fixtureProcedure,
  fixtureReport,
} from "../test/fixtures";
import { ChartPage } from "./ChartPage";

// Component render test with fixture (synthetic) FHIR data. The stub client
// stands in for the network boundary in a unit test only — shipped code
// always uses the real ApiClient.
function stubClient(): ApiClient {
  const byType: Record<string, unknown[]> = {
    Condition: [fixtureCondition],
    MedicationRequest: [fixtureMedication],
    Observation: [fixtureObservation],
    DiagnosticReport: [fixtureReport],
    Procedure: [fixtureProcedure],
    Encounter: [fixtureEncounter],
  };
  return {
    get: vi.fn(async () => fixturePatient),
    search: vi.fn(async (type: string) => byType[type] ?? []),
  } as unknown as ApiClient;
}

describe("ChartPage", () => {
  it("renders the patient header and every chart section from FHIR data", async () => {
    render(<ChartPage client={stubClient()} patientId="pat-1" />);

    expect(await screen.findByRole("heading", { name: /Synthetic Sample Testworth/ })).toBeTruthy();
    expect(screen.getByText("Synthetic example condition")).toBeTruthy();
    expect(screen.getByText(/Synthetic example medication 10 mg/)).toBeTruthy();
    expect(screen.getByText(/Synthetic example measurement/)).toBeTruthy();
    expect(screen.getByText(/42 units/)).toBeTruthy();
    expect(screen.getByText(/Synthetic example panel/)).toBeTruthy();
    expect(screen.getByText(/Synthetic conclusion for testing/)).toBeTruthy();
    expect(screen.getByText(/Synthetic example procedure/)).toBeTruthy();
    expect(screen.getByText(/finished/)).toBeTruthy();
  });

  it("shows the permission-denied state (not an empty chart) on a 403", async () => {
    const { ApiError } = await import("../api/client");
    const failing = {
      get: vi.fn(async () => {
        throw new ApiError(403, "Role patient may not perform this action");
      }),
      search: vi.fn(async () => []),
    } as unknown as ApiClient;
    render(<ChartPage client={failing} patientId="pat-1" />);
    expect(await screen.findByText(/Permission denied/)).toBeTruthy();
  });
});
