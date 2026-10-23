import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import type {
  AuditEntry,
  ModulePack,
  PackInstallation,
  StudioConfigObject,
} from "../api/types";
import { StudioPage } from "./StudioPage";

// See LabPage.test.tsx: no vitest globals here, so RTL auto-cleanup does
// not run — clean up explicitly.
afterEach(cleanup);

function cfg(
  kind: StudioConfigObject["kind"],
  key: string,
  payload: Record<string, unknown>,
  version = 1,
  status: StudioConfigObject["status"] = "published",
): StudioConfigObject {
  return {
    id: Math.floor(Math.random() * 1e6) + version,
    kind, key, version, status, payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-16T00:00:00Z", updated_at: "2026-10-16T00:00:00Z",
  };
}

const CONFIGS: StudioConfigObject[] = [
  cfg("branding", "default", {
    orgName: "OpenCare Clinic", portalTitle: "OpenCare Patient Portal",
    primaryColor: "#1a7f37", supportContact: "help@opencare.example",
  }),
  cfg("form", "intake", {
    resourceType: "Questionnaire", title: "Intake form",
    item: [
      { linkId: "q1", text: "Chief complaint", type: "string", required: true },
      { linkId: "q2", text: "Pain scale", type: "choice", required: false,
        answerOption: [{ valueCoding: { code: "none", display: "No pain" } }] },
    ],
  }),
  cfg("workflow", "appointment-adt", {
    states: ["booked", "arrived", "fulfilled"],
    transitions: [{ from: "booked", to: "arrived" }],
  }),
  cfg("terminology", "lab-catalog", {
    name: "Lab catalog", system: "http://loinc.org",
    concepts: [
      { code: "718-7", display: "Hemoglobin", active: true, unit: "g/dL",
        referenceRange: { unit: "g/dL", low: 12, high: 15.5 } },
    ],
  }),
  cfg("navigation", "main", {
    items: [{ module: "scheduling", label: "Scheduling", roles: ["clinician"],
              order: 1, visible: true }],
  }),
  cfg("integration", "dicom-viewer", {
    viewer: { name: "OHIF viewer (public demo)", baseUrl: "https://viewer.ohif.org" },
  }),
];

const PACKS: ModulePack[] = [
  { id: 1, name: "lab", version: "2.0.0",
    manifest: { name: "lab", version: "2.0.0", description: "Laboratory",
                modules: [{ id: "lab-orders", name: "Lab Orders", enabled: false }] } },
  { id: 2, name: "clinical-core", version: "1.0.0",
    manifest: { name: "clinical-core", version: "1.0.0" } },
];

const INSTALLATIONS: PackInstallation[] = [
  { id: 1, pack_id: 1, pack_name: "lab", version: "2.0.0",
    status: "enabled", installed_by: "dev-admin" },
];

const AUDIT: AuditEntry[] = [
  { id: 9, created_at: "2026-10-16T15:00:00Z", actor: "dev-admin",
    action: "studio.config.publish", entity: "config:branding/default@v2",
    entry_hash: "a".repeat(64), prev_hash: "b".repeat(64) },
  { id: 8, created_at: "2026-10-16T14:00:00Z", actor: "dev-configurator",
    action: "studio.install", entity: "pack:lab@2.0.0",
    entry_hash: "c".repeat(64), prev_hash: "a".repeat(64) },
];

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  const saved = (kind: string, key: string) => ({
    ...cfg(kind as StudioConfigObject["kind"], key, {}, 2, "draft"),
  });
  return {
    listConfigs: vi.fn(async (kind?: string) =>
      CONFIGS.filter((c) => !kind || c.kind === kind)),
    listPacks: vi.fn(async () => PACKS),
    listInstallations: vi.fn(async () => INSTALLATIONS),
    listAudit: vi.fn(async () => AUDIT),
    createConfigDraft: vi.fn(async (kind: string, key: string) => saved(kind, key)),
    updateConfigDraft: vi.fn(async (kind: string, key: string) => saved(kind, key)),
    publishConfig: vi.fn(async (kind: string, key: string) =>
      ({ ...cfg(kind as StudioConfigObject["kind"], key, {}, 2, "published") })),
    disableConfig: vi.fn(async () => ({})),
    rollbackConfig: vi.fn(async () => ({})),
    installPack: vi.fn(async () => INSTALLATIONS[0]),
    installationAction: vi.fn(async () => INSTALLATIONS[0]),
    ...overrides,
  } as unknown as ApiClient;
}

async function openTab(name: string) {
  fireEvent.click(screen.getByRole("tab", { name }));
}

describe("System Studio module", () => {
  it("renders all fourteen tabs and the overview content", async () => {
    render(<StudioPage client={stubClient()} />);
    for (const name of ["Overview", "Module packs", "Forms designer", "Workflows",
      "Terminology", "Navigation", "Branding", "Permissions", "Integration",
      "Agents (ADK)", "Compliance", "Dashboards",
      "Advanced (JSON)", "Audit"]) {
      expect(screen.getByRole("tab", { name })).toBeTruthy();
    }
    // overview: org profile from published branding, pack counts, audit
    expect(await screen.findByText("OpenCare Clinic")).toBeTruthy();
    expect(screen.getByText(/help@opencare\.example/)).toBeTruthy();
    expect(screen.getByText(/2 packs available/)).toBeTruthy();
    expect(screen.getByText("studio.config.publish")).toBeTruthy();
    expect(screen.getByText(/intake v1/)).toBeTruthy();
  });

  it("packs tab keeps the install / lifecycle surface", async () => {
    render(<StudioPage client={stubClient()} />);
    await openTab("Module packs");
    expect(await screen.findByText("clinical-core")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Install as draft" })).toBeTruthy();
    expect(screen.getByText("enabled")).toBeTruthy();
  });

  it("forms designer loads a form, adds an item, and saves a serialized draft", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Forms designer");
    expect(await screen.findByDisplayValue("Intake form")).toBeTruthy();
    expect(screen.getByDisplayValue("Chief complaint")).toBeTruthy();
    // preview renders the choice select read-only
    expect(screen.getByText("No pain")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Add item" }));
    const linkIds = screen.getAllByLabelText(/linkId/);
    expect(linkIds).toHaveLength(3);
    fireEvent.change(screen.getByLabelText("Item 3 text"), {
      target: { value: "Allergies" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => {
      expect(client.createConfigDraft).toHaveBeenCalled();
    });
    const [kind, key, payload] = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, string, Record<string, unknown>];
    expect(kind).toBe("form");
    expect(key).toBe("intake");
    const items = payload.item as Array<Record<string, unknown>>;
    expect(items).toHaveLength(3);
    expect(items[2]).toMatchObject({ linkId: "q3", text: "Allergies", type: "string" });
    expect(items[1].answerOption).toEqual([
      { valueCoding: { code: "none", display: "No pain" } },
    ]);
    expect(await screen.findByText(/Draft v2 saved/)).toBeTruthy();
  });

  it("workflows tab edits states/transitions and publishes via draft", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Workflows");
    const stateInput = await screen.findByLabelText("State 1 name");
    expect((stateInput as HTMLInputElement).value).toBe("booked");
    fireEvent.click(screen.getByRole("button", { name: "Publish" }));
    await waitFor(() => {
      expect(client.publishConfig).toHaveBeenCalledWith("workflow", "appointment-adt", 2);
    });
    // publishing an edited-from-published editor saves a draft first
    expect(client.createConfigDraft).toHaveBeenCalled();
    const payload = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0][2] as Record<string, unknown>;
    expect(payload.states).toEqual(["booked", "arrived", "fulfilled"]);
    expect(payload.transitions).toEqual([{ from: "booked", to: "arrived" }]);
  });

  it("terminology tab edits a reference range into the serialized payload", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Terminology");
    expect(await screen.findByDisplayValue("Hemoglobin")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Concept 1 reference low"), {
      target: { value: "11.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(client.createConfigDraft).toHaveBeenCalled());
    const payload = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0][2] as { concepts: Array<Record<string, unknown>> };
    expect(payload.concepts[0].referenceRange).toEqual(
      { unit: "g/dL", low: 11.5, high: 15.5 });
  });

  it("navigation tab surfaces the server's modules-only 400 verbatim", async () => {
    const client = stubClient({
      createConfigDraft: vi.fn(async () => {
        throw new ApiError(400,
          "Navigation menu items may only reference modules: pages and forms live inside modules and cannot be menu entries (System Studio rule).");
      }),
    });
    render(<StudioPage client={client} />);
    await openTab("Navigation");
    expect(await screen.findByDisplayValue("Scheduling")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("may only reference modules");
    expect(alert.textContent).toContain("System Studio rule");
  });

  it("branding tab previews the org header live and saves the profile", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Branding");
    const nameInput = await screen.findByLabelText("Organisation name");
    expect((nameInput as HTMLInputElement).value).toBe("OpenCare Clinic");
    fireEvent.change(nameInput, { target: { value: "Mayo-style Clinic" } });
    // live preview header follows the edit
    expect(screen.getByText("Mayo-style Clinic")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Primary colour hex"), {
      target: { value: "#003087" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(client.createConfigDraft).toHaveBeenCalled());
    const [kind, key, payload] = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, string, Record<string, unknown>];
    expect([kind, key]).toEqual(["branding", "default"]);
    expect(payload).toMatchObject({ orgName: "Mayo-style Clinic", primaryColor: "#003087" });
  });

  it("permissions tab shows the defaults matrix and saves toggles", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Permissions");
    const verifyBox = await screen.findByLabelText("lab.verify for configurator");
    expect((verifyBox as HTMLInputElement).checked).toBe(false); // built-in default
    expect((screen.getByLabelText("lab.verify for clinician") as HTMLInputElement)
      .checked).toBe(true);
    fireEvent.click(verifyBox);
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(client.createConfigDraft).toHaveBeenCalled());
    const [kind, key, payload] = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0] as [string, string, { capabilities: Record<string, Record<string, boolean>> }];
    expect([kind, key]).toEqual(["permissions", "role-matrix"]);
    expect(payload.capabilities["lab.verify"].configurator).toBe(true);
    expect(payload.capabilities["lab.verify"].patient).toBe(false);
  });

  it("integration tab edits viewer.baseUrl as a key/value row", async () => {
    const client = stubClient();
    render(<StudioPage client={client} />);
    await openTab("Integration");
    const valueInput = await screen.findByLabelText("Setting 2 value");
    expect((valueInput as HTMLInputElement).value).toBe("https://viewer.ohif.org");
    fireEvent.change(valueInput, { target: { value: "https://pacs.example.org/viewer" } });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(client.createConfigDraft).toHaveBeenCalled());
    const payload = (client.createConfigDraft as ReturnType<typeof vi.fn>)
      .mock.calls[0][2] as { viewer: Record<string, string> };
    expect(payload.viewer.baseUrl).toBe("https://pacs.example.org/viewer");
    expect(payload.viewer.name).toBe("OHIF viewer (public demo)");
  });

  it("advanced tab keeps the raw JSON editor", async () => {
    render(<StudioPage client={stubClient()} />);
    await openTab("Advanced (JSON)");
    expect(await screen.findByLabelText("Config payload JSON")).toBeTruthy();
    expect(screen.getByText("form/intake")).toBeTruthy();
  });

  it("audit tab renders entries with hash prefixes", async () => {
    render(<StudioPage client={stubClient()} />);
    await openTab("Audit");
    expect(await screen.findByText("studio.config.publish")).toBeTruthy();
    expect(screen.getByText("config:branding/default@v2")).toBeTruthy();
    expect(screen.getByText("aaaaaaaaaaaa…")).toBeTruthy();
    expect(screen.getByText("pack:lab@2.0.0")).toBeTruthy();
  });
});
