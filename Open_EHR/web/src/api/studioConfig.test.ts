import { describe, expect, it, vi } from "vitest";
import { ApiClient } from "./client";
import type { StudioConfigObject } from "./types";
import { configActions, groupConfigs } from "../pages/StudioPage";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fakeClient(fetchFn: ReturnType<typeof vi.fn>): ApiClient {
  return new ApiClient("http://localhost:8000", () => "admin-token", fetchFn as never);
}

const draft: StudioConfigObject = {
  id: 7,
  kind: "form",
  key: "intake",
  version: 2,
  status: "draft",
  payload: { item: [] },
  created_by: "dev-admin",
  updated_by: "dev-admin",
  created_at: null,
  updated_at: null,
};

describe("ApiClient studio config methods", () => {
  it("creates a draft with a POST of the payload JSON", async () => {
    const fetchFn = vi.fn(async () => jsonResponse(draft, 201));
    const client = fakeClient(fetchFn);
    const created = await client.createConfigDraft("form", "intake", { item: [] });
    expect(created.version).toBe(2);
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/studio/config/form/intake");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({ item: [] });
  });

  it("updates a draft with a PUT to the version URL", async () => {
    const fetchFn = vi.fn(async () => jsonResponse(draft));
    const client = fakeClient(fetchFn);
    await client.updateConfigDraft("form", "intake", 2, { item: [] });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/studio/config/form/intake/versions/2");
    expect(init.method).toBe("PUT");
  });

  it("publishes and disables via the version action URLs", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ ...draft, status: "published" }));
    const client = fakeClient(fetchFn);
    await client.publishConfig("form", "intake", 2);
    await client.disableConfig("form", "intake", 2);
    const urls = fetchFn.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual([
      "http://localhost:8000/studio/config/form/intake/versions/2/publish",
      "http://localhost:8000/studio/config/form/intake/versions/2/disable",
    ]);
  });

  it("rolls back with the target version in the body", async () => {
    const fetchFn = vi.fn(async () => jsonResponse({ ...draft, version: 3, status: "published" }, 201));
    const client = fakeClient(fetchFn);
    await client.rollbackConfig("form", "intake", 1);
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://localhost:8000/studio/config/form/intake/rollback");
    expect(JSON.parse(init.body as string)).toEqual({ version: 1 });
  });

  it("lists configs with and without a kind filter, and reads history/effective", async () => {
    const fetchFn = vi.fn(async () => jsonResponse([]));
    const client = fakeClient(fetchFn);
    await client.listConfigs();
    await client.listConfigs("workflow");
    await client.configHistory("workflow", "appointment-adt");
    await client.effectiveConfigs("branding");
    const urls = fetchFn.mock.calls.map((c) => (c as unknown as [string])[0]);
    expect(urls).toEqual([
      "http://localhost:8000/studio/config",
      "http://localhost:8000/studio/config?kind=workflow",
      "http://localhost:8000/studio/config/workflow/appointment-adt",
      "http://localhost:8000/studio/config/branding/effective",
    ]);
  });
});

describe("groupConfigs / configActions", () => {
  it("groups versions per object, ascending, objects sorted", () => {
    const groups = groupConfigs([
      { ...draft, id: 2, version: 2 },
      { ...draft, id: 1, version: 1, status: "superseded" },
      { ...draft, id: 3, kind: "branding", key: "default", version: 1, status: "published" },
    ]);
    expect(groups.map((g) => `${g.kind}/${g.key}`)).toEqual(["branding/default", "form/intake"]);
    expect(groups[1].versions.map((v) => v.version)).toEqual([1, 2]);
  });

  it("offers lifecycle actions matching each status", () => {
    expect(configActions("draft")).toEqual(["edit", "publish"]);
    expect(configActions("published")).toEqual(["disable"]);
    expect(configActions("disabled")).toEqual(["publish", "rollback"]);
    expect(configActions("superseded")).toEqual(["rollback"]);
  });
});
