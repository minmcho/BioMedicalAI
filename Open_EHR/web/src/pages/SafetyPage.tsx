import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role, SafetyEventItem } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { InfectionPreventionPanel } from "./CareFlows";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const CATEGORIES = ["medication", "fall", "procedure", "equipment", "communication", "other"];
const HARM_LEVELS = ["near-miss", "none", "temporary", "permanent", "death"];

/** Harm + event-status tone maps (UX spec P-3). */
export function harmTone(harm?: string | null): StatusTone {
  if (harm === "death" || harm === "permanent") return "bad";
  if (harm === "temporary") return "warn";
  return "info";
}

export function safetyStatusTone(status?: string | null): StatusTone {
  if (status === "closed") return "ok";
  if (status === "in-review") return "warn";
  return "info";
}

/** Phase 12 safety & quality operations (Armstrong pattern): any
 *  staff role can file an event; the confidential flag shields the
 *  reporter's identity in every listing. Reviewers work the triage
 *  queue and the RCA workqueue, with a two-way follow-up thread on
 *  each event. The harm taxonomy and RCA triggers are published
 *  config, not code. */
export function SafetyPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canReview = permissions["safety.review"]?.[role] === true;
  const canReport = permissions["safety.report"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    description: "", category: "medication", harmLevel: "near-miss",
    unit: "", confidential: true,
  });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [message, setMessage] = useState("");

  const eventsState = useAsync(
    () => (canReview ? client.safetyEvents() : Promise.resolve([])),
    [client, tick, canReview],
  );
  const rcaState = useAsync(
    () => (canReview ? client.safetyRcaQueue() : Promise.resolve([])),
    [client, tick, canReview],
  );
  const summaryState = useAsync(
    () => (canReview ? client.safetySummary() : Promise.resolve(null)),
    [client, tick, canReview],
  );
  const detailState = useAsync(
    () => (selectedId ? client.safetyEvent(selectedId) : Promise.resolve(null)),
    [client, selectedId, tick],
  );

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

  function renderEvent(event: SafetyEventItem, inRca = false) {
    return (
      <li key={event.id} className="board-card">
        <div>
          <strong>{event.category}</strong>
          <StatusBadge tone={harmTone(event.harm_level)}>harm: {event.harm_level}</StatusBadge>
          <StatusBadge tone={safetyStatusTone(event.status)}>{event.status}</StatusBadge>
          {event.rca_required && (
            <StatusBadge tone={event.rca_status === "closed" ? "ok" : "warn"}>
              RCA {event.rca_status ?? ""}
            </StatusBadge>
          )}
          <span className="badge">reporter: {event.reporter}</span>
        </div>
        <div className="hint">
          {event.description}
          {event.unit ? ` · ${event.unit}` : ""}
        </div>
        <div className="row-actions">
          <button type="button" onClick={() => setSelectedId(event.id)}>
            Open follow-up
          </button>
          {canReview && event.status !== "closed" && (
            <>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  perform("Event triaged", () =>
                    client.triageSafetyEvent(event.id, { status: "in-review" }))}
              >
                Move to review
              </button>
              <button
                type="button"
                disabled={busy}
                onClick={() =>
                  perform("Event closed", () =>
                    client.triageSafetyEvent(event.id, { status: "closed" }))}
              >
                Close{inRca ? " (closes RCA)" : ""}
              </button>
            </>
          )}
        </div>
      </li>
    );
  }

  const summary = summaryState.data;
  const [tab, setTab] = useState("events");

  // The original safety-events view, unchanged, as the default tab;
  // Phase 15 infection prevention (app.infection_prevention) is the
  // second tab. Tabs mounts only the active panel, so the infection
  // panel's requests run only when its tab is opened.
  const eventsPanel = (
    <>
      {canReport && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!form.description.trim()) return;
            void perform("Safety event filed", async () => {
              await client.fileSafetyEvent({
                description: form.description.trim(),
                category: form.category,
                harmLevel: form.harmLevel,
                unit: form.unit.trim() || undefined,
                confidential: form.confidential,
              });
              setForm({ ...form, description: "" });
            });
          }}
        >
          <h3>Report a safety event</h3>
          <label>
            What happened{" "}
            <input
              aria-label="Event description"
              value={form.description}
              onChange={(e) => setForm({ ...form, description: e.target.value })}
            />
          </label>
          <label>
            Category{" "}
            <select aria-label="Event category" value={form.category}
              onChange={(e) => setForm({ ...form, category: e.target.value })}>
              {CATEGORIES.map((c) => <option key={c} value={c}>{c}</option>)}
            </select>
          </label>
          <label>
            Harm{" "}
            <select aria-label="Harm level" value={form.harmLevel}
              onChange={(e) => setForm({ ...form, harmLevel: e.target.value })}>
              {HARM_LEVELS.map((h) => <option key={h} value={h}>{h}</option>)}
            </select>
          </label>
          <label>
            Unit{" "}
            <input aria-label="Event unit" value={form.unit}
              onChange={(e) => setForm({ ...form, unit: e.target.value })}
              placeholder="unit-medsurg" />
          </label>
          <label>
            <input
              type="checkbox"
              aria-label="Confidential report"
              checked={form.confidential}
              onChange={(e) => setForm({ ...form, confidential: e.target.checked })}
            />{" "}
            Confidential (shield my identity)
          </label>
          <button type="submit" disabled={busy}>File event</button>
        </form>
      )}

      {!canReview && (
        <p className="hint">
          The triage queue is worked by the safety team (safety.review);
          your reports and their follow-ups appear in your event thread
          once a reviewer opens them.
        </p>
      )}

      {canReview && summary && (
        <div className="board-card" role="status">
          Events: <strong>{summary.total}</strong> · Open:{" "}
          <strong>{summary.open}</strong> · RCA open:{" "}
          <strong>{summary.rca_open}</strong>
        </div>
      )}

      {canReview && (
        <>
          <h3>Triage queue</h3>
          {eventsState.loading && <Loading label="Loading safety events…" />}
          {eventsState.error != null && <ErrorView error={eventsState.error} />}
          {eventsState.data && eventsState.data.length === 0 && (
            <Empty label="No safety events reported." />
          )}
          <ul className="board-list">{eventsState.data?.map((e) => renderEvent(e))}</ul>

          <h3>RCA workqueue ({rcaState.data?.length ?? 0})</h3>
          {rcaState.data && rcaState.data.length === 0 && (
            <Empty label="No open root-cause analyses." />
          )}
          <ul className="board-list">{rcaState.data?.map((e) => renderEvent(e, true))}</ul>
        </>
      )}

      {selectedId && detailState.data && (
        <div>
          <h3>Follow-up thread — {detailState.data.id}</h3>
          <ul className="board-list">
            {detailState.data.messages.map((m) => (
              <li key={m.id} className="board-card">
                <strong>{m.author}</strong>
                <span className="badge">{m.author_role}</span>
                <div>{m.body}</div>
              </li>
            ))}
          </ul>
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (!message.trim()) return;
              void perform("Follow-up posted", async () => {
                await client.safetyEventMessage(selectedId, message.trim());
                setMessage("");
              });
            }}
          >
            <label>
              Message{" "}
              <input aria-label="Follow-up message" value={message}
                onChange={(e) => setMessage(e.target.value)} />
            </label>
            <button type="submit" disabled={busy}>Post follow-up</button>
          </form>
        </div>
      )}
    </>
  );

  return (
    <section>
      <ModuleHeader
        icon="shield"
        group="Safety & Support"
        title="Safety reporting & triage"
        purpose="Report safety events confidentially; reviewers triage, run RCAs and follow up here."
      />
      <p className="hint">
        Reporting is open to every staff role and is non-punitive by
        design: file confidentially and your identity is shielded in
        listings and threads. Serious harm (per the published taxonomy)
        opens a root-cause analysis automatically.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      <Tabs
        tabs={[
          { key: "events", label: "Safety events" },
          { key: "infection", label: "Infection prevention" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Safety views"
        panels={{
          events: eventsPanel,
          infection: <InfectionPreventionPanel client={client} role={role} />,
        }}
      />
    </section>
  );
}
