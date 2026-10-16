import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import {
  fixtureModalities,
  fixtureRadiologyDetail,
  fixtureRadiologyItem,
} from "../test/fixtures";
import { RadiologyPage } from "./RadiologyPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    radiologyStudies: vi.fn(async () => [fixtureRadiologyItem]),
    radiologyModalities: vi.fn(async () => fixtureModalities),
    radiologyStudy: vi.fn(async () => fixtureRadiologyDetail),
    createStudy: vi.fn(async () => fixtureRadiologyItem),
    createImagingReport: vi.fn(async () => fixtureRadiologyDetail.reports[0]),
    verifyImagingReport: vi.fn(async () => ({
      ...fixtureRadiologyDetail.reports[0], status: "final",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("RadiologyPage", () => {
  it("lists studies with the external viewer link and states metadata-only plainly", async () => {
    render(<RadiologyPage client={stubClient()} role="clinician" />);
    expect(await screen.findByText("Chest X-ray, 2 views")).toBeTruthy();
    expect(screen.getByText(/metadata-only in v1/)).toBeTruthy();
    expect(screen.getByText("no report")).toBeTruthy();
    const link = screen.getByRole("link", { name: "Open in viewer" });
    expect(link.getAttribute("href")).toBe(
      "https://viewer.ohif.org/viewer?StudyInstanceUIDs=2.25.123456789");
    expect(link.getAttribute("target")).toBe("_blank");
  });

  it("opens study detail and verifies the preliminary report", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    fireEvent.click(await screen.findByRole("button", { name: "Details" }));
    expect(await screen.findByText("No acute findings. (synthetic)")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Verify report" }));
    await waitFor(() => {
      expect(client.verifyImagingReport).toHaveBeenCalledWith("rep-img-1");
    });
  });

  it("creates a study from an order id + modality through the client", async () => {
    const client = stubClient();
    render(<RadiologyPage client={client} role="clinician" />);
    await screen.findByText("Chest X-ray, 2 views");
    fireEvent.change(screen.getByLabelText("Service request id"), {
      target: { value: "sr-img-9" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create study" }));
    await waitFor(() => {
      expect(client.createStudy).toHaveBeenCalledWith({
        serviceRequestId: "sr-img-9", modality: "CT",
      });
    });
  });

  it("viewer sees studies but no create form and no verify action", async () => {
    render(<RadiologyPage client={stubClient()} role="viewer" />);
    expect(await screen.findByText("Chest X-ray, 2 views")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Create study" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(await screen.findByText("No acute findings. (synthetic)")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Verify report" })).toBeNull();
  });
});
