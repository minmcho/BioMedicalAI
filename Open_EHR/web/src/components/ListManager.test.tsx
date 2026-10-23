import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, type ApiClient } from "../api/client";
import {
  getManagedList,
  listRegistry,
  putListDraft,
} from "../api/lists";
import { ListManager } from "./ListManager";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

const NOTICE =
  "Draft only: these changes take effect only when a human publishes " +
  "this draft in System Studio. Consumers read the published list and " +
  "are unchanged until then.";

const PUBLISHED = [
  { code: "CT", display: "Computed Tomography", active: true },
  { code: "MR", display: "Magnetic Resonance Imaging", active: true },
];

function managedPayload(overrides: Record<string, unknown> = {}) {
  return {
    kind: "terminology",
    listKey: "imaging-modalities",
    label: "Imaging modalities",
    module: "radiology",
    managerJobRoles: ["radiographer", "department-head"],
    active: true,
    caller_can_manage: true,
    manage_basis: "capability:lists.manage",
    published: { version: 2, concepts: PUBLISHED.map((c) => ({ ...c })) },
    draft: null,
    notice: NOTICE,
    ...overrides,
  };
}

/** Stub ApiClient whose `request` routes /lists calls. */
function listsClient(
  overrides: {
    getPayload?: unknown;
    putImpl?: (path: string, init?: RequestInit) => Promise<unknown>;
  } = {},
): ApiClient & { request: ReturnType<typeof vi.fn> } {
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (init?.method === "PUT") {
      if (overrides.putImpl) return overrides.putImpl(path, init);
      const body = JSON.parse(String(init.body)) as {
        concepts: Array<Record<string, unknown>>;
      };
      return {
        kind: "terminology",
        listKey: "imaging-modalities",
        label: "Imaging modalities",
        module: "radiology",
        status: "draft",
        draft_version: 3,
        published_version: 2,
        manage_basis: "capability:lists.manage",
        diff: { added: [], updated: [], deactivated: [] },
        diff_basis: "published",
        concepts: body.concepts,
        notice: NOTICE,
      };
    }
    if (path === "/lists") {
      return {
        registry: { kind: "terminology", key: "manageable-lists", version: 1 },
        items: [],
        total: 0,
        excluded_lists: [],
        notes: null,
      };
    }
    return overrides.getPayload ?? managedPayload();
  });
  return { request } as unknown as ApiClient & {
    request: ReturnType<typeof vi.fn>;
  };
}

/** Render ListManager by invoking it through a wrapper that passes
 *  the props object directly. (The list key travels as `listKey`:
 *  React reserves `key` as a special prop and strips it from JSX.) */
function renderListManager(client: ApiClient) {
  function Wrapper() {
    return ListManager({
      client,
      kind: "terminology",
      listKey: "imaging-modalities",
      label: "Imaging modalities",
    });
  }
  return render(<Wrapper />);
}

async function openEditor(client: ApiClient) {
  renderListManager(client);
  const button = await screen.findByRole("button", {
    name: "Manage Imaging modalities",
  });
  fireEvent.click(button);
  expect(
    await screen.findByText(/Published v2/),
  ).toBeTruthy();
}

describe("api/lists client functions", () => {
  it("listRegistry GETs /lists", async () => {
    const client = listsClient();
    await listRegistry(client);
    expect(client.request).toHaveBeenCalledWith("/lists");
  });

  it("getManagedList encodes kind/key into the path", async () => {
    const client = listsClient();
    await getManagedList(client, "terminology", "imaging-modalities");
    expect(client.request).toHaveBeenCalledWith(
      "/lists/terminology/imaging-modalities",
    );
  });

  it("putListDraft PUTs the full concept list to the draft endpoint", async () => {
    const client = listsClient();
    await putListDraft(client, "terminology", "imaging-modalities", PUBLISHED);
    expect(client.request).toHaveBeenCalledWith(
      "/lists/terminology/imaging-modalities/draft",
      { method: "PUT", body: JSON.stringify({ concepts: PUBLISHED }) },
    );
  });
});

describe("ListManager", () => {
  it("renders nothing when caller_can_manage is false", async () => {
    const client = listsClient({
      getPayload: managedPayload({
        caller_can_manage: false,
        manage_basis: null,
      }),
    });
    renderListManager(client);
    // Wait for the fetch to settle, then assert invisibility.
    await waitFor(() => expect(client.request).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("button", { name: /Manage/ })).toBeNull();
    expect(document.body.textContent ?? "").not.toMatch(/Manage Imaging/);
  });

  it("opens the editor with code + display + active flag per concept", async () => {
    const client = listsClient();
    await openEditor(client);
    expect(screen.getByText("CT")).toBeTruthy();
    expect(screen.getByLabelText("Display for CT")).toBeTruthy();
    expect(
      (screen.getByLabelText("Display for CT") as HTMLInputElement).value,
    ).toBe("Computed Tomography");
    expect(screen.getAllByText("Active").length).toBeGreaterThan(0);
    expect(screen.getByText(NOTICE)).toBeTruthy();
  });

  it("edits a display, deactivates a published code (never hard-deletes) and saves the draft with the backend notice", async () => {
    const client = listsClient();
    await openEditor(client);
    // Published codes offer Deactivate only — no Remove button.
    expect(screen.queryByRole("button", { name: "Remove CT" })).toBeNull();
    fireEvent.change(screen.getByLabelText("Display for CT"), {
      target: { value: "CT scan" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Deactivate MR" }));
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    await waitFor(() => {
      expect(client.request).toHaveBeenCalledWith(
        "/lists/terminology/imaging-modalities/draft",
        expect.objectContaining({ method: "PUT" }),
      );
    });
    const putCall = client.request.mock.calls.find(
      (c) => (c[1] as RequestInit | undefined)?.method === "PUT",
    );
    const sent = JSON.parse(String((putCall?.[1] as RequestInit).body)) as {
      concepts: Array<{ code: string; display?: string; active?: boolean }>;
    };
    expect(sent.concepts).toEqual([
      { code: "CT", display: "CT scan", active: true },
      { code: "MR", display: "Magnetic Resonance Imaging", active: false },
    ]);
    // Success shows the backend's draft-only notice verbatim.
    expect(await screen.findByText(/Draft saved/)).toBeTruthy();
    expect(screen.getByText(NOTICE)).toBeTruthy();
  });

  it("adds a new concept; a draft-only code can be removed outright", async () => {
    const client = listsClient();
    await openEditor(client);
    fireEvent.change(screen.getByLabelText("New code"), {
      target: { value: "US" },
    });
    fireEvent.change(screen.getByLabelText("New display"), {
      target: { value: "Ultrasound" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add concept" }));
    expect(await screen.findByText("US")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Remove US" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Remove US" }));
    expect(screen.queryByText("US")).toBeNull();
  });

  it("starts from the draft concepts when a draft exists", async () => {
    const client = listsClient({
      getPayload: managedPayload({
        draft: {
          version: 3,
          created_by: "dev-admin",
          updated_by: "dev-admin",
          concepts: [
            { code: "CT", display: "CT (draft edit)", active: true },
            { code: "MR", display: "Magnetic Resonance Imaging", active: false },
          ],
        },
      }),
    });
    await openEditor(client);
    expect(
      (screen.getByLabelText("Display for CT") as HTMLInputElement).value,
    ).toBe("CT (draft edit)");
    expect(screen.getByText(/Draft v3 pending/)).toBeTruthy();
    expect(screen.getByText("Inactive")).toBeTruthy();
  });

  it("surfaces the server 422 detail verbatim when a save is refused", async () => {
    const detail =
      "Published code(s) ['MR'] are missing from the submitted list. " +
      "Published values are never hard-deleted because records " +
      "reference them: to delete one, keep its code in the list and " +
      "mark it \"active\": false (deactivation).";
    const client = listsClient({
      putImpl: async () => {
        throw new ApiError(422, detail);
      },
    });
    await openEditor(client);
    fireEvent.click(screen.getByRole("button", { name: "Save draft" }));
    expect(await screen.findByText(detail)).toBeTruthy();
  });
});
