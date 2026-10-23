import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "./api/client";
import { parsePermissions } from "./pages/studio/models";
import { resolveNavGroups } from "./auth/session";
import { BOARD_PAGES, BreadcrumbBar, GlobalPatientSearch, TopBar } from "./App";

// Home & Module UX pass: the web shell — slim top bar (brand, Home,
// global patient search for staff, identity + sign out), breadcrumb
// bar (Home › group › module; chart: Home › Patients › {id}), and
// the wide-layout board set. Shell-level behaviours (route guard,
// idle lock, downtime) ride AuthProvider and stay covered by their
// own suites; these tests pin the new shell surfaces directly.

afterEach(cleanup);

const PATIENTS = [
  {
    resourceType: "Patient",
    id: "pat-1",
    name: [{ family: "Lovelace", given: ["Ada"] }],
    identifier: [{ system: "urn:opencare:mrn", value: "MRN-000001" }],
  },
  {
    resourceType: "Patient",
    id: "pat-2",
    name: [{ family: "Hopper", given: ["Grace"] }],
    identifier: [{ system: "urn:opencare:mrn", value: "MRN-000002" }],
  },
];

function searchClient(): ApiClient {
  return {
    search: vi.fn(async () => PATIENTS),
  } as unknown as ApiClient;
}

function topBarClient(): ApiClient {
  return searchClient();
}

describe("TopBar", () => {
  it("renders brand, Home, user + role badge and Sign out for staff", () => {
    const onHome = vi.fn();
    const onSignOut = vi.fn();
    render(
      <TopBar
        orgName="Mercy General"
        username="dev-clinician"
        role="clinician"
        isPatient={false}
        client={topBarClient()}
        onHome={onHome}
        onOpenChart={() => {}}
        onSignOut={onSignOut}
      />,
    );
    expect(screen.getByText("Mercy General")).toBeTruthy();
    expect(screen.getByText("dev-clinician")).toBeTruthy();
    expect(screen.getByText("clinician")).toBeTruthy();
    expect(screen.getByLabelText("Search patients")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Go to Home" }));
    expect(onHome).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalledTimes(1);
  });

  it("patient top bar has NO patient search", () => {
    render(
      <TopBar
        orgName="OpenCare"
        username="dev-patient"
        role="patient"
        isPatient
        client={topBarClient()}
        onHome={() => {}}
        onOpenChart={() => {}}
        onSignOut={() => {}}
      />,
    );
    expect(screen.queryByLabelText("Search patients")).toBeNull();
    expect(screen.getByText("dev-patient")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign out" })).toBeTruthy();
  });
});

describe("GlobalPatientSearch", () => {
  it("does not query below 2 characters; at 2+ it queries the Patients-page search and shows name + MRN", async () => {
    const client = searchClient();
    render(<GlobalPatientSearch client={client} onOpenChart={() => {}} />);
    const input = screen.getByLabelText("Search patients");
    fireEvent.change(input, { target: { value: "a" } });
    await new Promise((r) => setTimeout(r, 300));
    expect(client.search).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "ada" } });
    expect(await screen.findByText("Ada Lovelace")).toBeTruthy();
    expect(screen.getByText("MRN-000001")).toBeTruthy();
    expect(client.search).toHaveBeenCalledWith("Patient");
    // Non-matching patients are filtered out (same match as PatientsPage).
    expect(screen.queryByText("Grace Hopper")).toBeNull();
  });

  it("clicking a result opens the chart in one action", async () => {
    const onOpenChart = vi.fn();
    render(<GlobalPatientSearch client={searchClient()} onOpenChart={onOpenChart} />);
    fireEvent.change(screen.getByLabelText("Search patients"), {
      target: { value: "hopper" },
    });
    fireEvent.click(await screen.findByText("Grace Hopper"));
    expect(onOpenChart).toHaveBeenCalledTimes(1);
    expect(onOpenChart).toHaveBeenCalledWith("pat-2");
  });

  it("Enter opens the first matching chart", async () => {
    const onOpenChart = vi.fn();
    render(<GlobalPatientSearch client={searchClient()} onOpenChart={onOpenChart} />);
    const input = screen.getByLabelText("Search patients");
    fireEvent.change(input, { target: { value: "a" } });
    fireEvent.change(input, { target: { value: "ad" } });
    await screen.findByText("Ada Lovelace");
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(onOpenChart).toHaveBeenCalledWith("pat-1"));
  });

  it("pressing / outside a form field focuses the search", async () => {
    render(<GlobalPatientSearch client={searchClient()} onOpenChart={() => {}} />);
    const input = screen.getByLabelText("Search patients") as HTMLInputElement;
    fireEvent.keyDown(document.body, { key: "/" });
    await waitFor(() => expect(document.activeElement).toBe(input));
  });

  it("a search failure closes the dropdown quietly (no dead control, no error wall)", async () => {
    const client = {
      search: vi.fn(async () => {
        throw new Error("offline");
      }),
    } as unknown as ApiClient;
    render(<GlobalPatientSearch client={client} onOpenChart={() => {}} />);
    fireEvent.change(screen.getByLabelText("Search patients"), {
      target: { value: "ada" },
    });
    await new Promise((r) => setTimeout(r, 350));
    expect(screen.queryByText("Ada Lovelace")).toBeNull();
    expect(screen.queryByRole("listbox")).toBeNull();
  });
});

describe("BreadcrumbBar", () => {
  const items = resolveNavGroups("clinician", null, parsePermissions({})).flatMap(
    (g) => g.items,
  );

  it("module page: Home › {group} › {module label}, Home is a working button", () => {
    const onHome = vi.fn();
    render(
      <BreadcrumbBar
        page="lab"
        chartPatientId={null}
        items={items}
        onHome={onHome}
        onNavigate={() => {}}
      />,
    );
    const nav = screen.getByLabelText("Breadcrumb");
    expect(nav.textContent).toContain("Diagnostics & Pharmacy");
    expect(nav.textContent).toContain("Laboratory");
    expect(screen.getByText("Laboratory").getAttribute("aria-current")).toBe("page");
    fireEvent.click(screen.getByRole("button", { name: "Home" }));
    expect(onHome).toHaveBeenCalledTimes(1);
  });

  it("chart: Home › Patients › {patient id}, Patients navigates back to the list", () => {
    const onNavigate = vi.fn();
    render(
      <BreadcrumbBar
        page="chart"
        chartPatientId="pat-1"
        items={items}
        onHome={() => {}}
        onNavigate={onNavigate}
      />,
    );
    const nav = screen.getByLabelText("Breadcrumb");
    expect(nav.textContent).toContain("Patients");
    expect(nav.textContent).toContain("pat-1");
    fireEvent.click(screen.getByRole("button", { name: "Patients" }));
    expect(onNavigate).toHaveBeenCalledWith("patients");
  });

  it("home page: a single current Home crumb", () => {
    render(
      <BreadcrumbBar
        page="home"
        chartPatientId={null}
        items={items}
        onHome={() => {}}
        onNavigate={() => {}}
      />,
    );
    const nav = screen.getByLabelText("Breadcrumb");
    expect(nav.textContent?.trim()).toBe("Home");
    expect(screen.queryByRole("button", { name: "Home" })).toBeNull();
  });
});

describe("BOARD_PAGES (wide layout set)", () => {
  it("is exactly the ledger's board set", () => {
    expect([...BOARD_PAGES].sort()).toEqual(
      ["beds", "command", "data-platform", "ed", "lab", "unit-board"].sort(),
    );
  });
});
