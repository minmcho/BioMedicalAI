import { useCallback, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Lab Depth W9 (LAB-D15 + LAB-D16) — POCT go-live & laboratory QMS.
//
// - D15 POCT deployment profile (governed Studio config
//   terminology/poct-deployment-profile): the effective enforcement
//   state at POCT result entry, the per-device lot + QC-range
//   CONFIGURATION RECORDS (what each device was told, with history)
//   and the per-device / per-operator QC review surface with
//   corrective-action comments.
//   HONEST BOUND, shown on this surface exactly as the API states
//   it: the PHYSICAL device lockout is an external leg — this
//   product enforces at its own result-entry surface and records
//   device configuration; it does not reach into device firmware.
// - D16 laboratory QMS: CLIA competency board + the competency
//   gating map (every test labelled GATED or UNGATED — unmapped
//   tests are honestly ungated), lookback + deviation lists, the
//   EQA exchange panel with the PT-failure corrective-action rule,
//   lab nonconformance intake (closure rides Wave C — labelled),
//   the SOP library (versions, periodic review, read-and-
//   acknowledge, overdue reviews) and the internal-audit programme.

export interface PoctProfile {
  profile_mode: string;
  profile_config: { kind: string; key: string; edit_in: string };
  programme_enforcement: { blockUncompetentOperator?: boolean; blockMissingDailyQc?: boolean };
  effective_enforcement: { block_uncompetent_operator: boolean; block_missing_daily_qc: boolean };
  enforced_at: string;
  external_leg: string;
}

export interface DeviceConfigRecord {
  id: number;
  device_key: string;
  reagent_lot: string;
  control_lot: string;
  qc_ranges: Record<string, { low: number; high: number }>;
  effective_from: string;
  effective_to?: string | null;
  note: string;
  recorded_by: string;
  current: boolean;
}

export interface QcReviewData {
  devices: Array<{
    device_key: string; qc_current: boolean;
    levels_today: Record<string, string | null>;
    runs_total: number; fails_total: number;
    current_config?: DeviceConfigRecord | null;
  }>;
  operators: Array<{ operator: string; runs: number; passes: number; fails: number; devices: string[] }>;
  reviews: Array<{ id: number; device_key?: string | null; staff_id?: string | null; comment: string; corrective_action: string; reviewer: string }>;
  external_leg: string;
}

export interface CompetencyRecord {
  id: number; staff_id: string; staff_name?: string | null;
  test_system: string; status: string; expires_on?: string | null;
  days_to_expiry?: number | null; missing_elements: string[];
}

export interface GatingData {
  tests: Array<{ code: string; display?: string | null; gated: boolean; required_test_system?: string | null; state_label: string }>;
  gated_count: number; ungated_count: number;
  config: { kind: string; key: string; edit_in: string };
}

export interface Nonconformance {
  nc_id: string; source_type: string; source_ref: string; title: string;
  severity: string; owner?: string | null; corrective_action: string;
  status: string; safety_event_id?: string | null;
  closure_state: string; closure_label: string;
}

export interface PtEvent {
  record_id: number; event: string; analyte_code: string;
  reported_value: number; acceptable_low: number; acceptable_high: number;
  passed: boolean; corrective_action_nc?: string | null;
  closure?: { closed_by: string; nonconformance_id?: string | null } | null;
  closeable: boolean;
}

export interface Sop {
  sop_id: string; sop_code: string; title: string; version: number;
  effective_date: string; review_due_date: string; status: string;
  review_overdue: boolean; acknowledgements: number;
}

export interface AuditItem {
  audit_id: string; title: string; checklist_template: string;
  checklist_items: string[]; scheduled_for: string; auditor: string;
  tracer_ref: string; status: string;
  findings: Array<{ id: number; finding: string; severity: string; nonconformance_id?: string | null }>;
}

function errText(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return err instanceof Error ? err.message : String(err);
}

type Async<T> = ReturnType<typeof useAsync<T>>;

export function LabQms({ client }: { client: ApiClient }) {
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);
  const profile = useAsync(() => client.request<PoctProfile>("/lab/qms/poct/profile"), [client, refreshKey]);
  const deviceConfigs = useAsync(
    () => client.request<{ items: DeviceConfigRecord[]; total: number; external_leg: string }>("/lab/qms/poct/device-config"),
    [client, refreshKey]);
  const qcReview = useAsync(() => client.request<QcReviewData>("/lab/qms/poct/qc-review"), [client, refreshKey]);
  const devices = useAsync(
    () => client.request<{ items: Array<{ device_key: string; name: string }>; total: number }>("/lab/poct/devices"),
    [client, refreshKey]);
  const board = useAsync(
    () => client.request<{ records: CompetencyRecord[]; total: number; worklist: { total: number; expired: number; incomplete: number } }>("/lab/qms/competency/board"),
    [client, refreshKey]);
  const gating = useAsync(() => client.request<GatingData>("/lab/qms/competency/gating"), [client, refreshKey]);
  const lookback = useAsync(
    () => client.request<{ items: Array<{ case_id: string; trigger_unit_id: string; status: string }>; total: number }>("/lab/qms/lookback"),
    [client, refreshKey]);
  const deviations = useAsync(
    () => client.request<{ items: Array<{ report_id: string; description: string; pdf_generated: boolean }>; total: number }>("/lab/qms/deviations"),
    [client, refreshKey]);
  const eqa = useAsync(
    () => client.request<{ enrolments: Array<{ enrolment_id: string; programme: string; status: string }>; total: number; pt_events: PtEvent[]; live_exchange: string }>("/lab/qms/eqa"),
    [client, refreshKey]);
  const ncs = useAsync(
    () => client.request<{ items: Nonconformance[]; total: number; closure_label: string }>("/lab/qms/nonconformances"),
    [client, refreshKey]);
  const sops = useAsync(
    () => client.request<{ items: Sop[]; total: number; overdue_reviews: Sop[] }>("/lab/qms/sops"),
    [client, refreshKey]);
  const audits = useAsync(
    () => client.request<{ items: AuditItem[]; total: number; checklist_templates: Array<{ code: string; display: string }> }>("/lab/qms/audits"),
    [client, refreshKey]);

  return (
    <div className="lab-qms">
      <h2>POCT go-live &amp; laboratory QMS</h2>
      <ProfileSection profile={profile} />
      <DeviceConfigSection client={client} configs={deviceConfigs} devices={devices} onChanged={refresh} />
      <QcReviewSection client={client} review={qcReview} devices={devices} onChanged={refresh} />
      <CompetencySection board={board} gating={gating} />
      <LookbackDeviationSection lookback={lookback} deviations={deviations} />
      <EqaSection client={client} eqa={eqa} onChanged={refresh} />
      <NonconformanceSection client={client} ncs={ncs} onChanged={refresh} />
      <SopSection client={client} sops={sops} board={board} onChanged={refresh} />
      <AuditSection client={client} audits={audits} onChanged={refresh} />
    </div>
  );
}

function ProfileSection({ profile }: { profile: Async<PoctProfile> }) {
  if (profile.loading) return <Loading label="Loading POCT deployment profile" />;
  if (profile.error) return <ErrorView error={profile.error} />;
  const data = profile.data;
  if (!data) return null;
  const eff = data.effective_enforcement;
  return (
    <section aria-label="POCT deployment profile">
      <h3>POCT deployment profile (LAB-D15)</h3>
      <p role="note">
        Governed Studio config {data.profile_config.kind}/{data.profile_config.key} (edit in {data.profile_config.edit_in}).
        Profile mode: <strong>{data.profile_mode}</strong>. Effective enforcement at {data.enforced_at}:{" "}
        block uncompetent operator — <strong>{eff.block_uncompetent_operator ? "ON" : "OFF"}</strong>;{" "}
        block missing/failed daily QC — <strong>{eff.block_missing_daily_qc ? "ON" : "OFF"}</strong>.
        {data.profile_mode === "track-only"
          ? " Track-only: legacy programme behaviour is preserved exactly."
          : " Enforce: the profile's blocks apply at POCT result entry in addition to the programme's own switches."}
      </p>
      <p role="note"><strong>{data.external_leg}</strong></p>
    </section>
  );
}

function DeviceConfigSection({ client, configs, devices, onChanged }: {
  client: ApiClient; configs: Async<{ items: DeviceConfigRecord[]; total: number; external_leg: string }>;
  devices: Async<{ items: Array<{ device_key: string; name: string }>; total: number }>;
  onChanged: () => void;
}) {
  const [deviceKey, setDeviceKey] = useState("");
  const [reagentLot, setReagentLot] = useState("");
  const [controlLot, setControlLot] = useState("");
  const [l1Low, setL1Low] = useState(""); const [l1High, setL1High] = useState("");
  const [l2Low, setL2Low] = useState(""); const [l2High, setL2High] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const deviceList = devices.data?.items ?? [];
  const noDevices = !devices.loading && deviceList.length === 0;

  const submit = async () => {
    setBusy(true); setError(null); setNote(null);
    try {
      await client.request("/lab/qms/poct/device-config", {
        method: "POST",
        body: JSON.stringify({
          device_key: deviceKey || deviceList[0]?.device_key,
          reagent_lot: reagentLot, control_lot: controlLot,
          qc_ranges: {
            L1: { low: Number(l1Low), high: Number(l1High) },
            L2: { low: Number(l2Low), high: Number(l2High) },
          },
        }),
      });
      setNote("Device configuration record saved (record of the push — the physical push is the external leg).");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="POCT device configuration records">
      <h3>Device configuration records — what each device was told</h3>
      {configs.loading && <Loading label="Loading device configuration records" />}
      {configs.error != null && <ErrorView error={configs.error} />}
      {configs.data && configs.data.items.length === 0 && (
        <Empty label="No device configuration records yet" />)}
      {configs.data && configs.data.items.length > 0 && (
        <table>
          <thead><tr><th>Device</th><th>Reagent lot</th><th>Control lot</th><th>QC ranges</th><th>Effective from</th><th>State</th></tr></thead>
          <tbody>
            {configs.data.items.map((row) => (
              <tr key={row.id}>
                <td>{row.device_key}</td><td>{row.reagent_lot}</td><td>{row.control_lot || "—"}</td>
                <td>{Object.entries(row.qc_ranges).map(([lvl, r]) => `${lvl}: ${r.low}–${r.high}`).join("; ")}</td>
                <td>{row.effective_from}</td><td>{row.current ? "current" : "history"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label>
          Device{" "}
          <select aria-label="Configuration device" value={deviceKey}
            onChange={(e) => setDeviceKey(e.target.value)} disabled={noDevices}>
            {deviceList.map((d) => <option key={d.device_key} value={d.device_key}>{d.device_key} — {d.name}</option>)}
          </select>
          {noDevices && <span> No POCT devices in the registry — configuration records need a registered device.</span>}
        </label>
        <label>Reagent lot <input aria-label="Reagent lot" value={reagentLot} onChange={(e) => setReagentLot(e.target.value)} /></label>
        <label>Control lot <input aria-label="Control lot" value={controlLot} onChange={(e) => setControlLot(e.target.value)} /></label>
        <label>L1 low <input aria-label="L1 low" value={l1Low} onChange={(e) => setL1Low(e.target.value)} /></label>
        <label>L1 high <input aria-label="L1 high" value={l1High} onChange={(e) => setL1High(e.target.value)} /></label>
        <label>L2 low <input aria-label="L2 low" value={l2Low} onChange={(e) => setL2Low(e.target.value)} /></label>
        <label>L2 high <input aria-label="L2 high" value={l2High} onChange={(e) => setL2High(e.target.value)} /></label>
        <button type="submit" disabled={busy || noDevices || !reagentLot}>
          {busy ? "Saving…" : "Record device configuration"}
        </button>
        {note && <span role="status"> {note}</span>}
        {error && <span role="alert"> {error}</span>}
      </form>
    </section>
  );
}

function QcReviewSection({ client, review, devices, onChanged }: {
  client: ApiClient; review: Async<QcReviewData>;
  devices: Async<{ items: Array<{ device_key: string; name: string }>; total: number }>;
  onChanged: () => void;
}) {
  const [deviceKey, setDeviceKey] = useState("");
  const [comment, setComment] = useState("");
  const [corrective, setCorrective] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const deviceList = devices.data?.items ?? [];

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      await client.request("/lab/qms/poct/qc-review", {
        method: "POST",
        body: JSON.stringify({ device_key: deviceKey || deviceList[0]?.device_key, comment, corrective_action: corrective }),
      });
      setComment(""); setCorrective("");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="POCT QC review">
      <h3>QC review — per device &amp; per operator</h3>
      {review.loading && <Loading label="Loading QC review" />}
      {review.error != null && <ErrorView error={review.error} />}
      {review.data && (
        <>
          {review.data.devices.length === 0 && <Empty label="No POCT QC recorded yet" />}
          {review.data.devices.map((d) => (
            <p key={d.device_key}>
              {d.device_key}: QC {d.qc_current ? "current" : "NOT current"} today
              ({Object.entries(d.levels_today).map(([lvl, r]) => `${lvl}: ${r ?? "not run"}`).join(", ")});{" "}
              {d.runs_total} run(s), {d.fails_total} fail(s) on record;{" "}
              configured reagent lot: {d.current_config?.reagent_lot ?? "no configuration record"}.
            </p>
          ))}
          {review.data.operators.map((o) => (
            <p key={o.operator}>Operator {o.operator}: {o.runs} QC run(s), {o.passes} pass, {o.fails} fail, on {o.devices.join(", ")}.</p>
          ))}
          {review.data.reviews.map((r) => (
            <p key={r.id}>
              Review by {r.reviewer} on {r.device_key ?? r.staff_id ?? "—"}: {r.comment}
              {r.corrective_action ? ` Corrective action: ${r.corrective_action}` : ""}
            </p>
          ))}
        </>
      )}
      <form onSubmit={(e) => { e.preventDefault(); void submit(); }}>
        <label>
          Device{" "}
          <select aria-label="Review device" value={deviceKey} onChange={(e) => setDeviceKey(e.target.value)}
            disabled={deviceList.length === 0}>
            {deviceList.map((d) => <option key={d.device_key} value={d.device_key}>{d.device_key}</option>)}
          </select>
          {deviceList.length === 0 && <span> No POCT devices in the registry — reviews need a registered device.</span>}
        </label>
        <label>Comment <input aria-label="Review comment" value={comment} onChange={(e) => setComment(e.target.value)} /></label>
        <label>Corrective action <input aria-label="Review corrective action" value={corrective} onChange={(e) => setCorrective(e.target.value)} /></label>
        <button type="submit" disabled={busy || deviceList.length === 0 || !comment}>
          {busy ? "Saving…" : "Record QC review"}
        </button>
        {error && <span role="alert"> {error}</span>}
      </form>
    </section>
  );
}

function CompetencySection({ board, gating }: {
  board: Async<{ records: CompetencyRecord[]; total: number; worklist: { total: number; expired: number; incomplete: number } }>;
  gating: Async<GatingData>;
}) {
  return (
    <section aria-label="CLIA competency board">
      <h3>CLIA competency board &amp; gating map (LAB-D16a/b)</h3>
      {board.loading && <Loading label="Loading competency board" />}
      {board.error != null && <ErrorView error={board.error} />}
      {board.data && (
        <>
          <p>
            {board.data.total} competency record(s); worklist: {board.data.worklist.total} due
            ({board.data.worklist.expired} expired, {board.data.worklist.incomplete} incomplete).
            Competency is checked at result entry AND at verification for gated tests.
          </p>
          {board.data.records.length === 0 && <Empty label="No CLIA competency records yet" />}
          {board.data.records.map((r) => (
            <p key={r.id}>
              {r.staff_name ?? r.staff_id} — {r.test_system}: <strong>{r.status}</strong>
              {r.expires_on ? ` (expires ${r.expires_on})` : ""}
              {r.missing_elements.length > 0 ? `; missing elements: ${r.missing_elements.join(", ")}` : ""}
            </p>
          ))}
        </>
      )}
      {gating.loading && <Loading label="Loading gating map" />}
      {gating.error != null && <ErrorView error={gating.error} />}
      {gating.data && (
        <>
          <p>
            Gating map (Studio config {gating.data.config.kind}/{gating.data.config.key}):{" "}
            {gating.data.gated_count} gated, {gating.data.ungated_count} ungated.{" "}
            <strong>Unmapped test systems are UNGATED — the surface says so per test; nothing is silently gated or silently ungated.</strong>
          </p>
          <ul>
            {gating.data.tests.map((t) => (
              <li key={t.code}>{t.code} — {t.state_label}</li>
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

function LookbackDeviationSection({ lookback, deviations }: {
  lookback: Async<{ items: Array<{ case_id: string; trigger_unit_id: string; status: string }>; total: number }>;
  deviations: Async<{ items: Array<{ report_id: string; description: string; pdf_generated: boolean }>; total: number }>;
}) {
  return (
    <section aria-label="Lookback and deviation reports">
      <h3>Lookback &amp; deviation / BPD reports (LAB-D16a)</h3>
      {lookback.loading && <Loading label="Loading lookback cases" />}
      {lookback.error != null && <ErrorView error={lookback.error} />}
      {lookback.data && lookback.data.items.length === 0 && <Empty label="No lookback cases" />}
      {lookback.data?.items.map((c) => (
        <p key={c.case_id}>Lookback {c.case_id} — trigger unit {c.trigger_unit_id}, status {c.status}.</p>
      ))}
      {deviations.loading && <Loading label="Loading deviation reports" />}
      {deviations.error != null && <ErrorView error={deviations.error} />}
      {deviations.data && deviations.data.items.length === 0 && <Empty label="No deviation reports" />}
      {deviations.data?.items.map((d) => (
        <p key={d.report_id}>Deviation {d.report_id} — {d.description} (PDF {d.pdf_generated ? "generated" : "not generated"}).</p>
      ))}
    </section>
  );
}

function EqaSection({ client, eqa, onChanged }: {
  client: ApiClient;
  eqa: Async<{ enrolments: Array<{ enrolment_id: string; programme: string; status: string }>; total: number; pt_events: PtEvent[]; live_exchange: string }>;
  onChanged: () => void;
}) {
  const [busyId, setBusyId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);

  const close = async (event: PtEvent) => {
    setBusyId(event.record_id); setError(null); setNote(null);
    try {
      await client.request(`/lab/qms/pt-events/${event.record_id}/close`, { method: "POST", body: "{}" });
      setNote(`PT record ${event.record_id} closed.`);
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusyId(null); }
  };

  return (
    <section aria-label="EQA exchange and PT events">
      <h3>EQA exchange &amp; PT corrective action (LAB-D16a/f)</h3>
      {eqa.loading && <Loading label="Loading EQA exchange" />}
      {eqa.error != null && <ErrorView error={eqa.error} />}
      {eqa.data && (
        <>
          <p>Provider exchange: {eqa.data.live_exchange}. {eqa.data.total} enrolment(s).</p>
          {eqa.data.enrolments.map((e) => (
            <p key={e.enrolment_id}>{e.programme} — {e.status}.</p>
          ))}
          <p>
            <strong>PT failure rule:</strong> a failed PT event requires an opened corrective action
            (a pt-failure nonconformance with corrective-action text) before the event can be closed;
            closing without one is refused by name.
          </p>
          {eqa.data.pt_events.length === 0 && <Empty label="No PT records yet" />}
          {eqa.data.pt_events.map((e) => (
            <p key={e.record_id}>
              PT record {e.record_id} ({e.event}, {e.analyte_code}): reported {e.reported_value},
              acceptable {e.acceptable_low}–{e.acceptable_high} — <strong>{e.passed ? "passed" : "FAILED"}</strong>
              {e.closure
                ? `; closed by ${e.closure.closed_by}${e.closure.nonconformance_id ? ` with corrective action ${e.closure.nonconformance_id}` : ""}.`
                : e.passed
                  ? "; open, closeable."
                  : `; open — ${e.corrective_action_nc ? `corrective action on file (${e.corrective_action_nc}), closeable.` : "no corrective action on file, NOT closeable."}`}
              {!e.closure && (
                <button type="button" onClick={() => void close(e)} disabled={busyId === e.record_id}>
                  {busyId === e.record_id ? "Closing…" : "Close PT event"}
                </button>
              )}
            </p>
          ))}
          {note && <span role="status"> {note}</span>}
          {error && <span role="alert"> {error}</span>}
        </>
      )}
    </section>
  );
}

const NC_SOURCES = ["specimen-rejection", "qc-failure", "corrected-report", "pt-failure", "audit-finding", "manual"];
const NC_STATUSES = ["open", "assigned", "in-progress", "corrective-action-recorded"];

function NonconformanceSection({ client, ncs, onChanged }: {
  client: ApiClient;
  ncs: Async<{ items: Nonconformance[]; total: number; closure_label: string }>;
  onChanged: () => void;
}) {
  const [sourceType, setSourceType] = useState("qc-failure");
  const [sourceRef, setSourceRef] = useState("");
  const [title, setTitle] = useState("");
  const [severity, setSeverity] = useState("major");
  const [owner, setOwner] = useState("");
  const [corrective, setCorrective] = useState("");
  const [assignId, setAssignId] = useState("");
  const [assignStatus, setAssignStatus] = useState("in-progress");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const raise = async () => {
    setBusy(true); setError(null);
    try {
      await client.request("/lab/qms/nonconformances", {
        method: "POST",
        body: JSON.stringify({
          source_type: sourceType, source_ref: sourceRef, title,
          severity, owner: owner || undefined, corrective_action: corrective,
        }),
      });
      setSourceRef(""); setTitle("");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  const assign = async () => {
    if (!assignId) return;
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/qms/nonconformances/${assignId}/assign`, {
        method: "POST",
        body: JSON.stringify({ status: assignStatus, corrective_action: corrective || undefined }),
      });
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Lab nonconformances">
      <h3>Lab nonconformance intake (LAB-D16c)</h3>
      {ncs.data && <p role="note"><strong>{ncs.data.closure_label}</strong></p>}
      {ncs.loading && <Loading label="Loading nonconformances" />}
      {ncs.error != null && <ErrorView error={ncs.error} />}
      {ncs.data && ncs.data.items.length === 0 && <Empty label="No nonconformances raised" />}
      {ncs.data?.items.map((nc) => (
        <p key={nc.nc_id}>
          {nc.nc_id} — [{nc.source_type}] {nc.title} (source {nc.source_ref}); severity {nc.severity};
          status {nc.status}; owner {nc.owner ?? "unassigned"};
          {nc.corrective_action ? ` corrective action: ${nc.corrective_action};` : " no corrective action yet;"}
          {" "}closure: {nc.closure_state}.
        </p>
      ))}
      <form onSubmit={(e) => { e.preventDefault(); void raise(); }}>
        <label>
          Source{" "}
          <select aria-label="Nonconformance source" value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
            {NC_SOURCES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>Source reference <input aria-label="Nonconformance source reference" value={sourceRef} onChange={(e) => setSourceRef(e.target.value)} /></label>
        <label>Title <input aria-label="Nonconformance title" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>
          Severity{" "}
          <select aria-label="Nonconformance severity" value={severity} onChange={(e) => setSeverity(e.target.value)}>
            <option value="minor">minor</option><option value="major">major</option><option value="critical">critical</option>
          </select>
        </label>
        <label>Owner (staff id) <input aria-label="Nonconformance owner" value={owner} onChange={(e) => setOwner(e.target.value)} /></label>
        <label>Corrective action <input aria-label="Nonconformance corrective action" value={corrective} onChange={(e) => setCorrective(e.target.value)} /></label>
        <button type="submit" disabled={busy || !sourceRef || !title}>
          {busy ? "Raising…" : "Raise nonconformance"}
        </button>
      </form>
      <form onSubmit={(e) => { e.preventDefault(); void assign(); }}>
        <label>
          Record{" "}
          <select aria-label="Assign nonconformance" value={assignId} onChange={(e) => setAssignId(e.target.value)}
            disabled={(ncs.data?.items.length ?? 0) === 0}>
            <option value="">Select a nonconformance</option>
            {ncs.data?.items.map((nc) => <option key={nc.nc_id} value={nc.nc_id}>{nc.nc_id} — {nc.title}</option>)}
          </select>
          {(ncs.data?.items.length ?? 0) === 0 && <span> No nonconformances to assign yet.</span>}
        </label>
        <label>
          Status{" "}
          <select aria-label="Assign status" value={assignStatus} onChange={(e) => setAssignStatus(e.target.value)}>
            {NC_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <button type="submit" disabled={busy || !assignId}>{busy ? "Saving…" : "Update assignment / status"}</button>
        {error && <span role="alert"> {error}</span>}
      </form>
    </section>
  );
}

function SopSection({ client, sops, board, onChanged }: {
  client: ApiClient;
  sops: Async<{ items: Sop[]; total: number; overdue_reviews: Sop[] }>;
  board: Async<{ records: CompetencyRecord[]; total: number; worklist: { total: number; expired: number; incomplete: number } }>;
  onChanged: () => void;
}) {
  const [code, setCode] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [staffId, setStaffId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const staffIds = Array.from(new Set((board.data?.records ?? []).map((r) => r.staff_id)));

  const create = async () => {
    setBusy(true); setError(null);
    try {
      await client.request("/lab/qms/sops", {
        method: "POST",
        body: JSON.stringify({ sop_code: code, title, body }),
      });
      setCode(""); setTitle(""); setBody("");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  const acknowledge = async (sop: Sop) => {
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/qms/sops/${sop.sop_id}/acknowledge`, {
        method: "POST",
        body: JSON.stringify({ staff_id: staffId || staffIds[0] }),
      });
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="SOP library">
      <h3>SOP / procedure library (LAB-D16d)</h3>
      {sops.loading && <Loading label="Loading SOP library" />}
      {sops.error != null && <ErrorView error={sops.error} />}
      {sops.data && (
        <>
          <p>
            Overdue periodic reviews: {sops.data.overdue_reviews.length}
            {sops.data.overdue_reviews.map((s) => ` ${s.sop_code} v${s.version} (due ${s.review_due_date})`).join(";")}
          </p>
          {sops.data.items.length === 0 && <Empty label="No procedures in the library yet" />}
          {sops.data.items.map((s) => (
            <p key={s.sop_id}>
              {s.sop_code} v{s.version} — {s.title}; {s.status}; effective {s.effective_date};
              review due {s.review_due_date}{s.review_overdue ? " — REVIEW OVERDUE" : ""};{" "}
              {s.acknowledgements} acknowledgement(s).
              {s.status === "current" && (
                <button type="button" onClick={() => void acknowledge(s)} disabled={busy || staffIds.length === 0}>
                  Acknowledge this version
                </button>
              )}
            </p>
          ))}
        </>
      )}
      <label>
        Acknowledge as staff{" "}
        <select aria-label="Acknowledge as staff" value={staffId} onChange={(e) => setStaffId(e.target.value)}
          disabled={staffIds.length === 0}>
          {staffIds.map((id) => <option key={id} value={id}>{id}</option>)}
        </select>
        {staffIds.length === 0 && <span> No staff with competency records yet — acknowledgement needs a staff identity from the competency board.</span>}
      </label>
      <form onSubmit={(e) => { e.preventDefault(); void create(); }}>
        <label>SOP code <input aria-label="SOP code" value={code} onChange={(e) => setCode(e.target.value)} /></label>
        <label>Title <input aria-label="SOP title" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>Procedure text <input aria-label="SOP body" value={body} onChange={(e) => setBody(e.target.value)} /></label>
        <button type="submit" disabled={busy || !code || !title || !body}>
          {busy ? "Saving…" : "Publish SOP version"}
        </button>
        {error && <span role="alert"> {error}</span>}
      </form>
    </section>
  );
}

function AuditSection({ client, audits, onChanged }: {
  client: ApiClient;
  audits: Async<{ items: AuditItem[]; total: number; checklist_templates: Array<{ code: string; display: string }> }>;
  onChanged: () => void;
}) {
  const [title, setTitle] = useState("");
  const [template, setTemplate] = useState("");
  const [auditor, setAuditor] = useState("");
  const [auditId, setAuditId] = useState("");
  const [finding, setFinding] = useState("");
  const [raiseNc, setRaiseNc] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const templates = audits.data?.checklist_templates ?? [];
  const auditItems = audits.data?.items ?? [];

  const schedule = async () => {
    setBusy(true); setError(null);
    try {
      await client.request("/lab/qms/audits", {
        method: "POST",
        body: JSON.stringify({ title, checklist_template: template || templates[0]?.code, auditor }),
      });
      setTitle("");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  const addFinding = async () => {
    const target = auditId || auditItems[0]?.audit_id;
    if (!target) return;
    setBusy(true); setError(null);
    try {
      await client.request(`/lab/qms/audits/${target}/findings`, {
        method: "POST",
        body: JSON.stringify({ finding, raise_nonconformance: raiseNc }),
      });
      setFinding("");
      onChanged();
    } catch (err) { setError(errText(err)); } finally { setBusy(false); }
  };

  return (
    <section aria-label="Internal audit programme">
      <h3>Internal-audit programme (LAB-D16e)</h3>
      {audits.loading && <Loading label="Loading audit programme" />}
      {audits.error != null && <ErrorView error={audits.error} />}
      {audits.data && audits.data.items.length === 0 && <Empty label="No internal audits scheduled" />}
      {auditItems.map((a) => (
        <div key={a.audit_id}>
          <p>
            {a.audit_id} — {a.title}; template {a.checklist_template || "custom"}; scheduled {a.scheduled_for};
            auditor {a.auditor}; status {a.status}; tracer {a.tracer_ref || "—"}.
          </p>
          {a.findings.map((f) => (
            <p key={f.id}>
              Finding: {f.finding} (severity {f.severity})
              {f.nonconformance_id ? ` — raised nonconformance ${f.nonconformance_id}` : ""}
            </p>
          ))}
        </div>
      ))}
      <form onSubmit={(e) => { e.preventDefault(); void schedule(); }}>
        <label>Audit title <input aria-label="Audit title" value={title} onChange={(e) => setTitle(e.target.value)} /></label>
        <label>
          Checklist template{" "}
          <select aria-label="Checklist template" value={template} onChange={(e) => setTemplate(e.target.value)}
            disabled={templates.length === 0}>
            {templates.map((t) => <option key={t.code} value={t.code}>{t.display}</option>)}
          </select>
          {templates.length === 0 && <span> No governed checklist templates published.</span>}
        </label>
        <label>Auditor (staff id) <input aria-label="Auditor" value={auditor} onChange={(e) => setAuditor(e.target.value)} /></label>
        <button type="submit" disabled={busy || !title || !auditor || templates.length === 0}>
          {busy ? "Scheduling…" : "Schedule audit"}
        </button>
      </form>
      <form onSubmit={(e) => { e.preventDefault(); void addFinding(); }}>
        <label>
          Audit{" "}
          <select aria-label="Finding audit" value={auditId} onChange={(e) => setAuditId(e.target.value)}
            disabled={auditItems.length === 0}>
            <option value="">Select an audit</option>
            {auditItems.map((a) => <option key={a.audit_id} value={a.audit_id}>{a.title}</option>)}
          </select>
          {auditItems.length === 0 && <span> Schedule an audit before recording findings.</span>}
        </label>
        <label>Finding <input aria-label="Audit finding" value={finding} onChange={(e) => setFinding(e.target.value)} /></label>
        <label>
          Raise nonconformance{" "}
          <input type="checkbox" aria-label="Raise nonconformance" checked={raiseNc}
            onChange={(e) => setRaiseNc(e.target.checked)} />
        </label>
        <button type="submit" disabled={busy || auditItems.length === 0 || !finding}>
          {busy ? "Saving…" : "Record finding"}
        </button>
        {error && <span role="alert"> {error}</span>}
      </form>
    </section>
  );
}
