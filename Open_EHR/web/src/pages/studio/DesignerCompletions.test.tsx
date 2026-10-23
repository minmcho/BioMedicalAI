import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { DashboardsTab } from "./DashboardsTab";
import { FormsTab } from "./FormsTab";
import { PackBuilder } from "./PackBuilder";
import { WorkflowsTab } from "./WorkflowsTab";

// Designer completions (Studio parity rows 7, 16, 19, 28, 39): form
// palette alignment, workflow canvas, dashboard authoring, pack
// builder. Stub clients answer at the ApiClient boundary, like the
// other Studio tab tests.

afterEach(cleanup);

function cfg(
  kind: StudioConfigObject["kind"],
  key: string,
  payload: Record<string, unknown>,
  version = 1,
  status: StudioConfigObject["status"] = "draft",
): StudioConfigObject {
  return {
    id: version * 7 + key.length, kind, key, version, status, payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
  };
}

// Mirror of backend/app/configstore.py FORM_ITEM_TYPES —
// ("group", "display", "boolean", "decimal", "integer", "string",
// "text", "date", "dateTime", "time", "choice", "open-choice",
// "quantity"). Hardcoded here so a palette drift in either direction
// (offering a type the validator refuses, or dropping one it accepts)
// fails this test.
const EXPECTED_FORM_TYPES = [
  "group", "display", "boolean", "decimal", "integer", "string",
  "text", "date", "dateTime", "time", "choice", "open-choice", "quantity",
];

describe("FormsTab palette completion", () => {
  it("offers exactly the validate_form field types in the palette", async () => {
    const client = {
      listConfigs: vi.fn(async () => []),
    } as unknown as ApiClient;
    render(<FormsTab client={client} />);
    const select = (await screen.findByLabelText(
      "Item 1 type",
    )) as HTMLSelectElement;
    const offered = [...select.options].map((o) => o.value);
    expect(offered).toEqual(EXPECTED_FORM_TYPES);
  });

  it("edits valueSet and open-choice options and serializes both", async () => {
    const createConfigDraft = vi.fn(async () =>
      cfg("form", "intake", {}, 1, "draft"));
    const client = {
      listConfigs: vi.fn(async () => []),
      createConfigDraft,
    } as unknown as ApiClient;
    render(<FormsTab client={client} />);
    // Select the new key first: the shell (re)initialises the editor
    // when the key changes, so field edits come after key selection.
    await screen.findByLabelText("Item 1 type");
    fireEvent.change(screen.getByLabelText("form config key"), {
      target: { value: "__new__" },
    });
    fireEvent.change(screen.getByLabelText("New config key"), {
      target: { value: "intake" },
    });
    const typeSelect = screen.getByLabelText("Item 1 type");
    fireEvent.change(typeSelect, { target: { value: "open-choice" } });
    fireEvent.change(screen.getByLabelText("Item 1 valueSet"), {
      target: { value: "ValueSet/yes-no" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add option" }));
    fireEvent.change(screen.getByLabelText("Item 1 option 1 code"), {
      target: { value: "yes" },
    });
    fireEvent.change(screen.getByLabelText("Item 1 option 1 display"), {
      target: { value: "Yes" },
    });
    fireEvent.change(screen.getByLabelText("Item 1 text"), {
      target: { value: "Willing?" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(createConfigDraft).toHaveBeenCalled());
    const [, key, payload] = createConfigDraft.mock.calls[0] as unknown as [
      string, string, Record<string, unknown>,
    ];
    expect(key).toBe("intake");
    const items = payload.item as Array<Record<string, unknown>>;
    expect(items[0].type).toBe("open-choice");
    expect(items[0].valueSet).toBe("ValueSet/yes-no");
    expect(items[0].answerOption).toEqual([
      { valueCoding: { code: "yes", display: "Yes" } },
    ]);
  });
});

describe("WorkflowsTab canvas", () => {
  it("renders stage names in canvas view and syncs a canvas click to the list selection", async () => {
    const workflow = cfg("workflow", "appointment-adt", {
      states: ["booked", "arrived", "fulfilled"],
      transitions: [
        { from: "booked", to: "arrived", action: "check in" },
        { from: "arrived", to: "fulfilled", action: "complete" },
      ],
      initial: "booked",
      terminal: ["fulfilled"],
    });
    const client = {
      listConfigs: vi.fn(async () => [workflow]),
    } as unknown as ApiClient;
    render(<WorkflowsTab client={client} />);
    expect(
      (await screen.findByLabelText("State 1 name")) as HTMLInputElement,
    ).toBeTruthy();
    expect(
      (screen.getByLabelText("State 1 name") as HTMLInputElement).value,
    ).toBe("booked");

    fireEvent.click(screen.getByRole("button", { name: "Canvas view" }));
    // Stage names + transition labels render from the same draft.
    expect(screen.getByText("arrived")).toBeTruthy();
    expect(screen.getByText("fulfilled")).toBeTruthy();
    expect(screen.getByText("check in")).toBeTruthy();
    expect(
      screen.getByText(/Canvas is a view of the same draft/),
    ).toBeTruthy();

    fireEvent.click(
      screen.getByRole("button", { name: "Select state arrived" }),
    );
    // Back in the list, state 2's row is the selected one.
    const state2 = await screen.findByLabelText("State 2 name");
    expect(state2.closest("p")?.getAttribute("data-selected")).toBe("true");

    // …and the selection survives back into the canvas (shared state).
    fireEvent.click(screen.getByRole("button", { name: "Canvas view" }));
    expect(
      screen
        .getByRole("button", { name: "Select state arrived" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });
});

describe("DashboardsTab author mode", () => {
  it("adds a widget row and Save calls createConfigDraft with kind dashboard and the composed payload", async () => {
    const createConfigDraft = vi.fn(async () =>
      cfg("dashboard", "ops-board", {}, 3, "draft"));
    const client = {
      listConfigs: vi.fn(async () => []),
      createConfigDraft,
      publishConfig: vi.fn(async () => cfg("dashboard", "ops-board", {}, 3, "published")),
    } as unknown as ApiClient;
    render(<DashboardsTab client={client} />);
    fireEvent.click(
      await screen.findByRole("button", { name: "Author dashboard" }),
    );
    fireEvent.change(screen.getByLabelText("Dashboard key"), {
      target: { value: "ops-board" },
    });
    fireEvent.change(screen.getByLabelText("Dashboard title"), {
      target: { value: "Ops board" },
    });
    // Add a second widget row, drop the blank first row, compose in it.
    fireEvent.click(screen.getByRole("button", { name: "Add widget" }));
    expect(screen.getByLabelText("Widget 2 title")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove widget 1" }));
    fireEvent.change(screen.getByLabelText("Widget 1 title"), {
      target: { value: "Patients in beds" },
    });
    fireEvent.change(screen.getByLabelText("Widget 1 endpoint"), {
      target: { value: "/inpatient/census" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => expect(createConfigDraft).toHaveBeenCalled());
    expect(createConfigDraft).toHaveBeenCalledWith(
      "dashboard",
      "ops-board",
      expect.objectContaining({
        title: "Ops board",
        widgets: [
          expect.objectContaining({
            id: "widget-1",
            title: "Patients in beds",
            endpoint: "/inpatient/census",
            display: "total",
          }),
        ],
      }),
    );
    expect(await screen.findByText(/Draft v3 saved/)).toBeTruthy();
  });
});

describe("PackBuilder", () => {
  it("submits a manifest whose api_endpoints all carry enabled:false", async () => {
    const request = vi.fn(async () => ({
      id: 9, name: "solo-profile", version: "1.0.0", manifest: {},
    }));
    const client = {
      listConfigs: vi.fn(async (kind?: string) =>
        kind === "form" ? [cfg("form", "intake", {})] : []),
      request,
    } as unknown as ApiClient;
    render(<PackBuilder client={client} />);
    fireEvent.change(screen.getByLabelText("Pack name"), {
      target: { value: "solo-profile" },
    });
    fireEvent.change(screen.getByLabelText("Pack description"), {
      target: { value: "Solo practice profile" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add endpoint" }));
    fireEvent.change(screen.getByLabelText("Endpoint 1 path"), {
      target: { value: "/solo/summary" },
    });
    // The artefact picker's key select is populated from listConfigs.
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Artefact key") as HTMLSelectElement).value,
      ).toBe("intake"));
    fireEvent.click(screen.getByRole("button", { name: "Add artefact" }));
    expect(screen.getByText("form/intake")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Create draft pack" }));
    await waitFor(() => expect(request).toHaveBeenCalled());
    const [path, init] = request.mock.calls[0] as unknown as [
      string, { method: string; body: string },
    ];
    expect(path).toBe("/studio/packs");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body) as {
      name: string;
      version: string;
      manifest: {
        description: string;
        api_endpoints: Array<{ method: string; path: string; enabled: boolean }>;
        artefacts: Array<{ kind: string; key: string }>;
      };
    };
    expect(body.name).toBe("solo-profile");
    expect(body.manifest.description).toBe("Solo practice profile");
    expect(body.manifest.api_endpoints).toEqual([
      { method: "GET", path: "/solo/summary", enabled: false },
    ]);
    expect(body.manifest.api_endpoints.every((e) => e.enabled === false)).toBe(true);
    expect(body.manifest.artefacts).toEqual([{ kind: "form", key: "intake" }]);
    expect(
      await screen.findByText(/Next: install → approve → attach → enable in Module packs/),
    ).toBeTruthy();
  });
});
