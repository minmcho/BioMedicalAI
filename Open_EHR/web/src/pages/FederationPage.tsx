import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 16 (WS-A2 / K2) — federation: peer record location against the
// published `integration/federation` config, and reconciliation of
// pulled external records. Peers are governed Studio configuration —
// there is deliberately no peer-editing control here. Consent is
// enforced by the RESPONDING peer; this page shows the consent outcome
// the peer returned (permit answer vs refusal) rather than assuming it.
// Staged records never enter the chart until a human accepts or links
// them here; there is no auto-merge.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Peer {
  name: string;
  baseUrl?: string;
  enabled?: boolean;
}

interface PeersResponse {
  node: { name: string; baseUrl?: string };
  peers: Peer[];
  config: { kind: string; key: string; version: number | null };
}

interface QueryResponse {
  mode: "full" | "references";
  purpose?: string;
  peer: string;
  patient_ref?: string;
  patient?: Record<string, unknown>;
  counts?: Record<string, number>;
  references?: Array<{ reference: string; resourceType: string }>;
  resources?: Array<Record<string, unknown>>;
  total?: number;
}

interface ReconItem {
  id: number;
  source_peer: string;
  resource_type: string;
  external_id: string;
  external_ref: string;
  patient_ref: string;
  status: string;
  linked_ref: string | null;
  provenance: { peer?: string; pulled_by?: string; pulled_at?: string } | null;
  acted_by: string | null;
  action_note: string | null;
  chart_exists: boolean;
  chart_ref: string | null;
  same_type_chart_refs: string[];
  fields: Array<{ field: string; external: unknown; chart: unknown; differs: boolean }>;
}

interface ReconResponse {
  patient_ref: string;
  items: ReconItem[];
  staged_count: number;
}

function statusTone(status: string): StatusTone {
  if (status === "staged") return "warn";
  if (status === "accepted" || status === "linked") return "ok";
  if (status === "rejected") return "bad";
  return "neutral";
}

function fmt(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

export function FederationPage({ client }: Props) {
  const peersState = useAsync<PeersResponse>(() => client.request("/federation/peers"), [client]);
  const peers = peersState.data?.peers ?? [];
  const [peer, setPeer] = useState("");
  const [patientId, setPatientId] = useState("");
  const [mode, setMode] = useState<"full" | "references">("references");
  const [purpose, setPurpose] = useState("treatment");
  const [queryResult, setQueryResult] = useState<QueryResponse | null>(null);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [consentRefused, setConsentRefused] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [reconPatient, setReconPatient] = useState("");
  const recon = useAsync<ReconResponse | null>(
    () =>
      reconPatient
        ? client.request(`/federation/reconciliation/${encodeURIComponent(reconPatient)}`)
        : Promise.resolve(null),
    [client, reconPatient],
  );

  const selectedPeer = peer || peers[0]?.name || "";

  async function runQuery() {
    setBusy(true);
    setQueryError(null);
    setConsentRefused(null);
    setQueryResult(null);
    try {
      const result = await client.request<QueryResponse>("/federation/query", {
        method: "POST",
        body: JSON.stringify({ peer: selectedPeer, patient_id: patientId, mode, purpose }),
      });
      setQueryResult(result);
      if (result.patient_ref) setReconPatient(result.patient_ref.replace(/^Patient\//, ""));
    } catch (err) {
      if (err instanceof ApiError && err.status === 403) {
        setConsentRefused(err.detail);
      } else {
        setQueryError(errorText(err));
      }
    } finally {
      setBusy(false);
    }
  }

  async function pull() {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const result = await client.request<{ staged_count: number; skipped_decided: number }>(
        "/federation/pull",
        {
          method: "POST",
          body: JSON.stringify({ peer: selectedPeer, patient_id: patientId, purpose }),
        },
      );
      setNotice(
        `Pulled from ${selectedPeer}: ${result.staged_count} record(s) staged for reconciliation` +
          (result.skipped_decided ? `, ${result.skipped_decided} already-decided record(s) skipped.` : "."),
      );
      if (patientId) setReconPatient(patientId);
      recon.reload();
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <ModuleHeader
        icon="network"
        group="Data Platform"
        title="Federation"
        purpose="Locate and pull records across peer OpenCare nodes, then reconcile what was staged."
      />
      <p className="hint">
        Record location across peer OpenCare nodes. Peers come from the published Studio
        configuration <code>integration/federation</code>
        {peersState.data ? ` (v${peersState.data.config.version ?? "—"})` : ""} — edit them in
        System Studio, not here. The responding peer enforces the patient&apos;s consent on its
        side; a refusal is shown, never bypassed.
      </p>
      {peersState.loading && <Loading label="Loading federation peers…" />}
      {peersState.error != null && <ErrorView error={peersState.error} />}
      {peersState.data && (
        <div className="card">
          <h3>This node</h3>
          <p>
            <strong>{peersState.data.node.name}</strong>
            {peersState.data.node.baseUrl ? ` · ${peersState.data.node.baseUrl}` : ""}
          </p>
          <h3>Peers</h3>
          {peers.length === 0 ? (
            <Empty label="No peers are published in integration/federation yet. Publish a peer list in System Studio to query other nodes." />
          ) : (
            <ul className="result-list">
              {peers.map((p) => (
                <li key={p.name} className="result-row">
                  <span className="result-type">{p.name}</span>
                  <span className="result-meta">{p.baseUrl ?? "no base URL published"}</span>
                  <StatusBadge tone={p.enabled === false ? "neutral" : "ok"}>
                    {p.enabled === false ? "disabled" : "enabled"}
                  </StatusBadge>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <div className="card">
        <h3>Record-locator query</h3>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void runQuery();
          }}
        >
          <label>
            Peer
            <select
              value={selectedPeer}
              onChange={(e) => setPeer(e.target.value)}
              disabled={peers.length === 0}
            >
              {peers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name}
                </option>
              ))}
            </select>
            {peers.length === 0 && (
              <span className="field-error">No peers published — the query is unavailable until a peer list is published in System Studio.</span>
            )}
          </label>
          <label>
            Patient ID at the peer
            <input value={patientId} onChange={(e) => setPatientId(e.target.value)} required />
          </label>
          <label>
            Mode
            <select value={mode} onChange={(e) => setMode(e.target.value as "full" | "references")}>
              <option value="references">References only — data behind glass</option>
              <option value="full">Full record</option>
            </select>
          </label>
          <label>
            Purpose of use
            <input value={purpose} onChange={(e) => setPurpose(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || peers.length === 0 || !patientId}>
            Query peer
          </button>{" "}
          <button type="button" disabled={busy || peers.length === 0 || !patientId} onClick={() => void pull()}>
            Pull records into reconciliation staging
          </button>
        </form>
        {notice && <p role="status">{notice}</p>}
        {actionError && (
          <p className="form-error" role="alert">
            {actionError}
          </p>
        )}
        {consentRefused && (
          <div className="state state-error" role="alert">
            <strong>Consent refused by the peer.</strong> {consentRefused} No data was returned;
            the peer records this refusal in its own audit log.
          </div>
        )}
        {queryError && (
          <p className="form-error" role="alert">
            {queryError}
          </p>
        )}
        {queryResult && (
          <div>
            <p role="status">
              <StatusBadge tone="ok">consent permit — peer answered</StatusBadge>{" "}
              {queryResult.mode === "references" ? (
                <>
                  Mode: <strong>references</strong> — <em>data behind glass</em>: counts and bare
                  references only, no clinical payload crossed the boundary.
                </>
              ) : (
                <>Mode: <strong>full record</strong> — the peer returned the clinical payload.</>
              )}
            </p>
            {queryResult.counts && (
              <ul className="result-list">
                {Object.entries(queryResult.counts).map(([type, count]) => (
                  <li key={type} className="result-row">
                    <span className="result-type">{type}</span>
                    <span className="result-meta">{count}</span>
                  </li>
                ))}
              </ul>
            )}
            {queryResult.mode === "references" && queryResult.references && (
              <ul className="result-list">
                {queryResult.references.map((r) => (
                  <li key={r.reference} className="result-row">
                    <span className="result-type">{r.reference}</span>
                    <span className="result-meta">{r.resourceType}</span>
                  </li>
                ))}
              </ul>
            )}
            {queryResult.mode === "full" && (
              <p className="hint">
                {queryResult.total ?? queryResult.resources?.length ?? 0} resource(s) returned for{" "}
                {queryResult.patient_ref ?? patientId}. Use “Pull records into reconciliation
                staging” to bring them in for review — a query alone files nothing.
              </p>
            )}
          </div>
        )}
      </div>

      <div className="card">
        <h3>Reconciliation — staged external records</h3>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            recon.reload();
          }}
        >
          <label>
            Patient ID
            <input value={reconPatient} onChange={(e) => setReconPatient(e.target.value)} />
          </label>
          <button type="submit" disabled={!reconPatient}>
            Load staged records
          </button>
        </form>
        {recon.loading && reconPatient && <Loading label="Loading staged records…" />}
        {recon.error != null && <ErrorView error={recon.error} />}
        {recon.data && recon.data.items.length === 0 && (
          <Empty label="No external records are staged for this patient. Pull from a peer first — records land in staging, never directly in the chart." />
        )}
        {recon.data?.items.map((item) => (
          <ReconRow key={item.id} client={client} item={item} onChanged={() => recon.reload()} />
        ))}
      </div>
    </section>
  );
}

function ReconRow({
  client,
  item,
  onChanged,
}: {
  client: ApiClient;
  item: ReconItem;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [linkRef, setLinkRef] = useState(item.same_type_chart_refs[0] ?? "");
  const [reason, setReason] = useState("");
  const differing = item.fields.filter((f) => f.differs);

  async function act(path: string, body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      await client.request(path, { method: "POST", body: JSON.stringify(body) });
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h4>
        {item.external_ref} <StatusBadge tone={statusTone(item.status)}>{item.status}</StatusBadge>
      </h4>
      <p className="hint">
        Source peer: <strong>{item.source_peer}</strong>
        {item.provenance?.pulled_by
          ? ` · pulled by ${item.provenance.pulled_by}${item.provenance.pulled_at ? ` at ${item.provenance.pulled_at}` : ""}`
          : ""}
        {item.linked_ref ? ` · linked to ${item.linked_ref}` : ""}
        {item.acted_by ? ` · decided by ${item.acted_by}` : ""}
        {item.action_note ? ` · note: ${item.action_note}` : ""}
      </p>
      <p className="hint">
        Chart:{" "}
        {item.chart_exists
          ? `a chart resource already exists at ${item.chart_ref} — accept-new will be refused; link instead.`
          : "no chart resource with this type/id yet."}
      </p>
      {differing.length > 0 ? (
        <table className="data-table">
          <thead>
            <tr>
              <th>Field</th>
              <th>External (peer)</th>
              <th>Chart (here)</th>
            </tr>
          </thead>
          <tbody>
            {differing.map((f) => (
              <tr key={f.field}>
                <td>{f.field}</td>
                <td>{fmt(f.external)}</td>
                <td>{fmt(f.chart)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : (
        <p className="hint">No field differences against the chart resource.</p>
      )}
      {item.status === "staged" && (
        <div>
          <button
            type="button"
            disabled={busy}
            onClick={() => void act(`/federation/reconciliation/items/${item.id}/accept-new`, {})}
          >
            Accept as new chart record
          </button>{" "}
          <label>
            Link to existing chart record
            <select value={linkRef} onChange={(e) => setLinkRef(e.target.value)}>
              {item.same_type_chart_refs.length === 0 && <option value="">none in chart</option>}
              {item.same_type_chart_refs.map((ref) => (
                <option key={ref} value={ref}>
                  {ref}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={busy || !linkRef}
            onClick={() =>
              void act(`/federation/reconciliation/items/${item.id}/link-existing`, {
                chart_ref: linkRef,
              })
            }
          >
            Link existing
          </button>{" "}
          <label>
            Reject reason
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void act(`/federation/reconciliation/items/${item.id}/reject`, { reason })
            }
          >
            Reject
          </button>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
