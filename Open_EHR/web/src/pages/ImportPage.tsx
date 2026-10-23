import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { ImportValidation, Role } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { ModuleHeader } from "../components/ModuleHeader";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Import centre (Phase 13, Track F): pick a governed mapping profile
 *  (published Studio config: source column → target field, transforms
 *  limited to the governed set), paste or upload CSV, run the full
 *  validation pass — every bad row, field and reason, nothing written —
 *  then commit all-or-nothing. The commit re-validates server-side, so
 *  a stale "valid" here can never import bad data. */
export function ImportPage({ client }: Props) {
  const profiles = useAsync(() => client.importProfiles(), [client]);
  const [profileKey, setProfileKey] = useState("");
  const [csv, setCsv] = useState("");
  const [result, setResult] = useState<ImportValidation | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (profiles.loading)
    return (
      <>
        <ModuleHeader title="Import centre" />
        <Loading label="Loading mapping profiles…" />
      </>
    );
  if (profiles.error)
    return (
      <>
        <ModuleHeader title="Import centre" />
        <ErrorView error={profiles.error} />
      </>
    );

  const list = profiles.data ?? [];
  const activeKey = profileKey || list[0]?.key || "";
  const active = list.find((p) => p.key === activeKey);

  function reset(nextCsv: string, nextProfile?: string) {
    setCsv(nextCsv);
    if (nextProfile) setProfileKey(nextProfile);
    setResult(null);
    setNotice(null);
    setActionError(null);
  }

  async function validate() {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      setResult(await client.validateImport(activeKey, csv));
    } catch (err) {
      setActionError(`Validation failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function commit() {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const out = await client.commitImport(activeKey, csv);
      setNotice(
        `Imported ${out.imported} ${out.entity_type} record(s) via profile ` +
          `${out.profile}. The import was audited with its row count.`,
      );
      setResult(null);
    } catch (err) {
      // A 422 carries the full validation report in practice; the
      // validate pass above is the detailed view of it.
      setActionError(`Import refused: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <ModuleHeader
        icon="upload"
        group="System"
        title="Import centre"
        purpose="Bulk-load records through a governed mapping profile: validate every row first, then commit all-or-nothing."
      />
      <p className="hint">
        Mapping profiles are published Studio config. Validation reports
        every problem and writes nothing; commit is all-or-nothing.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}
      {list.length === 0 ? (
        <Empty label="No published mapping profiles yet. Author one in System Studio as kind=mapping and publish it." />
      ) : (
        <>
          <p>
            <label>
              Mapping profile{" "}
              <select
                aria-label="Mapping profile"
                value={activeKey}
                onChange={(e) => reset(csv, e.target.value)}
              >
                {list.map((p) => (
                  <option key={p.key} value={p.key}>
                    {p.key} ({p.entity_type})
                  </option>
                ))}
              </select>
            </label>
          </p>
          {active && active.columns.length > 0 && (
            <p className="hint">Expected columns: {active.columns.join(", ")}</p>
          )}
          <p>
            <label>
              CSV file{" "}
              <input
                aria-label="CSV file"
                type="file"
                accept=".csv,text/csv"
                onChange={async (e) => {
                  const file = e.target.files?.[0];
                  if (file) reset(await file.text());
                }}
              />
            </label>
          </p>
          <p>
            <label>
              CSV content{" "}
              <textarea
                aria-label="CSV content"
                rows={10}
                cols={80}
                value={csv}
                onChange={(e) => reset(e.target.value)}
              />
            </label>
          </p>
          <p>
            <button type="button" disabled={busy || !csv} onClick={validate}>
              Validate
            </button>{" "}
            <button
              type="button"
              disabled={busy || !result || !result.valid}
              onClick={commit}
            >
              Commit import
            </button>{" "}
            {!result && (
              <span className="hint">
                Commit unlocks after a validation run — nothing imports until every row is valid.
              </span>
            )}
            {result && !result.valid && (
              <span className="hint">
                Commit stays disabled while validation reports errors — fix the rows and validate again.
              </span>
            )}
          </p>
          {result && (
            <section aria-label="Validation results">
              <h3>Validation results</h3>
              <p>
                {result.rows_total} data row(s);{" "}
                {result.valid ? (
                  <strong>valid — ready to commit.</strong>
                ) : (
                  <strong>{result.errors.length} error(s) — nothing will import.</strong>
                )}
              </p>
              {result.errors.length > 0 && (
                <table className="data-table">
                  <thead>
                    <tr>
                      <th scope="col">Row</th>
                      <th scope="col">Field</th>
                      <th scope="col">Reason</th>
                    </tr>
                  </thead>
                  <tbody>
                    {result.errors.map((e, i) => (
                      <tr key={i}>
                        <td className="num">{e.row}</td>
                        <td>{e.field}</td>
                        <td>{e.reason}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </section>
          )}
        </>
      )}
    </div>
  );
}
