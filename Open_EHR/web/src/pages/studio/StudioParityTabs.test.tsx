import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import type { StudioConfigObject } from "../../api/types";
import { AiProvidersTab } from "./AiProvidersTab";
import { ApprovalsTab } from "./ApprovalsTab";
import { DataViewsTab } from "./DataViewsTab";
import { DatabaseTab } from "./DatabaseTab";
import { OrganisationTab } from "./OrganisationTab";
import { RegistryTab } from "./RegistryTab";

// Studio parity tabs (Workstream 10): AI providers, approvals
// inbox, organisation read model, data views, database platform and
// the endpoint/tool registry. Stub clients only (the network
// boundary); the /ai-providers and /studio-ops backends are sibling
// builds, so these tests pin the CONTRACT shapes the tabs code
// against, including the honesty lines each surface must carry.

afterEach(cleanup);

// ---- AI Providers ----

const PROVIDER_DEFINITION = {
  key: "local-ollama",
  version: 1,
  status: "draft",
  payload: {
    name: "Local Ollama",
    type: "ollama",
    model: "llama3.2:3b",
    baseUrl: "http://127.0.0.1:11434",
    department: "All departments",
    apiKeyRef: "",
    enabled: true,
  },
};

const EFFECTIVE_PROVIDERS = [
  {
    key: "local-ollama", name: "Local Ollama", type: "ollama",
    model: "llama3.2:3b", baseUrl: "http://127.0.0.1:11434",
    department: "All departments", enabled: true, keyConfigured: null,
  },
  {
    key: "openai-main", name: "OpenAI main", type: "openai-compatible",
    model: "gpt-4o", baseUrl: "https://api.openai.com/v1",
    department: "Radiology", enabled: true, keyConfigured: true,
  },
  {
    key: "anthropic-main", name: "Anthropic main", type: "anthropic",
    model: "claude-sonnet", baseUrl: "https://api.anthropic.com",
    department: "Pathology", enabled: false, keyConfigured: false,
  },
];

function aiClient(overrides: {
  definitions?: unknown;
  effective?: unknown;
  calls?: string[];
} = {}): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      overrides.calls?.push(path);
      if (path === "/ai-providers/definitions") {
        return overrides.definitions ?? { items: [PROVIDER_DEFINITION] };
      }
      if (path === "/ai-providers/effective") {
        return (
          overrides.effective ??
          { items: EFFECTIVE_PROVIDERS, note: "Published providers only." }
        );
      }
      if (path.includes("/publish")) return {};
      if (path.startsWith("/ai-providers/definitions/")) return {};
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
}

describe("AiProvidersTab", () => {
  it("renders definitions, effective providers and the key-reference hint", async () => {
    render(<AiProvidersTab client={aiClient()} />);
    expect((await screen.findAllByText("Local Ollama")).length)
      .toBeGreaterThanOrEqual(2);
    expect(screen.getByText("OpenAI main")).toBeTruthy();
    expect(screen.getAllByText("llama3.2:3b").length)
      .toBeGreaterThanOrEqual(2);
    // keyConfigured: true → Yes, false → No, null → Not required.
    expect(screen.getByText("Yes")).toBeTruthy();
    expect(screen.getByText("No")).toBeTruthy();
    expect(screen.getByText("Not required")).toBeTruthy();
    expect(screen.getByText("Published providers only.")).toBeTruthy();
    expect(
      screen.getAllByText(/API keys are never entered here/).length,
    ).toBeGreaterThanOrEqual(1);
  });

  it("tolerates a plain-array definitions response and shows the empty effective state", async () => {
    render(
      <AiProvidersTab
        client={aiClient({
          definitions: [PROVIDER_DEFINITION],
          effective: { items: [] },
        })}
      />,
    );
    expect((await screen.findAllByText("Local Ollama")).length)
      .toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText("No published providers are effective yet."),
    ).toBeTruthy();
  });

  it("shows the empty state when there are no definitions", async () => {
    render(
      <AiProvidersTab
        client={aiClient({ definitions: { items: [] }, effective: { items: [] } })}
      />,
    );
    expect(
      await screen.findByText("No AI provider definitions yet."),
    ).toBeTruthy();
  });

  it("publishes a draft definition through the versioned publish route", async () => {
    const calls: string[] = [];
    render(<AiProvidersTab client={aiClient({ calls })} />);
    await screen.findAllByText("Local Ollama");
    fireEvent.click(screen.getByRole("button", { name: "Publish v1" }));
    await waitFor(() =>
      expect(
        calls.includes(
          "/ai-providers/definitions/local-ollama/versions/1/publish",
        ),
      ).toBe(true),
    );
  });
});

// ---- Approvals ----

const INBOX = {
  items: [
    {
      source: "packs", id: "inst-3", title: "Oncology pack awaiting approval",
      detail: "Installed as draft by dev-admin",
      created_at: "2026-10-18T09:00:00Z", action: "Review in Module packs",
    },
    {
      source: "configs", id: "form/intake@v2", title: "Form draft awaiting publish",
      detail: "intake v2 is a draft",
      created_at: "2026-10-18T10:00:00Z", action: "Review in Forms designer",
    },
  ],
  total: 2,
  by_source: { packs: 1, configs: 1 },
};

function inboxClient(response: unknown): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/studio-ops/inbox") return response;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
}

describe("ApprovalsTab", () => {
  it("renders the inbox grouped by source with counts and action hints", async () => {
    render(<ApprovalsTab client={inboxClient(INBOX)} />);
    expect(
      await screen.findByText("Oncology pack awaiting approval"),
    ).toBeTruthy();
    expect(screen.getByText("Form draft awaiting publish")).toBeTruthy();
    expect(screen.getByText("packs (1)")).toBeTruthy();
    expect(screen.getByText("configs (1)")).toBeTruthy();
    expect(screen.getByText("Review in Module packs")).toBeTruthy();
    expect(screen.getByText("packs: 1")).toBeTruthy();
  });

  it("shows the empty state when nothing is pending", async () => {
    render(
      <ApprovalsTab
        client={inboxClient({ items: [], total: 0, by_source: {} })}
      />,
    );
    expect(
      await screen.findByText("Nothing is awaiting approval right now."),
    ).toBeTruthy();
  });
});

// ---- Organisation ----

const ORGANISATION = {
  facilities: [
    {
      name: "General Hospital",
      units: [
        { name: "ICU", beds: 12 },
        { name: "Ward A", beds: 18 },
      ],
    },
  ],
  totals: { facilities: 1, units: 2, beds: 30 },
  staff: [{ id: "s1" }, { id: "s2" }],
  accounts: { total: 9 },
  edit_surfaces: ["Locations editor", "People & workforce", "Accounts"],
  notes: ["Read model only — counts are live."],
};

function orgClient(response: unknown): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/studio-ops/organisation") return response;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
}

describe("OrganisationTab", () => {
  it("renders totals, the facilities→units tree and the edit surfaces", async () => {
    render(<OrganisationTab client={orgClient(ORGANISATION)} />);
    expect(await screen.findByText("General Hospital")).toBeTruthy();
    expect(screen.getByText(/ICU — 12 beds/)).toBeTruthy();
    expect(screen.getByText(/Ward A — 18 beds/)).toBeTruthy();
    expect(screen.getByText("30")).toBeTruthy();
    expect(screen.getByText("Locations editor")).toBeTruthy();
    expect(screen.getByText(/Read model only/)).toBeTruthy();
  });

  it("shows the empty state when no facilities are published", async () => {
    render(
      <OrganisationTab
        client={orgClient({
          facilities: [], totals: {}, staff: [], accounts: [],
          edit_surfaces: [], notes: [],
        })}
      />,
    );
    expect(
      await screen.findByText(
        "No facilities in the published locations config.",
      ),
    ).toBeTruthy();
  });
});

// ---- Data views ----

function dataViewsClient(response: unknown): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/studio-ops/data-views") return response;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
}

describe("DataViewsTab", () => {
  it("renders registry rows with column and row counts and the no-SQL note", async () => {
    render(
      <DataViewsTab
        client={dataViewsClient({
          items: [
            {
              name: "Census view", kind: "dataset",
              description: "Nightly census snapshot",
              columns: ["unit", "beds", "occupied"], row_count: 42,
              source: "warehouse",
            },
            {
              name: "Lake events", kind: "lake-zone", description: "",
              columns: 5, row_count: null, source: "lake",
            },
          ],
        })}
      />,
    );
    expect(await screen.findByText("Census view")).toBeTruthy();
    expect(screen.getByText("Lake events")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/no SQL editor, by design/)).toBeTruthy();
  });

  it("shows the empty state when no views are registered", async () => {
    render(<DataViewsTab client={dataViewsClient({ items: [] })} />);
    expect(
      await screen.findByText("No governed data views are registered yet."),
    ).toBeTruthy();
  });
});

// ---- Database ----

const DB_ENGINES = {
  engines: [
    {
      engine: "postgres", display_name: "PostgreSQL 16",
      sqlalchemy_dialect: "postgresql+psycopg",
      driver_package: "psycopg[binary]",
      verification: "live-verified (conformance subset)",
    },
    {
      engine: "mysql", display_name: "MySQL 8",
      sqlalchemy_dialect: "mysql+pymysql", driver_package: "pymysql",
      verification: "dialect-only — live NOT RUN",
    },
  ],
  total: 2,
};

const DB_PROFILE = {
  id: 1, kind: "dbplatform", key: "primary-postgres", version: 1,
  status: "draft",
  payload: {
    key: "primary-postgres", engine: "postgres", host: "db.internal",
    port: 5432, database: "opencare", adapter: "sqlalchemy",
    vault_key_name: "prod/db/password",
  },
  created_by: "system", updated_by: "system",
  created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
};

const DB_EXTENSION = {
  id: 2, kind: "dbplatform-schema", key: "site-extensions", version: 1,
  status: "draft",
  payload: {
    key: "site-extensions",
    extensions: [
      {
        table: "ext_site_flags", column: "legacy_code", type: "string",
        purpose: "Local legacy identifier",
      },
    ],
  },
  created_by: "system", updated_by: "system",
  created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
};

function dbClient(overrides: {
  profiles?: unknown;
  extensions?: unknown;
} = {}): ApiClient {
  return {
    request: vi.fn(async (path: string) => {
      if (path === "/dbplatform/engines") return DB_ENGINES;
      if (path === "/dbplatform/profiles") {
        return overrides.profiles ?? { items: [DB_PROFILE] };
      }
      if (path === "/dbplatform/schema") {
        return { tables: [{ name: "fhir_resources", source: "app" }], total: 42 };
      }
      if (path === "/dbplatform/schema/extensions") {
        return overrides.extensions ?? { items: [DB_EXTENSION] };
      }
      if (path.startsWith("/dbplatform/ddl/")) {
        return {
          engine: "postgres", variant: "both",
          ddl: "CREATE TABLE demo (id INTEGER);",
        };
      }
      if (path.includes("/publish")) return DB_PROFILE;
      throw new Error(`unexpected ${path}`);
    }),
  } as unknown as ApiClient;
}

describe("DatabaseTab", () => {
  it("renders the engine matrix, profiles, schema summary and DDL", async () => {
    render(<DatabaseTab client={dbClient()} />);
    expect(await screen.findByText("PostgreSQL 16")).toBeTruthy();
    expect(screen.getByText("psycopg[binary]")).toBeTruthy();
    expect(screen.getByText("live-verified (conformance subset)"))
      .toBeTruthy();
    expect(screen.getByText("primary-postgres")).toBeTruthy();
    expect(screen.getByText("prod/db/password")).toBeTruthy();
    expect(screen.getByText("42")).toBeTruthy();
    expect(screen.getByText(/ext_site_flags\.legacy_code/)).toBeTruthy();
    expect(
      await screen.findByText("CREATE TABLE demo (id INTEGER);"),
    ).toBeTruthy();
    expect(screen.getAllByText(/vault key NAME only/).length)
      .toBeGreaterThanOrEqual(1);
  });

  it("shows empty states when there are no profiles or extensions", async () => {
    render(
      <DatabaseTab
        client={dbClient({ profiles: { items: [] }, extensions: { items: [] } })}
      />,
    );
    expect(
      await screen.findByText(
        "No connection profiles yet. Create a draft below.",
      ),
    ).toBeTruthy();
    expect(screen.getByText("No schema-extension drafts yet.")).toBeTruthy();
  });
});

// ---- Registry ----

function regCfg(
  kind: string,
  key: string,
  payload: Record<string, unknown>,
  version = 1,
  status: "draft" | "published" = "draft",
): StudioConfigObject {
  return {
    id: version * 11 + key.length,
    kind, key, version, status, payload,
    created_by: "system", updated_by: "system",
    created_at: "2026-10-18T00:00:00Z", updated_at: "2026-10-18T00:00:00Z",
  } as unknown as StudioConfigObject;
}

const ENDPOINT_CFG = regCfg("endpoint", "patient-intake-api", {
  title: "Patient intake API", method: "POST", path: "/intake/patients",
  capability: "intake.work", backing: "service", enabled: false,
});

const TOOL_CFG = regCfg(
  "tool",
  "fhir-lookup",
  {
    title: "FHIR lookup tool", kind: "rest",
    configRef: "integration/fhir-main", enabled: false,
  },
  1,
  "published",
);

function registryClient(lists: { endpoint?: unknown[]; tool?: unknown[] } = {}) {
  const publishConfig = vi.fn(async () => ({}));
  const client = {
    listConfigs: vi.fn(async (kind?: string) => {
      if (kind === "endpoint") return lists.endpoint ?? [ENDPOINT_CFG];
      if (kind === "tool") return lists.tool ?? [TOOL_CFG];
      return [];
    }),
    createConfigDraft: vi.fn(async () => ({})),
    publishConfig,
  } as unknown as ApiClient;
  return { client, publishConfig };
}

describe("RegistryTab", () => {
  it("renders endpoint and tool declarations with both honesty lines", async () => {
    const { client } = registryClient();
    render(<RegistryTab client={client} />);
    expect(await screen.findByText("Patient intake API")).toBeTruthy();
    expect(screen.getByText("/intake/patients")).toBeTruthy();
    expect(screen.getByText("FHIR lookup tool")).toBeTruthy();
    expect(screen.getByText("integration/fhir-main")).toBeTruthy();
    expect(
      screen.getByText(/A declaration does not create a live route/),
    ).toBeTruthy();
    expect(screen.getAllByText(/born disabled/).length)
      .toBeGreaterThanOrEqual(1);
    expect(
      screen.getByText(/Tool execution lands with MCP in Phase 16/),
    ).toBeTruthy();
  });

  it("shows empty states when both registries are empty", async () => {
    const { client } = registryClient({ endpoint: [], tool: [] });
    render(<RegistryTab client={client} />);
    expect(
      await screen.findByText("No endpoint declarations yet."),
    ).toBeTruthy();
    expect(screen.getByText("No tool declarations yet.")).toBeTruthy();
  });

  it("publishes a draft endpoint declaration", async () => {
    const { client, publishConfig } = registryClient();
    render(<RegistryTab client={client} />);
    await screen.findByText("Patient intake API");
    fireEvent.click(screen.getByRole("button", { name: "Publish v1" }));
    await waitFor(() =>
      expect(publishConfig).toHaveBeenCalledWith(
        "endpoint", "patient-intake-api", 1,
      ),
    );
  });
});
