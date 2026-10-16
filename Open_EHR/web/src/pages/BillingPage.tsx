import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type { Claim, Role } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

// The seeded claim lifecycle (Studio config workflow/claim-lifecycle v1).
// The server is the authority — this map only decides which buttons to
// offer; a transition the server rejects returns as an inline error.
const NEXT_ACTIONS: Record<string, Array<{ to: string; label: string; note?: boolean }>> = {
  draft: [{ to: "ready", label: "Mark ready" }],
  ready: [{ to: "submitted", label: "Submit claim" }],
  submitted: [
    { to: "paid", label: "Record paid" },
    { to: "denied", label: "Record denied" },
  ],
  denied: [{ to: "appealed", label: "Appeal (once, with note)", note: true }],
  appealed: [{ to: "submitted", label: "Resubmit after appeal" }],
  paid: [],
};

const CLAIM_STATES = ["draft", "ready", "submitted", "paid", "denied", "appealed"];
const EXPORTABLE = ["submitted", "paid", "denied", "appealed"];

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function money(value?: number, currency?: string): string {
  return typeof value === "number" ? `${currency ?? "USD"} ${value.toFixed(2)}` : "—";
}

function linePriced(item: NonNullable<Claim["item"]>[number]): boolean {
  return item.extension?.find((e) => e.url.endsWith("/priced"))?.valueBoolean ?? true;
}

export function BillingPage({ client, role }: Props) {
  const canManage = role === "admin" || role === "configurator";
  const isPatient = role === "patient";
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [encounterId, setEncounterId] = useState("");
  const [appealNote, setAppealNote] = useState("");
  const [exportText, setExportText] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const claimsState = useAsync(
    () => client.claims(statusFilter ? { status: statusFilter } : {}),
    [client, statusFilter, tick],
  );
  const detailState = useAsync(
    () => (selectedId ? client.getClaim(selectedId) : Promise.resolve(null)),
    [client, selectedId, tick],
  );

  const claims = claimsState.data ?? [];
  const detail = detailState.data;

  async function perform(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function download837(claim: Claim) {
    setBusy(true);
    setActionError(null);
    try {
      const text = await client.export837(claim.id as string);
      setExportText(text);
      const blob = new Blob([text], { type: "text/plain" });
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `claim-${claim.id}-837.txt`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setActionError(`837 export failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <h2>Billing</h2>
      <p className="hint">
        Claims are captured from an encounter: the problem list becomes the
        diagnoses (and priced problem-evaluation lines), and the encounter's
        procedures and observations are priced from the published Studio fee
        schedule (<code>terminology/fee-schedule</code>). A code missing from
        the schedule is charged at 0.00 and flagged <strong>unpriced</strong> —
        never dropped. The lifecycle runs from Studio workflow config; a
        denied claim may be appealed once, with a note. The 837 export is a
        format-correct X12 837P-style file for review — nothing is
        transmitted to a clearinghouse.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      {canManage && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            const id = encounterId.trim();
            if (!id) return;
            void perform("Generate claim", async () => {
              const claim = await client.generateClaim(id);
              setSelectedId(claim.id as string);
            });
          }}
        >
          <label>
            Encounter id{" "}
            <input
              aria-label="Encounter id"
              value={encounterId}
              onChange={(e) => setEncounterId(e.target.value)}
              placeholder="enc-1"
            />
          </label>
          <button type="submit" disabled={busy || !encounterId.trim()}>
            Generate claim from encounter
          </button>
        </form>
      )}

      <div className="toolbar">
        <label>
          Status{" "}
          <select
            aria-label="Claim status filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All statuses</option>
            {CLAIM_STATES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      </div>

      <h3>Claims{isPatient ? " (your claims)" : ""}</h3>
      {claimsState.loading && <Loading label="Loading claims…" />}
      {claimsState.error != null && <ErrorView error={claimsState.error} />}
      {!claimsState.loading && !claimsState.error && claims.length === 0 && (
        <Empty label="No claims in this view." />
      )}
      {claims.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Claim</th>
              <th>Patient</th>
              <th>Status</th>
              <th>Lines</th>
              <th>Total</th>
              <th>Appeals</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {claims.map((claim) => (
              <tr key={claim.id}>
                <td>{claim.id}</td>
                <td>{refLabel(claim.patient)}</td>
                <td><span className="badge">{claim.status ?? "—"}</span></td>
                <td>{claim.item?.length ?? 0}</td>
                <td>{money(claim.total?.value, claim.total?.currency)}</td>
                <td>{claim.appealCount ?? 0}</td>
                <td>
                  <button type="button" onClick={() => setSelectedId(claim.id as string)}>
                    View
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedId && (
        <>
          <h3>Claim detail</h3>
          {detailState.loading && <Loading label="Loading claim…" />}
          {detailState.error != null && <ErrorView error={detailState.error} />}
          {detail && (
            <div>
              <p>
                <strong>{detail.id}</strong> · patient {refLabel(detail.patient)} ·{" "}
                status <span className="badge">{detail.status}</span> · total{" "}
                <strong>{money(detail.total?.value, detail.total?.currency)}</strong>
              </p>
              {detail.diagnosis && detail.diagnosis.length > 0 && (
                <p className="hint">
                  Diagnoses:{" "}
                  {detail.diagnosis
                    .map((d) => codeLabel(d.diagnosisCodeableConcept))
                    .join("; ")}
                </p>
              )}
              <table className="table">
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Charge</th>
                    <th>Code</th>
                    <th>Qty</th>
                    <th>Unit price</th>
                    <th>Net</th>
                    <th>Priced</th>
                  </tr>
                </thead>
                <tbody>
                  {(detail.item ?? []).map((item, i) => (
                    <tr key={item.sequence ?? i}>
                      <td>{item.sequence ?? i + 1}</td>
                      <td>{codeLabel(item.productOrService)}</td>
                      <td>{item.productOrService?.coding?.[0]?.code ?? "—"}</td>
                      <td>{item.quantity?.value ?? 1}</td>
                      <td>{money(item.unitPrice?.value, item.unitPrice?.currency)}</td>
                      <td>{money(item.net?.value, item.net?.currency)}</td>
                      <td>{linePriced(item) ? "yes" : <strong>unpriced</strong>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {detail.statusHistory && detail.statusHistory.length > 0 && (
                <p className="hint">
                  History:{" "}
                  {detail.statusHistory
                    .map((h) => `${h.from ?? "∅"} → ${h.to} (${h.by}${h.note ? ` — ${h.note}` : ""})`)
                    .join(" · ")}
                </p>
              )}

              <div className="toolbar">
                {canManage &&
                  (NEXT_ACTIONS[detail.status ?? ""] ?? [])
                    .filter((a) => !(a.to === "appealed" && (detail.appealCount ?? 0) >= 1))
                    .map((action) =>
                      action.note ? (
                        <span key={action.to}>
                          <input
                            aria-label="Appeal note"
                            value={appealNote}
                            onChange={(e) => setAppealNote(e.target.value)}
                            placeholder="Appeal note (required)"
                          />
                          <button
                            type="button"
                            disabled={busy || !appealNote.trim()}
                            onClick={() =>
                              void perform(action.label, async () => {
                                await client.transitionClaim(
                                  detail.id as string, action.to, appealNote.trim());
                                setAppealNote("");
                              })
                            }
                          >
                            {action.label}
                          </button>
                        </span>
                      ) : (
                        <button
                          key={action.to}
                          type="button"
                          disabled={busy}
                          onClick={() =>
                            void perform(action.label, () =>
                              client.transitionClaim(detail.id as string, action.to))
                          }
                        >
                          {action.label}
                        </button>
                      ),
                    )}
                {EXPORTABLE.includes(detail.status ?? "") && (
                  <button type="button" disabled={busy} onClick={() => void download837(detail)}>
                    Download 837 export
                  </button>
                )}
              </div>
              {exportText && (
                <pre className="pre" aria-label="837 export preview">{exportText}</pre>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
