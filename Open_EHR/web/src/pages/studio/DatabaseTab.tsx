import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Database tab — the DB platform surface (Studio parity rows
 *  26 + 34, over backend/app/dbplatform.py): the engine capability
 *  matrix, governed connection PROFILES, the canonical schema read
 *  model with governed extension drafts, and per-engine DDL export.
 *  Credentials are never stored: a profile names WHERE a database
 *  is and WHICH vault key holds its credential — never the
 *  credential itself (the server refuses credential-shaped fields
 *  with a 422). Exported DDL is applied by DBAs under change
 *  control; this tab executes no migration against a live
 *  database, by safety design. */

interface EngineRow {
  engine: string;
  display_name?: string;
  sqlalchemy_dialect?: string | null;
  driver_package?: string;
  verification?: string;
}

interface DbConfigRow {
  id: number;
  kind: string;
  key: string;
  version: number;
  status: string;
  payload: Record<string, unknown>;
}

interface SchemaResponse {
  tables: Array<{ name: string; source?: string }>;
  total: number;
}

interface DdlResponse {
  engine: string;
  variant: string;
  ddl: string;
}

const FALLBACK_DDL_ENGINES = [
  "postgres",
  "sqlite",
  "mysql",
  "mariadb",
  "mssql",
  "oracle",
];

function payloadStr(payload: Record<string, unknown>, key: string): string {
  const value = payload[key];
  if (typeof value === "string" && value) return value;
  if (typeof value === "number") return String(value);
  return "—";
}

export function DatabaseTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const enginesRes = useAsync<{ engines: EngineRow[]; total: number }>(
    () => client.request("/dbplatform/engines"),
    [client],
  );
  const profilesRes = useAsync<{ items: DbConfigRow[] }>(
    () => client.request("/dbplatform/profiles"),
    [client, reloadKey],
  );
  const schemaRes = useAsync<SchemaResponse>(
    () => client.request("/dbplatform/schema"),
    [client],
  );
  const extensionsRes = useAsync<{ items: DbConfigRow[] }>(
    () => client.request("/dbplatform/schema/extensions"),
    [client],
  );
  const [ddlEngine, setDdlEngine] = useState("postgres");
  const ddlRes = useAsync<DdlResponse>(
    () => client.request(`/dbplatform/ddl/${encodeURIComponent(ddlEngine)}`),
    [client, ddlEngine],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [form, setForm] = useState({
    key: "",
    engine: "postgres",
    host: "",
    port: "5432",
    database: "",
    adapter: "sqlalchemy",
    vaultKeyRef: "",
  });

  if (enginesRes.loading || profilesRes.loading || schemaRes.loading) {
    return <Loading label="Loading database platform…" />;
  }
  if (enginesRes.error) return <ErrorView error={enginesRes.error} />;
  if (profilesRes.error) return <ErrorView error={profilesRes.error} />;
  if (schemaRes.error) return <ErrorView error={schemaRes.error} />;

  const engines = enginesRes.data?.engines ?? [];
  const profiles = profilesRes.data?.items ?? [];
  const extensions = extensionsRes.data?.items ?? [];
  const schemaTotal = schemaRes.data?.total ?? schemaRes.data?.tables.length ?? 0;
  const ddlEngines =
    engines.length > 0
      ? engines
          .map((e) => e.engine)
          .filter((key) => key !== "mongodb-ferretdb")
      : FALLBACK_DDL_ENGINES;

  async function act(fn: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function createProfile() {
    const key = form.key.trim();
    const port = Number(form.port);
    if (!key || !Number.isInteger(port)) return;
    await act(
      () =>
        client.request("/dbplatform/profiles", {
          method: "POST",
          body: JSON.stringify({
            key,
            engine: form.engine,
            host: form.host.trim(),
            port,
            database: form.database.trim(),
            adapter: form.adapter.trim() || "sqlalchemy",
            vault_key_name: form.vaultKeyRef.trim(),
          }),
        }),
      `Draft profile ${key} created. Publish it to make it effective.`,
    );
  }

  return (
    <div>
      <p className="hint">
        One canonical schema, many engines: the matrix states honestly how far
        each engine has been verified. Connection profiles are governed
        configuration (draft → publish) and carry no secrets — credentials are
        never stored; vault key NAME only.
      </p>
      {notice && (
        <p className="hint" role="status">
          {notice}
        </p>
      )}

      <h3>Engines</h3>
      {engines.length === 0 ? (
        <Empty label="No database engines are registered." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Engine</th>
              <th>Driver</th>
              <th>Dialect</th>
              <th>Verification</th>
            </tr>
          </thead>
          <tbody>
            {engines.map((engine) => (
              <tr key={engine.engine}>
                <td>
                  <strong>{engine.display_name || engine.engine}</strong>
                  <br />
                  <code>{engine.engine}</code>
                </td>
                <td>{engine.driver_package || "—"}</td>
                <td>
                  {engine.sqlalchemy_dialect ? (
                    <code>{engine.sqlalchemy_dialect}</code>
                  ) : (
                    "—"
                  )}
                </td>
                <td>
                  <span className="badge">{engine.verification || "—"}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Connection profiles</h3>
      {profiles.length === 0 ? (
        <Empty label="No connection profiles yet. Create a draft below." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Profile</th>
              <th>Engine</th>
              <th>Host</th>
              <th>Database</th>
              <th>Vault key</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {profiles.map((profile) => (
              <tr key={profile.id}>
                <td>
                  <strong>{profile.key}</strong>
                  <br />
                  <span className="hint">v{profile.version}</span>
                </td>
                <td>{payloadStr(profile.payload, "engine")}</td>
                <td>
                  {payloadStr(profile.payload, "host")}
                  {typeof profile.payload.port === "number"
                    ? `:${profile.payload.port}`
                    : ""}
                </td>
                <td>{payloadStr(profile.payload, "database")}</td>
                <td>{payloadStr(profile.payload, "vault_key_name")}</td>
                <td>
                  <span className="badge">{profile.status}</span>
                </td>
                <td>
                  {profile.status === "draft" && (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.request(
                              `/dbplatform/profiles/${encodeURIComponent(profile.key)}/publish`,
                              { method: "POST" },
                            ),
                          `Published profile ${profile.key}.`,
                        )
                      }
                    >
                      Publish
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Create profile draft</h3>
      <div className="inline-form">
        <label className="field">
          Key{" "}
          <input
            aria-label="Profile key"
            value={form.key}
            onChange={(e) => setForm({ ...form, key: e.target.value })}
            placeholder="e.g. primary-postgres"
          />
        </label>
        <label className="field">
          Engine{" "}
          <select
            aria-label="Profile engine"
            value={form.engine}
            onChange={(e) => setForm({ ...form, engine: e.target.value })}
          >
            {(engines.length > 0
              ? engines.map((e) => e.engine)
              : FALLBACK_DDL_ENGINES
            ).map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Host{" "}
          <input
            aria-label="Profile host"
            value={form.host}
            onChange={(e) => setForm({ ...form, host: e.target.value })}
            placeholder="db.example.internal"
          />
        </label>
        <label className="field">
          Port{" "}
          <input
            aria-label="Profile port"
            value={form.port}
            onChange={(e) => setForm({ ...form, port: e.target.value })}
            inputMode="numeric"
          />
        </label>
        <label className="field">
          Database{" "}
          <input
            aria-label="Profile database"
            value={form.database}
            onChange={(e) => setForm({ ...form, database: e.target.value })}
            placeholder="opencare"
          />
        </label>
        <label className="field">
          Adapter{" "}
          <input
            aria-label="Profile adapter"
            value={form.adapter}
            onChange={(e) => setForm({ ...form, adapter: e.target.value })}
          />
        </label>
        <label className="field">
          Vault key reference{" "}
          <input
            aria-label="Profile vault key reference"
            value={form.vaultKeyRef}
            onChange={(e) => setForm({ ...form, vaultKeyRef: e.target.value })}
            placeholder="e.g. prod/db/password"
          />
        </label>
        <button
          type="button"
          disabled={!form.key.trim()}
          onClick={() => void createProfile()}
        >
          Create draft
        </button>
      </div>
      <p className="hint">
        Credentials are never stored; vault key NAME only — the profile names
        the vault key that holds the secret, never the secret itself.
      </p>

      <h3>Canonical schema</h3>
      <p>
        <strong>{schemaTotal}</strong> tables in the canonical schema (the
        same metadata the DDL export below compiles). Canonical clinical
        schema changes are code migrations, never configuration.
      </p>
      <h4>Schema-extension drafts</h4>
      {extensionsRes.loading ? (
        <Loading label="Loading schema extensions…" />
      ) : extensionsRes.error ? (
        <ErrorView error={extensionsRes.error} />
      ) : extensions.length === 0 ? (
        <Empty label="No schema-extension drafts yet." />
      ) : (
        <ul>
          {extensions.map((ext) => {
            const declared = Array.isArray(ext.payload.extensions)
              ? (ext.payload.extensions as Array<Record<string, unknown>>)
              : [];
            return (
              <li key={ext.id}>
                <strong>{ext.key}</strong> — <span className="badge">{ext.status}</span>{" "}
                {declared.length > 0
                  ? declared
                      .map(
                        (d) =>
                          `${String(d.table ?? "")}.${String(d.column ?? "")} (${String(d.type ?? "")})`,
                      )
                      .join("; ")
                  : "no extensions declared"}
              </li>
            );
          })}
        </ul>
      )}

      <h3>DDL export</h3>
      <p className="hint">
        Generated from the canonical model for the chosen dialect. DBAs apply
        exported DDL under change control — this tab runs no migration.
      </p>
      <p>
        <label>
          Engine{" "}
          <select
            aria-label="DDL engine"
            value={ddlEngine}
            onChange={(e) => setDdlEngine(e.target.value)}
          >
            {ddlEngines.map((key) => (
              <option key={key} value={key}>
                {key}
              </option>
            ))}
          </select>
        </label>
      </p>
      {ddlRes.loading ? (
        <Loading label="Generating DDL…" />
      ) : ddlRes.error ? (
        <ErrorView error={ddlRes.error} />
      ) : ddlRes.data ? (
        <pre className="config-payload">{ddlRes.data.ddl}</pre>
      ) : null}
    </div>
  );
}
