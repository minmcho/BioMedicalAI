import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { LakeEntry, LineageResponse, Role } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { EmpiTab } from "./EmpiTab";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const RUN_ROLES: Role[] = ["admin", "configurator"];
const MANAGE_ROLES: Role[] = ["admin", "configurator", "clinician"];

/** Data platform (Phase 14, Tracks H+I): the append-only raw-zone
 *  lake with lineage lookup ("where did this value come from"), the
 *  governed pipeline definitions with run history, the data-quality
 *  quarantine queue (release resumes the run; reject stops it), dead
 *  letters with replay, and MPI dedup candidates for human review —
 *  the scan never merges; merging stays a human act elsewhere. */
export function DataPlatformPage({ client, role }: Props) {
  const lake = useAsync(() => client.lakeEntries(), [client]);
  const definitions = useAsync(() => client.pipelineDefinitions(), [client]);
  const runs = useAsync(() => client.pipelineRuns(), [client]);
  const letters = useAsync(() => client.pipelineDeadLetters(), [client]);
  const holds = useAsync(() => client.quarantine("held"), [client]);
  const candidates = useAsync(() => client.dedupCandidates("pending"), [client]);

  const [selected, setSelected] = useState<LakeEntry | null>(null);
  const [lineageType, setLineageType] = useState("Observation");
  const [lineageId, setLineageId] = useState("");
  const [lineage, setLineage] = useState<LineageResponse | null>(null);
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // Depth pass (Workstream 1): stacked sections became shared Tabs.
  const [tab, setTab] = useState<string>("lake");

  const canRun = RUN_ROLES.includes(role);
  const canManage = MANAGE_ROLES.includes(role);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(label);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function openEntry(id: number) {
    setActionError(null);
    try {
      setSelected(await client.lakeEntry(id));
    } catch (err) {
      setActionError(errorText(err));
    }
  }

  async function lookupLineage() {
    setActionError(null);
    setLineage(null);
    try {
      setLineage(await client.lineage(lineageType.trim(), lineageId.trim()));
    } catch (err) {
      setActionError(errorText(err));
    }
  }

  // UX pass (spec P-1): loading/error states keep the page title.
  if (lake.loading || definitions.loading) {
    return (
      <section>
        <ModuleHeader title="Data platform" purpose="Raw-zone lake, pipelines, quarantine, dedup and EMPI merges." />
        <Loading label="Loading data platform…" />
      </section>
    );
  }
  if (lake.error || definitions.error) {
    return (
      <section>
        <ModuleHeader title="Data platform" purpose="Raw-zone lake, pipelines, quarantine, dedup and EMPI merges." />
        <ErrorView error={lake.error ?? definitions.error} />
      </section>
    );
  }

  const lakeData = lake.data;
  const defs = definitions.data ?? [];
  const runList = runs.data ?? [];
  const letterList = letters.data ?? [];
  const holdList = holds.data ?? [];
  const candidateList = candidates.data ?? [];

  return (
    <section key={reloadKey}>
      <ModuleHeader icon="database" group="Data Platform" title="Data platform" purpose="Raw-zone lake, pipelines, quarantine, dedup and EMPI merges." />
      <p className="muted">
        The raw zone is append-only: inbound payloads land immutable with a content hash,
        and corrections are new entries — never edits. Curated records link back to their
        raw entry, so every value can answer “where did this come from”.
      </p>
      {notice && <p className="notice">{notice}</p>}
      {actionError && <p className="error">{actionError}</p>}
      <Tabs
        tabs={[
          { key: "lake", label: "Lake" },
          { key: "pipelines", label: "Pipelines" },
          { key: "quarantine", label: "Quarantine" },
          { key: "dedup", label: "Dedup review" },
          { key: "empi", label: "EMPI merges" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Data platform sections"
        panels={{
          lake: (
            <>
      <h3>Raw-zone lake</h3>
      {!lakeData || lakeData.items.length === 0 ? (
        <Empty label="No raw entries yet." />
      ) : (
        <>
          <p className="muted">
            {lakeData.total_entries} entries · by disposition:{" "}
            {Object.entries(lakeData.by_disposition).map(([k, v]) => `${k}: ${v}`).join(", ")}
          </p>
          <table className="data-table">
            <thead>
              <tr><th>#</th><th>Type</th><th>Device</th><th>Source</th><th>Disposition</th><th>Hash</th><th>Received</th><th /></tr>
            </thead>
            <tbody>
              {lakeData.items.slice(0, 25).map((e) => (
                <tr key={e.id}>
                  <td className="num">{e.id}</td>
                  <td>{e.payload_type}</td>
                  <td>{e.device_key ?? "—"}</td>
                  <td>{e.source} ({e.source_label})</td>
                  <td>{e.disposition}</td>
                  <td><code>{e.content_hash.slice(0, 12)}…</code></td>
                  <td>{e.received_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
                  <td><button type="button" onClick={() => openEntry(e.id)}>View payload</button></td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      {selected && (
        <div className="panel">
          <h4>Raw entry #{selected.id} — {selected.payload_type} ({selected.disposition})</h4>
          <pre>{selected.payload ?? "(payload not loaded)"}</pre>
          {selected.curated_links && selected.curated_links.length > 0 && (
            <p className="muted">
              Curated: {selected.curated_links.map((l) => `${l.curated_ref} (${l.link_kind})`).join(", ")}
            </p>
          )}
        </div>
      )}

      <h3>Lineage lookup</h3>
      <div className="row">
        <label>
          Resource type{" "}
          <input aria-label="Lineage resource type" value={lineageType} onChange={(e) => setLineageType(e.target.value)} />
        </label>
        <label>
          Resource id{" "}
          <input aria-label="Lineage resource id" value={lineageId} onChange={(e) => setLineageId(e.target.value)} />
        </label>
        <button type="button" disabled={!lineageId.trim()} onClick={lookupLineage}>
          Where did this come from?
        </button>
      </div>
      {lineage && (
        <ul>
          {lineage.raw_entries.map((e) => (
            <li key={e.id}>
              Raw #{e.id} via {e.interface ?? e.source}
              {e.device ? <> from device <strong>{e.device.device_key}</strong></> : null}
              {" "}({e.source_label}) — hash <code>{e.content_hash.slice(0, 12)}…</code>
            </li>
          ))}
        </ul>
      )}

            </>
          ),
          pipelines: (
            <>
      <h3>Pipelines</h3>
      {defs.length === 0 ? (
        <Empty label="No published pipeline definitions." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Pipeline</th><th>Schedule</th><th>Enabled</th><th>Steps</th><th>Last run</th><th /></tr>
          </thead>
          <tbody>
            {defs.map((d) => (
              <tr key={d.key}>
                <td>{d.name ?? d.key}<br /><span className="muted">{d.key} v{d.version}</span></td>
                <td>{d.schedule ?? "—"}</td>
                <td>{d.enabled ? "yes" : "no"}</td>
                <td>{d.steps.map((s) => s.type).join(" → ")}</td>
                <td>{d.last_run ? <StatusBadge tone={d.last_run.status === "succeeded" ? "ok" : "neutral"}>{d.last_run.status}</StatusBadge> : "—"}</td>
                <td>
                  {canRun && (
                    <button
                      type="button"
                      disabled={busy || !d.enabled}
                      onClick={() => act(`Pipeline ${d.key} run finished — see run history.`, () => client.runPipeline(d.key))}
                    >
                      Run now
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Run history</h3>
      {runList.length === 0 ? (
        <Empty label="No pipeline runs yet." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Run</th><th>Pipeline</th><th>Status</th><th>Steps</th><th>Started</th></tr>
          </thead>
          <tbody>
            {runList.slice(0, 10).map((r) => (
              <tr key={r.run_id}>
                <td><code>{r.run_id.slice(0, 8)}</code></td>
                <td>{r.pipeline_key}</td>
                <td>{r.status}</td>
                <td>{r.steps.map((s) => `${s.step_type}: ${s.status} (${s.rows_in}→${s.rows_out})`).join("; ")}</td>
                <td>{r.started_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Dead letters</h3>
      {letterList.length === 0 ? (
        <Empty label="No dead-lettered pipeline steps." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>#</th><th>Pipeline</th><th>Step</th><th>Error</th><th>Status</th><th /></tr>
          </thead>
          <tbody>
            {letterList.map((l) => (
              <tr key={l.id}>
                <td className="num">{l.id}</td>
                <td>{l.pipeline_key}</td>
                <td>{l.step_type}</td>
                <td>{l.error}</td>
                <td>{l.status}</td>
                <td>
                  {canRun && l.status === "open" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => act(`Dead letter #${l.id} replayed.`, () => client.replayPipelineDeadLetter(l.id))}
                    >
                      Replay
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

            </>
          ),
          quarantine: (
            <>
      <h3>Quarantine</h3>
      {holdList.length === 0 ? (
        <Empty label="Nothing held in quarantine." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>#</th><th>Source</th><th>Reason</th><th>Note</th><th /></tr>
          </thead>
          <tbody>
            {holdList.map((h) => (
              <tr key={h.id}>
                <td className="num">{h.id}</td>
                <td>{h.source}</td>
                <td>{h.reason}</td>
                <td>
                  <input
                    aria-label={`Decision note for hold ${h.id}`}
                    value={notes[h.id] ?? ""}
                    onChange={(e) => setNotes({ ...notes, [h.id]: e.target.value })}
                  />
                </td>
                <td>
                  {canManage && (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => act(`Hold #${h.id} released.`, () => client.releaseQuarantine(h.id, notes[h.id] || undefined))}
                      >
                        Release
                      </button>{" "}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => act(`Hold #${h.id} rejected.`, () => client.rejectQuarantine(h.id, notes[h.id] ?? ""))}
                      >
                        Reject
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

            </>
          ),
          dedup: (
            <>
      <h3>MPI dedup candidates</h3>
      <p className="muted">
        Probable duplicate patients from the deterministic matching rules in the published
        MPI config. Review is a human decision — the scan never merges records.
      </p>
      {candidateList.length === 0 ? (
        <Empty label="No pending dedup candidates." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Patient A</th><th>Patient B</th><th>Score</th><th>Signals</th><th /></tr>
          </thead>
          <tbody>
            {candidateList.map((c) => (
              <tr key={c.id}>
                <td>{c.patient_a}</td>
                <td>{c.patient_b}</td>
                <td className="num">{c.score}</td>
                <td>{Object.entries(c.reasons).map(([k, v]) => `${k}: ${v}`).join(", ")}</td>
                <td>
                  {canRun && (
                    <>
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => act(`Candidate #${c.id} marked not-duplicates.`, () => client.reviewDedupCandidate(c.id, "not-duplicates"))}
                      >
                        Not duplicates
                      </button>{" "}
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => act(`Candidate #${c.id} confirmed as duplicate (no merge performed).`, () => client.reviewDedupCandidate(c.id, "confirmed-duplicate"))}
                      >
                        Confirm duplicate
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
            </>
          ),
          empi: <EmpiTab client={client} />,
        }}
      />
    </section>
  );
}
