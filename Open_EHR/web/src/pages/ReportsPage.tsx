import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { saveDownloadedFile } from "../api/download";
import type { DownloadedFile, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Reports & print centre (Phase 13, Track F): the governed report
 *  catalogue (definitions are published Studio config over NAMED
 *  server-side queries — never ad-hoc SQL) with parameterised runs to
 *  PDF/CSV; the printable billing documents (invoice, receipt, patient
 *  statement, superbill) rendered server-side from live billing data;
 *  and the capability-gated export centre. Every generation is
 *  audited server-side with actor, definition and row count. */
export function ReportsPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canExport = permissions["data.export"]?.[role] === true;
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Depth pass (Workstream 1): stacked sections became shared Tabs.
  const [tab, setTab] = useState<string>("catalogue");

  const catalogue = useAsync(() => client.reportsCatalogue(), [client]);
  const datasets = useAsync(
    () => (canExport ? client.exportDatasets() : Promise.resolve([])),
    [client, canExport],
  );

  const [reportKey, setReportKey] = useState("");
  const [params, setParams] = useState<Record<string, string>>({});
  const [format, setFormat] = useState("pdf");
  const [refs, setRefs] = useState({ claim: "", payment: "", patient: "", encounter: "" });
  const [exportForm, setExportForm] = useState({ dataset: "patients", format: "csv", reason: "" });

  async function perform(label: string, fn: () => Promise<DownloadedFile>) {
    setBusy(true);
    setNotice(null);
    setActionError(null);
    try {
      const file = await fn();
      saveDownloadedFile(file);
      setNotice(
        `${label} downloaded (${file.filename}` +
          `${file.rows != null ? `, ${file.rows} rows` : ""}). Generation was audited.`,
      );
    } catch (err) {
      setActionError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  // UX pass (spec P-1): loading/error states keep the page title.
  if (catalogue.loading) {
    return (
      <div>
        <ModuleHeader title="Reports &amp; print centre" purpose="Run governed reports, print documents and export datasets." />
        <Loading label="Loading report catalogue…" />
      </div>
    );
  }
  if (catalogue.error) {
    return (
      <div>
        <ModuleHeader title="Reports &amp; print centre" purpose="Run governed reports, print documents and export datasets." />
        <ErrorView error={catalogue.error} />
      </div>
    );
  }

  const defs = catalogue.data ?? [];
  const active = defs.find((d) => d.key === reportKey) ?? defs[0];

  return (
    <div>
      <ModuleHeader icon="file" group="Insights" title="Reports &amp; print centre" purpose="Run governed reports, print documents and export datasets." />
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <p className="error-note" role="alert">{actionError}</p>}
      <Tabs
        tabs={[
          { key: "catalogue", label: "Catalogue" },
          { key: "print", label: "Print" },
          ...(canExport ? [{ key: "export", label: "Export" }] : []),
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Reports sections"
        panels={{
          catalogue: (
      <section aria-label="Report catalogue">
        <h3>Report catalogue</h3>
        {defs.length === 0 ? (
          <Empty label="No published report definitions yet. Author one in System Studio as kind=report and publish it." />
        ) : (
          <>
            <p>
              <label>
                Report{" "}
                <select
                  aria-label="Report"
                  value={active?.key ?? ""}
                  onChange={(e) => {
                    setReportKey(e.target.value);
                    setParams({});
                  }}
                >
                  {defs.map((d) => (
                    <option key={d.key} value={d.key}>
                      {d.title}
                    </option>
                  ))}
                </select>
              </label>{" "}
              <label>
                Format{" "}
                <select
                  aria-label="Format"
                  value={format}
                  onChange={(e) => setFormat(e.target.value)}
                >
                  {(active?.formats ?? ["pdf"]).map((f) => (
                    <option key={f} value={f}>
                      {f}
                    </option>
                  ))}
                </select>
              </label>
            </p>
            {active?.description && <p className="hint">{active.description}</p>}
            {active && active.parameters.length > 0 && (
              <div className="inline-form">
                {active.parameters.map((p) => (
                  <label key={p.name}>
                    {p.label ?? p.name}{" "}
                    {p.type === "choice" ? (
                      <select
                        aria-label={p.label ?? p.name}
                        value={params[p.name] ?? String(p.default ?? "")}
                        onChange={(e) =>
                          setParams((prev) => ({ ...prev, [p.name]: e.target.value }))
                        }
                      >
                        {(p.choices ?? []).map((c) => (
                          <option key={c} value={c}>
                            {c}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input
                        aria-label={p.label ?? p.name}
                        type={p.type === "number" ? "number" : p.type === "date" ? "date" : "text"}
                        value={params[p.name] ?? ""}
                        onChange={(e) =>
                          setParams((prev) => ({ ...prev, [p.name]: e.target.value }))
                        }
                      />
                    )}
                  </label>
                ))}
              </div>
            )}
            {active && (
              <p>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform(`Report ${active.title}`, () => {
                      const coerced: Record<string, string | number> = {};
                      for (const p of active.parameters) {
                        const raw = params[p.name];
                        if (raw == null || raw === "") {
                          if (p.default != null) coerced[p.name] = p.default;
                          continue;
                        }
                        coerced[p.name] = p.type === "number" ? Number(raw) : raw;
                      }
                      return client.runReport(active.key, format, coerced);
                    })
                  }
                >
                  Run &amp; download
                </button>
              </p>
            )}
          </>
        )}
      </section>

          ),
          print: (
      <section aria-label="Printable documents">
        <h3>Printable documents</h3>
        <p className="hint">
          Server-rendered PDFs on the published print template (letterhead +
          confidentiality footer from config). Every print is audit-chained.
        </p>
        <div className="inline-form">
          <label>
            Claim ID{" "}
            <input
              aria-label="Claim ID"
              value={refs.claim}
              onChange={(e) => setRefs((r) => ({ ...r, claim: e.target.value }))}
            />
          </label>
          <button
            type="button"
            disabled={busy || !refs.claim}
            onClick={() => perform("Invoice", () => client.printInvoice(refs.claim))}
          >
            Print invoice
          </button>
          <label>
            Payment ID{" "}
            <input
              aria-label="Payment ID"
              value={refs.payment}
              onChange={(e) => setRefs((r) => ({ ...r, payment: e.target.value }))}
            />
          </label>
          <button
            type="button"
            disabled={busy || !refs.payment}
            onClick={() => perform("Receipt", () => client.printReceipt(refs.payment))}
          >
            Print receipt
          </button>
          <label>
            Patient ID{" "}
            <input
              aria-label="Patient ID"
              value={refs.patient}
              onChange={(e) => setRefs((r) => ({ ...r, patient: e.target.value }))}
            />
          </label>
          <button
            type="button"
            disabled={busy || !refs.patient}
            onClick={() => perform("Statement", () => client.printStatement(refs.patient))}
          >
            Print statement
          </button>
          <label>
            Encounter ID{" "}
            <input
              aria-label="Encounter ID"
              value={refs.encounter}
              onChange={(e) => setRefs((r) => ({ ...r, encounter: e.target.value }))}
            />
          </label>
          <button
            type="button"
            disabled={busy || !refs.encounter}
            onClick={() => perform("Superbill", () => client.printSuperbill(refs.encounter))}
          >
            Print superbill
          </button>
        </div>
      </section>

          ),
          export: canExport ? (
        <section aria-label="Export centre">
          <h3>Export centre</h3>
          <p className="hint">
            Named, server-defined datasets only — no ad-hoc queries. Exports
            are audited with row counts; above the configured threshold a
            reason is required.
          </p>
          <div className="inline-form">
            <label>
              Dataset{" "}
              <select
                aria-label="Dataset"
                value={exportForm.dataset}
                onChange={(e) => setExportForm((f) => ({ ...f, dataset: e.target.value }))}
              >
                {(datasets.data ?? []).map((d) => (
                  <option key={d.key} value={d.key}>
                    {d.title}
                  </option>
                ))}
              </select>
            </label>
            <label>
              Format{" "}
              <select
                aria-label="Export format"
                value={exportForm.format}
                onChange={(e) => setExportForm((f) => ({ ...f, format: e.target.value }))}
              >
                <option value="csv">csv</option>
                <option value="json">json</option>
              </select>
            </label>
            <label>
              Reason (required for large exports){" "}
              <input
                aria-label="Export reason"
                value={exportForm.reason}
                onChange={(e) => setExportForm((f) => ({ ...f, reason: e.target.value }))}
              />
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform("Export", () =>
                  client.runExport({
                    dataset: exportForm.dataset,
                    format: exportForm.format,
                    reason: exportForm.reason || undefined,
                  }),
                )
              }
            >
              Export
            </button>
          </div>
        </section>          ) : (
            <Empty label="Export requires the data.export capability." />
          ),
        }}
      />
    </div>
  );
}
