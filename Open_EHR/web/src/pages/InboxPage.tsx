import { useState } from "react";
import type { ApiClient } from "../api/client";
import { codeLabel, observationValue } from "../api/display";
import {
  acknowledgeInboxResult,
  createInboxFollowUpTask,
  escalateInboxResult,
  getInboxResultDetail,
  getInboxSummary,
  listInboxResults,
  messageInboxPatient,
  type InboxResultDetail,
  type InboxResultItem,
  type InboxScope,
} from "../api/inbox";
import type { FhirResource, MessageThreadDetail } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: string;
  onNavigate?: (page: string) => void;
  onOpenChart?: (patientId: string) => void;
}

/** Results inbox (Phase 15, scope G) — the cross-patient clinician
 *  worklist backed by backend/app/results_inbox.py (/inbox), plus the
 *  Phase 9 secure-message threads preserved under the Messages tab.
 *  NOTE: no shared MasterDetail component exists in
 *  web/src/components yet, so the two-pane master-detail layout is
 *  built inline here (list pane + detail pane). */
export function InboxPage({ client, onNavigate, onOpenChart }: Props) {
  const [tab, setTab] = useState("results");
  return (
    <section>
      <ModuleHeader
        icon="inbox"
        group="Patient Care"
        title="Results inbox"
        purpose="Acknowledge results, create follow-ups and message patients about a result; secure message threads live on the Messages tab."
      />
      <Tabs
        ariaLabel="Inbox"
        tabs={[
          { key: "results", label: "Results" },
          { key: "messages", label: "Messages" },
        ]}
        activeKey={tab}
        onChange={setTab}
        panels={{
          results: (
            <ResultsPanel client={client} onNavigate={onNavigate} onOpenChart={onOpenChart} />
          ),
          messages: <MessagesPanel client={client} />,
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Results tab
// ---------------------------------------------------------------------------

function flagBadge(flag: InboxResultItem["flag"]) {
  if (flag === "critical") return <StatusBadge tone="bad">Critical</StatusBadge>;
  if (flag === "abnormal") return <StatusBadge tone="warn">Abnormal</StatusBadge>;
  return null;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function patientIdOf(ref: string | null | undefined): string | null {
  if (!ref) return null;
  return ref.startsWith("Patient/") ? ref.slice("Patient/".length) : ref;
}

function ResultsPanel({
  client,
  onNavigate,
  onOpenChart,
}: {
  client: ApiClient;
  onNavigate?: (page: string) => void;
  onOpenChart?: (patientId: string) => void;
}) {
  const [scope, setScope] = useState<InboxScope>("all");
  const [kind, setKind] = useState<"" | "lab" | "imaging" | "pathology">("");
  const [flag, setFlag] = useState<"" | "critical" | "abnormal" | "none">("");
  const [ack, setAck] = useState<"" | "unacknowledged" | "acknowledged">("");
  const [selectedKey, setSelectedKey] = useState<string | null>(null);

  const summary = useAsync(() => getInboxSummary(client), [client]);
  const list = useAsync(
    () =>
      listInboxResults(client, {
        scope,
        kind: kind || undefined,
        flag: flag || undefined,
        ack: ack || undefined,
      }),
    [client, scope, kind, flag, ack],
  );

  const items = list.data?.items ?? [];
  const selected =
    items.find((i) => `${i.kind}/${i.result_id}` === selectedKey) ?? null;

  return (
    <>
      {summary.loading && <Loading label="Loading inbox summary…" />}
      {summary.error != null && <ErrorView error={summary.error} />}
      {summary.data && (
        <div className="card">
          <h3>Inbox summary</h3>
          <p>
            {`${summary.data.results.unacknowledged} unacknowledged results · ${summary.data.results.critical_unacknowledged} critical unacknowledged`}
          </p>
          <p>
            {`Care continuum: ${summary.data.continuum.open_tasks} open tasks · ${summary.data.continuum.breached} breached`}
          </p>
          {onNavigate && (
            <button
              type="button"
              onClick={() => onNavigate(summary.data?.continuum.module || "command")}
            >
              Open Command Center inbox
            </button>
          )}
        </div>
      )}

      <form className="inline-form" onSubmit={(e) => e.preventDefault()}>
        <label className="field">
          Scope
          <select aria-label="Scope" value={scope} onChange={(e) => setScope(e.target.value as InboxScope)}>
            <option value="mine">Mine</option>
            <option value="unit">My unit</option>
            <option value="all">All</option>
          </select>
        </label>
        <label className="field">
          Kind
          <select aria-label="Kind" value={kind} onChange={(e) => setKind(e.target.value as typeof kind)}>
            <option value="">All kinds</option>
            <option value="lab">Lab</option>
            <option value="imaging">Imaging</option>
            <option value="pathology">Pathology</option>
          </select>
        </label>
        <label className="field">
          Flag
          <select aria-label="Flag" value={flag} onChange={(e) => setFlag(e.target.value as typeof flag)}>
            <option value="">All flags</option>
            <option value="critical">Critical</option>
            <option value="abnormal">Abnormal</option>
            <option value="none">No flag</option>
          </select>
        </label>
        <label className="field">
          Acknowledgement
          <select aria-label="Acknowledgement" value={ack} onChange={(e) => setAck(e.target.value as typeof ack)}>
            <option value="">All</option>
            <option value="unacknowledged">Unacknowledged</option>
            <option value="acknowledged">Acknowledged</option>
          </select>
        </label>
      </form>

      {list.loading ? (
        <Loading label="Loading results…" />
      ) : list.error ? (
        <ErrorView error={list.error} />
      ) : items.length === 0 ? (
        <Empty label="No results match these filters." />
      ) : (
        <div className="inbox-master-detail" style={{ display: "flex", gap: "1rem", alignItems: "flex-start" }}>
          <ul className="result-list" aria-label="Results worklist" style={{ flex: "1 1 45%" }}>
            {items.map((item) => {
              const key = `${item.kind}/${item.result_id}`;
              const chartPatientId = patientIdOf(item.patient_ref);
              return (
                <li
                  key={key}
                  className="result-row"
                  aria-current={selectedKey === key || undefined}
                >
                  <button type="button" onClick={() => setSelectedKey(key)}>
                    {item.title}
                  </button>
                  {onOpenChart && chartPatientId && (
                    <button
                      type="button"
                      className="button-secondary"
                      aria-label={`Open chart for result ${item.title}`}
                      onClick={() => onOpenChart(chartPatientId)}
                    >
                      Open chart
                    </button>
                  )}
                  {flagBadge(item.flag)}
                  <span>{item.kind}</span>
                  <span className="result-meta">
                    {item.patient_ref ?? "—"} · {formatTime(item.effective_at)}
                  </span>
                  <span>
                    {item.acknowledged
                      ? `Acknowledged${item.acknowledged_by ? ` by ${item.acknowledged_by}` : ""}`
                      : "Unacknowledged"}
                  </span>
                </li>
              );
            })}
          </ul>
          <div style={{ flex: "1 1 55%" }}>
            {selected ? (
              <ResultDetailPane
                key={`${selected.kind}/${selected.result_id}`}
                client={client}
                item={selected}
                onOpenChart={onOpenChart}
                onChanged={() => {
                  list.reload();
                  summary.reload();
                }}
              />
            ) : (
              <Empty label="Select a result to see its details." />
            )}
          </div>
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Detail pane
// ---------------------------------------------------------------------------

type ObsLike = FhirResource & {
  code?: { text?: string; coding?: Array<{ code?: string; display?: string }> };
  interpretation?: Array<{ coding?: Array<{ code?: string; display?: string }>; text?: string }>;
  referenceRange?: Array<{
    text?: string;
    low?: { value?: number; unit?: string };
    high?: { value?: number; unit?: string };
  }>;
};

function interpretationLabel(obs: ObsLike): string | null {
  const interp = obs.interpretation?.[0];
  if (!interp) return null;
  return interp.text ?? interp.coding?.[0]?.display ?? interp.coding?.[0]?.code ?? null;
}

function referenceRangeLabel(obs: ObsLike): string | null {
  const range = obs.referenceRange?.[0];
  if (!range) return null;
  if (range.text) return range.text;
  const unit = range.low?.unit ?? range.high?.unit ?? "";
  const low = range.low?.value;
  const high = range.high?.value;
  if (low !== undefined && high !== undefined) return `${low}–${high}${unit ? ` ${unit}` : ""}`;
  if (low !== undefined) return `≥ ${low}${unit ? ` ${unit}` : ""}`;
  if (high !== undefined) return `≤ ${high}${unit ? ` ${unit}` : ""}`;
  return null;
}

function interpTone(code: string | null): StatusTone {
  if (code === "LL" || code === "HH") return "bad";
  if (code === "L" || code === "H" || code === "A" || code === "AA") return "warn";
  return "neutral";
}

function ResultDetailPane({
  client,
  item,
  onOpenChart,
  onChanged,
}: {
  client: ApiClient;
  item: InboxResultItem;
  onOpenChart?: (patientId: string) => void;
  onChanged: () => void;
}) {
  const detail = useAsync<InboxResultDetail>(
    () => getInboxResultDetail(client, item.kind, item.result_id),
    [client, item.kind, item.result_id],
  );
  const [outcome, setOutcome] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [taskDescription, setTaskDescription] = useState("");
  const [messageText, setMessageText] = useState("");
  const [escalationReason, setEscalationReason] = useState("");

  async function run(action: () => Promise<string>) {
    setBusy(true);
    setActionError(null);
    try {
      setOutcome(await action());
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  if (detail.loading) return <Loading label="Loading result detail…" />;
  if (detail.error) return <ErrorView error={detail.error} />;
  if (!detail.data) return <Empty label="No detail available for this result." />;
  const d = detail.data;
  const patientId = patientIdOf(d.patient_ref);

  return (
    <div className="card">
      <h3>{d.title}</h3>
      <p>
        {flagBadge(d.flag)}{" "}
        <StatusBadge tone="info">{d.kind}</StatusBadge>{" "}
        <span>
          {d.acknowledged
            ? `Acknowledged${d.acknowledged_by ? ` by ${d.acknowledged_by}` : ""}${
                d.acknowledged_at ? ` at ${formatTime(d.acknowledged_at)}` : ""
              }`
            : "Unacknowledged"}
        </span>
      </p>
      <p className="result-meta">
        Status {d.status ?? "—"} · {formatTime(d.effective_at)} · {d.result_ref}
      </p>

      {d.patient_summary && (
        <p>
          Patient:{" "}
          {d.patient_summary.found
            ? `${d.patient_summary.name ?? d.patient_ref ?? "—"}${
                d.patient_summary.birth_date ? ` · born ${d.patient_summary.birth_date}` : ""
              }${d.patient_summary.gender ? ` · ${d.patient_summary.gender}` : ""}`
            : (d.patient_ref ?? "—")}
        </p>
      )}
      {!d.patient_summary && <p>Patient: {d.patient_ref ?? "—"}</p>}
      {d.encounter_context && (
        <p>
          Encounter: {d.encounter_context.encounter_ref}
          {d.encounter_context.found
            ? ` · ${d.encounter_context.status ?? "—"}${
                d.encounter_context.class_code ? ` · ${d.encounter_context.class_code}` : ""
              }${
                d.encounter_context.attending_staff_id
                  ? ` · attending ${d.encounter_context.attending_staff_id}`
                  : ""
              }`
            : " (not found)"}
        </p>
      )}
      {patientId && onOpenChart && (
        <p>
          <button type="button" onClick={() => onOpenChart(patientId)}>
            Open chart
          </button>
        </p>
      )}

      <h4>Observations</h4>
      {d.observations.length === 0 ? (
        <Empty label="No observations on this result." />
      ) : (
        <ul className="result-list">
          {d.observations.map((raw, idx) => {
            const obs = raw as ObsLike;
            const interp = interpretationLabel(obs);
            const range = referenceRangeLabel(obs);
            return (
              <li key={obs.id ?? idx} className="result-row">
                <span>{codeLabel(obs.code)}</span>
                <span>{observationValue(raw)}</span>
                {interp && (
                  <StatusBadge tone={interpTone(obs.interpretation?.[0]?.coding?.[0]?.code ?? null)}>
                    {interp}
                  </StatusBadge>
                )}
                {range && <span className="result-meta">Reference range: {range}</span>}
              </li>
            );
          })}
        </ul>
      )}

      {d.conclusion && (
        <>
          <h4>Conclusion</h4>
          <p>{d.conclusion}</p>
        </>
      )}

      <h4>Trend</h4>
      {d.trend.length === 0 ? (
        <Empty label="No prior results of the same code for this patient." />
      ) : (
        <ul className="result-list">
          {d.trend.map((t) => (
            <li key={`${t.observation_id}-${t.effective_at}`} className="result-row">
              <span>{t.display ?? t.code ?? "Observation"}</span>
              <span>
                {t.value === null || t.value === undefined ? "—" : String(t.value)}
                {t.unit ? ` ${t.unit}` : ""}
              </span>
              {t.interpretation && (
                <StatusBadge tone={interpTone(t.interpretation)}>{t.interpretation}</StatusBadge>
              )}
              <span className="result-meta">{formatTime(t.effective_at)}</span>
            </li>
          ))}
        </ul>
      )}

      <h4>Actions</h4>
      {outcome && (
        <p className="state state-ok" role="status">
          {outcome}
        </p>
      )}
      {actionError && (
        <p className="state state-error" role="alert">
          {actionError}
        </p>
      )}
      <p>
        <button
          type="button"
          disabled={busy || d.acknowledged}
          onClick={() =>
            run(async () => {
              const res = await acknowledgeInboxResult(client, d.kind, d.result_id);
              detail.reload();
              onChanged();
              return res.already_acknowledged
                ? "Already acknowledged — no duplicate recorded."
                : `Acknowledged by ${res.acknowledged_by}.`;
            })
          }
        >
          Acknowledge
        </button>
      </p>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!taskDescription.trim()) return;
          void run(async () => {
            const res = await createInboxFollowUpTask(client, d.kind, d.result_id, {
              description: taskDescription.trim(),
            });
            setTaskDescription("");
            return `Follow-up task created${res.task.id ? ` (${res.task.id})` : ""}.`;
          });
        }}
      >
        <label className="field">
          Follow-up task description
          <input
            value={taskDescription}
            onChange={(e) => setTaskDescription(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy || !taskDescription.trim()}>
          Create follow-up task
        </button>
      </form>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!messageText.trim()) return;
          void run(async () => {
            const res = await messageInboxPatient(client, d.kind, d.result_id, {
              text: messageText.trim(),
            });
            setMessageText("");
            return `Message sent to patient${res.thread_id ? ` (thread ${res.thread_id})` : ""}.`;
          });
        }}
      >
        <label className="field">
          Message to patient about this result
          <input value={messageText} onChange={(e) => setMessageText(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !messageText.trim()}>
          Message patient
        </button>
      </form>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          void run(async () => {
            const res = await escalateInboxResult(client, d.kind, d.result_id, {
              reason: escalationReason.trim() || undefined,
            });
            setEscalationReason("");
            return `Escalated${res.audience ? ` to ${res.audience}` : ""}.`;
          });
        }}
      >
        <label className="field">
          Escalation reason
          <input
            value={escalationReason}
            onChange={(e) => setEscalationReason(e.target.value)}
          />
        </label>
        <button type="submit" disabled={busy}>
          Escalate
        </button>
      </form>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Messages tab — Phase 9 secure-message threads, behaviour preserved
// from the previous InboxPage (per-patient threads, mark-read on open,
// reply in thread, start a new thread).
// ---------------------------------------------------------------------------

function MessagesPanel({ client }: { client: ApiClient }) {
  const [patient, setPatient] = useState("pat-1");
  const [submitted, setSubmitted] = useState("pat-1");
  return (
    <>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(patient.trim());
        }}
      >
        <label className="field">
          Patient id
          <input value={patient} onChange={(e) => setPatient(e.target.value)} placeholder="pat-1" />
        </label>
        <button type="submit">Load messages</button>
      </form>
      <PatientMessages client={client} patient={submitted} />
    </>
  );
}

/** Secure-message threads for the selected patient (Phase 9) — the
 *  same FHIR Communication threads the patient sees in the portal. */
function PatientMessages({ client, patient }: { client: ApiClient; patient: string }) {
  const [tick, setTick] = useState(0);
  const [openThread, setOpenThread] = useState<string | null>(null);
  const [reply, setReply] = useState("");
  const [draft, setDraft] = useState("");
  const threads = useAsync(() => client.messageInbox(patient), [client, patient, tick]);
  const detail = useAsync<MessageThreadDetail | null>(
    async () => (openThread ? client.messageThread(openThread) : null),
    [client, openThread, tick],
  );

  async function open(threadId: string) {
    setOpenThread(threadId);
    await client.markThreadRead(threadId);
    setTick((t) => t + 1);
  }

  async function sendReply(e: React.FormEvent) {
    e.preventDefault();
    if (!openThread || !reply.trim()) return;
    await client.replyToThread(openThread, reply.trim());
    setReply("");
    setTick((t) => t + 1);
  }

  async function sendNew(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim()) return;
    await client.sendMessage(patient, draft.trim());
    setDraft("");
    setTick((t) => t + 1);
  }

  return (
    <>
      <h3>Messages</h3>
      {threads.loading && <Loading label="Loading messages…" />}
      {threads.error != null && <ErrorView error={threads.error} />}
      {threads.data && threads.data.items.length === 0 && (
        <Empty label={`No message threads for patient ${patient}.`} />
      )}
      {threads.data && threads.data.items.length > 0 && (
        <ul className="result-list">
          {threads.data.items.map((t) => (
            <li key={t.thread_id} className="result-row">
              <span>
                {t.topic ?? "Message thread"}
                {t.unread_count > 0 && (
                  <span className="badge badge-warn"> {t.unread_count} unread</span>
                )}
              </span>
              <span className="result-meta">{t.last_text ?? ""}</span>
              <button type="button" onClick={() => open(t.thread_id)}>
                Open
              </button>
            </li>
          ))}
        </ul>
      )}
      {openThread && detail.data && (
        <div className="card">
          <ul className="result-list">
            {detail.data.messages.map((m) => (
              <li key={m.id} className="result-row">
                <span className="result-type">{m.senderRole}</span>
                <span>{m.text}</span>
                <span className="result-meta">{m.sent ?? ""}</span>
              </li>
            ))}
          </ul>
          <form className="inline-form" onSubmit={sendReply}>
            <label className="field">
              Reply
              <input value={reply} onChange={(e) => setReply(e.target.value)} />
            </label>
            <button type="submit" disabled={!reply.trim()}>Send reply</button>
          </form>
        </div>
      )}
      <form className="inline-form" onSubmit={sendNew}>
        <label className="field">
          New message to patient
          <input value={draft} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <button type="submit" disabled={!draft.trim()}>Send message</button>
      </form>
    </>
  );
}
