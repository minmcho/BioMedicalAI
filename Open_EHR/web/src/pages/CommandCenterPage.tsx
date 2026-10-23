import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import {
  createAccessCall,
  createHuddle,
  createStaffing,
  createTriageDecision,
  listAccessCalls,
  listDesks,
  listHuddles,
  listStaffing,
  listTriageDecisions,
  supportConfig,
} from "../api/flows";
import type { Role, TransferRequestItem } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import {
  CoordinatorPanel,
  EConsultsPanel,
  EpisodesPanel,
  StateBadge,
  useFlowActions,
} from "./CareFlows";

interface Props {
  client: ApiClient;
  role: Role;
  /** Deep-link section (the LabPage/PharmacyPage contract): the
   *  shell's section wins when provided — "desks" / "access-line"
   *  come from the command nav sections in session.ts. */
  section?: string;
  onSectionChange?: (section: string) => void;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const SURGE_OPTIONS = ["normal", "elevated", "surge", "critical"];
const DIVERSION_OPTIONS = ["open", "diversion-ed", "diversion-trauma", "internal-disaster"];

const COMMAND_TABS = [
  { key: "overview", label: "Overview" },
  { key: "desks", label: "Desks" },
  { key: "access-line", label: "Access line" },
  { key: "econsults", label: "eConsults" },
  { key: "episodes", label: "Episodes" },
  { key: "coordinator", label: "Coordinator panels" },
];

// ---------------------------------------------------------------------------
// BD-13 — Command centre as a staffed unit: desks, staffing, huddles,
// ICU triage decisions (published terminology/command-desks config)
// ---------------------------------------------------------------------------

function DesksPanel({ client, role }: Props) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [staffForm, setStaffForm] = useState({ desk: "", staff: "", starts_at: "", ends_at: "" });
  const [huddleForm, setHuddleForm] = useState({
    desk: "", census: "0", boarding: "0", expected: "0",
    chaired_by: "", decisions: "", note: "",
  });
  const [triageForm, setTriageForm] = useState({
    patient_ref: "", decision: "admit-icu", reason: "",
  });

  const configState = useAsync(() => supportConfig(client), [client]);
  const desksState = useAsync(() => listDesks(client), [client, tick]);
  const staffingState = useAsync(() => listStaffing(client), [client, tick]);
  const huddlesState = useAsync(() => listHuddles(client), [client, tick]);
  const triageState = useAsync(() => listTriageDecisions(client), [client, tick]);

  const desksConfig = configState.data?.["command-desks"];
  const desks = desksState.data?.items ?? desksConfig?.desks ?? [];
  const authorityRole =
    desksState.data?.triage_authority_role ?? desksConfig?.triageAuthorityRole ?? "physician";
  const authorityDisplay =
    desksConfig?.triageAuthorityDisplay ?? "ICU triage authority (HTRIP-equivalent)";
  const decisionOptions = desksConfig?.triageDecisions ?? ["admit-icu", "defer", "decline"];
  // The server accepts a triage decision only from the published
  // triage-authority job role (staff directory) or an admin sign-in.
  // The web layer knows the app role, so the action is enabled for
  // admins here; for everyone else it is disabled with the reason —
  // and the server still enforces the authority check on every post.
  const canTriage = role === "admin";
  const effectiveDesk = staffForm.desk || desks[0]?.code || "";
  const effectiveHuddleDesk = huddleForm.desk || desks[0]?.code || "";

  return (
    <div>
      <h3>Command desks</h3>
      <p className="hint">
        Desks, their role lists and huddle cadences are the published
        terminology/command-desks config — nothing here is hardcoded
        site data.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      {desksState.loading && <Loading label="Loading command desks…" />}
      {desksState.error != null && <ErrorView error={desksState.error} />}
      {desksState.data && desks.length === 0 && <Empty label="No command desks configured." />}
      <ul className="board-list">
        {desks.map((d) => (
          <li key={d.code} className="board-card">
            <div>
              <strong>{d.display}</strong>
              <span className="badge">{d.code}</span>
              {d.huddleCadence && <span className="badge">huddles: {d.huddleCadence}</span>}
            </div>
            <div className="hint">
              Staffed by roles: {(d.roles ?? []).join(", ") || "—"}
            </div>
          </li>
        ))}
      </ul>

      <h4>Staffing roster</h4>
      {staffingState.error != null && <ErrorView error={staffingState.error} />}
      {staffingState.data && staffingState.data.items.length === 0 && (
        <Empty label="No staffing recorded." />
      )}
      <ul className="board-list">
        {staffingState.data?.items.map((s) => (
          <li key={s.id} className="board-card">
            <div>
              <strong>{s.staff_name}</strong>
              <span className="badge">{s.desk}</span>
            </div>
            <div className="hint">{s.starts_at ?? "—"} → {s.ends_at ?? "—"}</div>
          </li>
        ))}
      </ul>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!effectiveDesk || !staffForm.staff.trim() || !staffForm.starts_at || !staffForm.ends_at) return;
          void perform("Staffing recorded", async () => {
            await createStaffing(client, {
              desk: effectiveDesk,
              staff: staffForm.staff.trim(),
              starts_at: staffForm.starts_at,
              ends_at: staffForm.ends_at,
            });
            setStaffForm({ ...staffForm, staff: "" });
          });
        }}
      >
        <h5>Add a shift</h5>
        <label>
          Desk{" "}
          <select aria-label="Staffing desk" value={effectiveDesk}
            onChange={(e) => setStaffForm({ ...staffForm, desk: e.target.value })}>
            {desks.map((d) => <option key={d.code} value={d.code}>{d.display}</option>)}
          </select>
        </label>
        <label>
          Staff (directory username or id){" "}
          <input aria-label="Staffing staff" value={staffForm.staff}
            onChange={(e) => setStaffForm({ ...staffForm, staff: e.target.value })} />
        </label>
        <label>
          Starts{" "}
          <input aria-label="Shift starts" type="datetime-local" value={staffForm.starts_at}
            onChange={(e) => setStaffForm({ ...staffForm, starts_at: e.target.value })} />
        </label>
        <label>
          Ends{" "}
          <input aria-label="Shift ends" type="datetime-local" value={staffForm.ends_at}
            onChange={(e) => setStaffForm({ ...staffForm, ends_at: e.target.value })} />
        </label>
        <button type="submit" disabled={busy}>Add staffing</button>
        <p className="hint">
          Overlapping shifts for the same person on the same desk are
          refused by the server (409) and the refusal is shown above.
        </p>
      </form>

      <h4>Capacity huddles</h4>
      {huddlesState.error != null && <ErrorView error={huddlesState.error} />}
      {huddlesState.data && huddlesState.data.items.length === 0 && (
        <Empty label="No huddle records yet." />
      )}
      <ul className="board-list">
        {huddlesState.data?.items.map((h) => (
          <li key={h.id} className="board-card">
            <div>
              <strong>{h.desk ?? "House-wide huddle"}</strong>
              <span className="badge">census {h.census}</span>
              <span className="badge">boarding {h.boarding}</span>
              <span className="badge">expected discharges {h.expected_discharges}</span>
            </div>
            <div className="hint">
              Chaired by {h.chaired_by} · {h.held_at ?? ""}
              {Array.isArray(h.decisions) && h.decisions.length > 0
                ? ` · decisions: ${h.decisions.map((d) => String(d)).join("; ")}`
                : ""}
            </div>
            {h.note && <div className="hint">{h.note}</div>}
          </li>
        ))}
      </ul>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!huddleForm.chaired_by.trim()) return;
          void perform("Huddle recorded", async () => {
            await createHuddle(client, {
              desk: effectiveHuddleDesk || undefined,
              census: Number(huddleForm.census) || 0,
              boarding: Number(huddleForm.boarding) || 0,
              expected_discharges: Number(huddleForm.expected) || 0,
              decisions: huddleForm.decisions
                .split(";")
                .map((d) => d.trim())
                .filter(Boolean),
              chaired_by: huddleForm.chaired_by.trim(),
              note: huddleForm.note.trim() || undefined,
            });
            setHuddleForm({ ...huddleForm, decisions: "", note: "" });
          });
        }}
      >
        <h5>Record a huddle (the record IS the meeting artefact)</h5>
        <label>
          Desk{" "}
          <select aria-label="Huddle desk" value={effectiveHuddleDesk}
            onChange={(e) => setHuddleForm({ ...huddleForm, desk: e.target.value })}>
            {desks.map((d) => <option key={d.code} value={d.code}>{d.display}</option>)}
          </select>
        </label>
        <label>
          Census{" "}
          <input aria-label="Huddle census" type="number" min="0" value={huddleForm.census}
            onChange={(e) => setHuddleForm({ ...huddleForm, census: e.target.value })} />
        </label>
        <label>
          Boarding{" "}
          <input aria-label="Huddle boarding" type="number" min="0" value={huddleForm.boarding}
            onChange={(e) => setHuddleForm({ ...huddleForm, boarding: e.target.value })} />
        </label>
        <label>
          Expected discharges{" "}
          <input aria-label="Huddle expected discharges" type="number" min="0"
            value={huddleForm.expected}
            onChange={(e) => setHuddleForm({ ...huddleForm, expected: e.target.value })} />
        </label>
        <label>
          Chaired by{" "}
          <input aria-label="Huddle chaired by" value={huddleForm.chaired_by}
            onChange={(e) => setHuddleForm({ ...huddleForm, chaired_by: e.target.value })} />
        </label>
        <label>
          Decisions (separated by ;){" "}
          <input aria-label="Huddle decisions" value={huddleForm.decisions}
            onChange={(e) => setHuddleForm({ ...huddleForm, decisions: e.target.value })} />
        </label>
        <button type="submit" disabled={busy}>Record huddle</button>
      </form>

      <h4>ICU triage decisions</h4>
      {!canTriage && (
        <p className="hint" role="note">
          Triage decisions are restricted: only the {authorityDisplay} —
          the published triage-authority role “{authorityRole}” in the
          staff directory — or an admin may record one. Your role
          ({role}) is not the triage authority, so the action is
          disabled here; the server enforces the same check on every
          decision.
        </p>
      )}
      {triageState.error != null && <ErrorView error={triageState.error} />}
      {triageState.data && triageState.data.items.length === 0 && (
        <Empty label="No triage decisions recorded." />
      )}
      <ul className="board-list">
        {triageState.data?.items.map((t) => (
          <li key={t.id} className="board-card">
            <div>
              <strong>{refText(t.patient_ref)}</strong>
              <StatusBadge tone={t.decision === "admit-icu" ? "ok" : "warn"}>
                {t.decision}
              </StatusBadge>
              <span className="badge">authority: {t.authority_role}</span>
            </div>
            <div className="hint">
              Decided by {t.decided_by}
              {t.reason ? ` — ${t.reason}` : ""}
            </div>
          </li>
        ))}
      </ul>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!canTriage || !triageForm.patient_ref.trim()) return;
          void perform("Triage decision recorded", async () => {
            await createTriageDecision(client, {
              patient_ref: triageForm.patient_ref.trim(),
              decision: triageForm.decision,
              reason: triageForm.reason.trim() || undefined,
            });
            setTriageForm({ ...triageForm, patient_ref: "", reason: "" });
          });
        }}
      >
        <h5>Record a triage decision</h5>
        <label>
          Patient{" "}
          <input aria-label="Triage patient" value={triageForm.patient_ref}
            placeholder="Patient/…"
            onChange={(e) => setTriageForm({ ...triageForm, patient_ref: e.target.value })} />
        </label>
        <label>
          Decision{" "}
          <select aria-label="Triage decision" value={triageForm.decision}
            onChange={(e) => setTriageForm({ ...triageForm, decision: e.target.value })}>
            {decisionOptions.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <label>
          Reason{" "}
          <input aria-label="Triage reason" value={triageForm.reason}
            onChange={(e) => setTriageForm({ ...triageForm, reason: e.target.value })} />
        </label>
        <button type="submit" disabled={busy || !canTriage}>
          Record triage decision
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// BD-14 — Access-line call centre (published terminology/access-line)
// ---------------------------------------------------------------------------

function AccessLinePanel({ client, role }: Props) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [form, setForm] = useState({
    caller_name: "", caller_org: "", referral_source: "",
    patient_name: "", patient_ref: "", patient_summary: "",
    disposition: "advice",
  });
  const [lastTransferId, setLastTransferId] = useState<string | null>(null);

  const configState = useAsync(() => supportConfig(client), [client]);
  const callsState = useAsync(() => listAccessCalls(client), [client, tick]);

  const accessConfig = configState.data?.["access-line"];
  const sources = accessConfig?.referralSources ?? [];
  const dispositions = accessConfig?.dispositions ?? ["advice", "transfer-requested", "clinic-referral", "other"];
  const effectiveSource = form.referral_source || sources[0]?.code || "other";

  return (
    <div>
      <h3>Access line</h3>
      <p className="hint">
        Calls are logged against the published access-line referral
        sources. A “transfer-requested” disposition creates a REAL
        Phase 12 TransferRequest in the transfer centre — this log
        holds a queued intake record referencing it; acceptance is
        decided there, not here.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      {lastTransferId && (
        <p className="board-card" role="status">
          Transfer requested — Phase 12 TransferRequest{" "}
          <strong>{lastTransferId}</strong> created and queued for the
          transfer centre.
        </p>
      )}

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!form.caller_name.trim() || !form.patient_summary.trim()) return;
          void perform("Call logged", async () => {
            const created = await createAccessCall(client, {
              caller_name: form.caller_name.trim(),
              caller_org: form.caller_org.trim() || undefined,
              referral_source: effectiveSource,
              patient_name: form.patient_name.trim() || undefined,
              patient_ref: form.patient_ref.trim() || undefined,
              patient_summary: form.patient_summary.trim(),
              disposition: form.disposition,
            });
            setLastTransferId(created.transfer_request_id ?? null);
            setForm({
              caller_name: "", caller_org: "", referral_source: "",
              patient_name: "", patient_ref: "", patient_summary: "",
              disposition: "advice",
            });
          });
        }}
      >
        <h4>Log a call</h4>
        <label>
          Caller name{" "}
          <input aria-label="Caller name" value={form.caller_name}
            onChange={(e) => setForm({ ...form, caller_name: e.target.value })} />
        </label>
        <label>
          Caller organisation{" "}
          <input aria-label="Caller organisation" value={form.caller_org}
            onChange={(e) => setForm({ ...form, caller_org: e.target.value })} />
        </label>
        <label>
          Referral source{" "}
          <select aria-label="Referral source" value={effectiveSource}
            onChange={(e) => setForm({ ...form, referral_source: e.target.value })}>
            {sources.map((s) => <option key={s.code} value={s.code}>{s.display}</option>)}
            {sources.length === 0 && <option value="other">Other</option>}
          </select>
        </label>
        <label>
          Patient name{" "}
          <input aria-label="Access patient name" value={form.patient_name}
            onChange={(e) => setForm({ ...form, patient_name: e.target.value })} />
        </label>
        <label>
          Patient ref (optional){" "}
          <input aria-label="Access patient ref" value={form.patient_ref}
            placeholder="Patient/…"
            onChange={(e) => setForm({ ...form, patient_ref: e.target.value })} />
        </label>
        <label>
          Summary{" "}
          <input aria-label="Call summary" value={form.patient_summary}
            onChange={(e) => setForm({ ...form, patient_summary: e.target.value })} />
        </label>
        <label>
          Disposition{" "}
          <select aria-label="Call disposition" value={form.disposition}
            onChange={(e) => setForm({ ...form, disposition: e.target.value })}>
            {dispositions.map((d) => <option key={d} value={d}>{d}</option>)}
          </select>
        </label>
        <button type="submit" disabled={busy}>Log call</button>
      </form>

      <h4>Call log</h4>
      {callsState.loading && <Loading label="Loading access-line calls…" />}
      {callsState.error != null && <ErrorView error={callsState.error} />}
      {callsState.data && callsState.data.items.length === 0 && (
        <Empty label="No access-line calls logged." />
      )}
      <ul className="board-list">
        {callsState.data?.items.map((c) => (
          <li key={c.id} className="board-card">
            <div>
              <strong>{c.caller_name}</strong>
              <span className="badge">{c.referral_source}</span>
              <StateBadge state={c.disposition} />
            </div>
            <div className="hint">
              {c.caller_org ? `${c.caller_org} · ` : ""}
              {c.patient_name ?? refText(c.patient_ref)} — {c.patient_summary}
            </div>
            {c.transfer_request_id && (
              <div className="hint">
                Phase 12 TransferRequest: <strong>{c.transfer_request_id}</strong>
                {c.transfer_linkage?.note ? ` — ${c.transfer_linkage.note}` : ""}
              </div>
            )}
          </li>
        ))}
      </ul>
      <p className="hint">Signed in as {role}; call logging follows support.work server-side.</p>
    </div>
  );
}

/** Phase 12 Capacity Command Center: one operations surface over the
 *  live modules — census + deterministic projection, bed-request queue
 *  with assign, transfer centre, transport dispatch and the surge /
 *  diversion banner. Nothing here is fixture data; every number comes
 *  from /continuum/command/*.
 *
 *  Phase 15 adds the care-flow views as tabs: Desks + Access line
 *  (BD-13/BD-14, app.support_ops) and the eConsults / Episodes /
 *  Coordinator panels (app.care_coordination, ./CareFlows). The
 *  default view is the original overview, unchanged. */
export function CommandCenterPage({ client, role, section, onSectionChange }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["continuum.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [beds, setBeds] = useState<Record<string, string>>({});
  const [declineReasons, setDeclineReasons] = useState<Record<string, string>>({});
  const [intake, setIntake] = useState({ name: "", facility: "", reason: "" });
  // Tab state: the shell's section prop wins when provided (nav
  // deep-link); otherwise the page manages its own active tab.
  const [tabLocal, setTabLocal] = useState("overview");
  const activeTab = section ?? tabLocal;
  const setActiveTab = (key: string) => {
    setTabLocal(key);
    onSectionChange?.(key);
  };

  const overviewState = useAsync(() => client.commandOverview(), [client, tick]);
  const transfersState = useAsync(() => client.transferRequests(), [client, tick]);
  const transportState = useAsync(() => client.transportRequests(), [client, tick]);
  const overview = overviewState.data;

  // Phase 17 (BD-69): realtime fan-out — the overview refreshes when
  // a board.changed event arrives (SSE stream; automatic polling
  // fallback inside connectCommandRealtime). The badge states the
  // live transport honestly: "Live" only while the stream is held.
  const [liveMode, setLiveMode] = useState<"stream" | "polling">("polling");
  useEffect(() => {
    // A client without the realtime method (older builds, partial
    // test doubles) simply keeps the manual-refresh behaviour.
    if (typeof client.connectCommandRealtime !== "function") return;
    const conn = client.connectCommandRealtime({
      onEvent: () => setTick((t) => t + 1),
      onMode: (m) => setLiveMode(m),
    });
    return () => conn.close();
  }, [client]);

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

  function renderTransfer(t: TransferRequestItem) {
    return (
      <li key={t.id} className="board-card">
        <div>
          <strong>{t.patient_name}</strong>
          <StatusBadge
            tone={t.status === "accepted" ? "ok" : t.status === "declined" ? "bad" : "warn"}
          >
            {t.status}
          </StatusBadge>
          {t.urgency && (
            <StatusBadge tone={t.urgency === "emergent" || t.urgency === "urgent" ? "warn" : "neutral"}>
              {t.urgency}
            </StatusBadge>
          )}
        </div>
        <div className="hint">
          From {t.referring_facility}
          {t.service_line ? ` · ${t.service_line}` : ""} — {t.reason}
        </div>
        {t.status === "declined" && t.decision_reason && (
          <div className="hint">Declined: {t.decision_reason}</div>
        )}
        {t.status === "accepted" && t.admission_task_id && (
          <div className="hint">Admission task {t.admission_task_id} created.</div>
        )}
        {canWork && t.status === "pending" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() => perform("Transfer accepted", () => client.decideTransfer(t.id, "accept"))}
            >
              Accept
            </button>
            <input
              aria-label={`Decline reason for ${t.id}`}
              placeholder="Decline reason (required)"
              value={declineReasons[t.id] ?? ""}
              onChange={(e) =>
                setDeclineReasons((prev) => ({ ...prev, [t.id]: e.target.value }))}
            />
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform("Transfer declined", () =>
                  client.decideTransfer(t.id, "decline", declineReasons[t.id] ?? ""))}
            >
              Decline
            </button>
          </div>
        )}
      </li>
    );
  }

  // Defensive boarding (page↔API contract): GET /continuum/command/
  // overview carries boarding as {total, items, ed_total,
  // oldest_wait_minutes} (backend/app/continuum.py), but a payload
  // without the object — an older deployment, a scoped variant — must
  // degrade to the flat boarding_count, never crash the board.
  const boarding = overview?.boarding ?? {
    total: overview?.boarding_count ?? 0,
    items: [],
    ed_total: overview?.ed_boarding_count ?? 0,
    oldest_wait_minutes: 0,
  };

  const overviewPanel = (
    <>
      {overviewState.loading && <Loading label="Loading command overview…" />}
      {overviewState.error != null && <ErrorView error={overviewState.error} />}
      {overview && (
        <>
          <div className="board-card" role="status">
            <span className="badge" data-testid="realtime-mode">
              {liveMode === "stream" ? "Live updates: streaming" : "Live updates: polling"}
            </span>{" "}
            <strong>
              {overview.diversion === "open"
                ? "Accepting all arrivals"
                : `DIVERSION: ${overview.diversion}`}
            </strong>{" "}
            · Surge level: <strong>{overview.surge_level}</strong> · ED boarding:{" "}
            <strong>{overview.boarding_count}</strong> ({overview.ed_boarding_count} from ED) ·
            Discharges today: {overview.discharges_today.total} (
            {overview.discharges_today.before_noon} before noon)
          </div>
          {canWork && (
            <div className="row-actions">
              <label>
                Diversion{" "}
                <select
                  aria-label="Diversion state"
                  defaultValue={overview.diversion}
                  id="cc-diversion"
                >
                  {DIVERSION_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </label>
              <label>
                Surge{" "}
                <select aria-label="Surge level" defaultValue={overview.surge_level} id="cc-surge">
                  {SURGE_OPTIONS.map((s) => (
                    <option key={s} value={s}>{s}</option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                disabled={busy}
                onClick={() => {
                  const status = (document.getElementById("cc-diversion") as HTMLSelectElement).value;
                  const surgeLevel = (document.getElementById("cc-surge") as HTMLSelectElement).value;
                  void perform("Diversion updated", () => client.setDiversion({ status, surgeLevel }));
                }}
              >
                Update banner
              </button>
            </div>
          )}

          <h3>Census by unit</h3>
          <ul className="board-list">
            {overview.census.units.map((u) => (
              // The real census payload keys units by `code`
              // (backend inpatient census) — pinned by the WS6
              // page-contract test against the live response shape.
              <li key={u.code} className="board-card">
                <strong>{u.display}</strong>{" "}
                <span className="badge">{u.occupied}/{u.beds_total} occupied</span>
                <span className="badge">{u.available} available</span>
                <span className="badge">{u.cleaning} cleaning</span>
                <span className="badge">{u.occupancy_pct}% occupancy</span>
              </li>
            ))}
          </ul>
          <p className="hint">
            Projected occupied end of day:{" "}
            <strong>{overview.projection.projected_occupied_end_of_day}</strong> —{" "}
            {overview.projection.basis}
          </p>

          <h3>Bed requests ({boarding.total})</h3>
          {boarding.total === 0 && <Empty label="No patients are waiting for a bed." />}
          <ul className="board-list">
            {boarding.items.map((item) => (
              <li key={item.encounter_id} className="board-card">
                <div>
                  <strong>{refText(item.patient_ref)}</strong>
                  <span className="badge">{item.source === "ed" ? "from ED" : "direct admit"}</span>
                  <span className="badge">waiting {item.wait_minutes} min</span>
                </div>
                {canWork && (
                  <div className="row-actions">
                    <input
                      aria-label={`Bed for ${item.encounter_id}`}
                      placeholder="Bed code (e.g. MS-101A)"
                      value={beds[item.encounter_id] ?? ""}
                      onChange={(e) =>
                        setBeds((prev) => ({ ...prev, [item.encounter_id]: e.target.value }))}
                    />
                    <button
                      type="button"
                      disabled={busy || !(beds[item.encounter_id] ?? "").trim()}
                      onClick={() =>
                        perform("Bed assigned", () =>
                          client.commandAssignBed(item.encounter_id, beds[item.encounter_id].trim()))}
                    >
                      Assign bed
                    </button>
                  </div>
                )}
              </li>
            ))}
          </ul>
        </>
      )}

      <h3>Transfer centre</h3>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!intake.name.trim() || !intake.facility.trim() || !intake.reason.trim()) return;
            void perform("Transfer request intaked", async () => {
              await client.createTransferRequest({
                patientName: intake.name.trim(),
                referringFacility: intake.facility.trim(),
                reason: intake.reason.trim(),
              });
              setIntake({ name: "", facility: "", reason: "" });
            });
          }}
        >
          <h4>New external transfer request</h4>
          <label>
            Patient name{" "}
            <input aria-label="Transfer patient name" value={intake.name}
              onChange={(e) => setIntake({ ...intake, name: e.target.value })} />
          </label>
          <label>
            Referring facility{" "}
            <input aria-label="Referring facility" value={intake.facility}
              onChange={(e) => setIntake({ ...intake, facility: e.target.value })} />
          </label>
          <label>
            Reason{" "}
            <input aria-label="Transfer reason" value={intake.reason}
              onChange={(e) => setIntake({ ...intake, reason: e.target.value })} />
          </label>
          <button type="submit" disabled={busy}>Intake request</button>
        </form>
      )}
      {transfersState.data && transfersState.data.length === 0 && (
        <Empty label="No transfer requests." />
      )}
      <ul className="board-list">{transfersState.data?.map(renderTransfer)}</ul>

      <h3>Transport dispatch</h3>
      {transportState.data && transportState.data.length === 0 && (
        <Empty label="No transport requests." />
      )}
      <ul className="board-list">
        {transportState.data?.map((t) => (
          <li key={t.id} className="board-card">
            <div>
              <strong>{refText(t.patient_ref)}</strong>
              <StatusBadge
                tone={t.status === "completed" ? "ok" : t.status === "cancelled" ? "bad" : t.status === "requested" ? "neutral" : "info"}
              >
                {t.status}
              </StatusBadge>
              <span className="badge">{t.priority}</span>
            </div>
            <div className="hint">{t.from_location} → {t.to_location}</div>
            {canWork && t.status !== "completed" && t.status !== "cancelled" && (
              <div className="row-actions">
                {t.status === "requested" && (
                  <button type="button" disabled={busy}
                    onClick={() => perform("Transport assigned", () =>
                      client.transitionTransport(t.id, "assigned"))}>
                    Assign
                  </button>
                )}
                {t.status === "assigned" && (
                  <button type="button" disabled={busy}
                    onClick={() => perform("Transport in transit", () =>
                      client.transitionTransport(t.id, "in-transit"))}>
                    Start transport
                  </button>
                )}
                {t.status === "in-transit" && (
                  <button type="button" disabled={busy}
                    onClick={() => perform("Transport completed", () =>
                      client.transitionTransport(t.id, "completed"))}>
                    Complete
                  </button>
                )}
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );

  return (
    <section>
      <ModuleHeader
        icon="command"
        group="Operations"
        title="Capacity Command Center"
        purpose="House-wide capacity: census, boarding, bed requests, transfers, transport and the surge banner."
      />
      <p className="hint">
        Live roll-up of census, ED boarding, bed requests, transfers and
        transport. The end-of-day census is a deterministic projection
        (occupied + boarding + accepted transfers − expected discharges),
        labelled as such — not a forecast model. Diversion and surge
        changes are audited.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      <Tabs
        tabs={COMMAND_TABS}
        activeKey={activeTab}
        onChange={setActiveTab}
        ariaLabel="Command centre views"
        panels={{
          overview: overviewPanel,
          desks: <DesksPanel client={client} role={role} />,
          "access-line": <AccessLinePanel client={client} role={role} />,
          econsults: <EConsultsPanel client={client} role={role} />,
          episodes: <EpisodesPanel client={client} role={role} />,
          coordinator: <CoordinatorPanel client={client} role={role} />,
        }}
      />
    </section>
  );
}
