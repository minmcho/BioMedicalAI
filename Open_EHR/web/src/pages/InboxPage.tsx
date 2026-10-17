import { useState } from "react";
import type { ApiClient } from "../api/client";
import { codeLabel, observationValue, resourceDate, resourceStatus } from "../api/display";
import type { CodeableConcept, FhirResource, MessageThreadDetail } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

/** Results inbox — GET /fhir/inbox?patient= (staff roles only on the
 *  server; a 403 renders as permission denied, not an empty inbox). */
export function InboxPage({ client }: Props) {
  const [patient, setPatient] = useState("pat-1");
  const [submitted, setSubmitted] = useState("pat-1");

  const { data, loading, error } = useAsync<FhirResource[]>(
    () => client.inbox(submitted),
    [client, submitted],
  );

  return (
    <section>
      <h2>Results inbox</h2>
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
        <button type="submit">Load results</button>
      </form>

      {loading ? (
        <Loading label="Loading results…" />
      ) : error ? (
        <ErrorView error={error} />
      ) : !data || data.length === 0 ? (
        <Empty label={`No results for patient ${submitted}.`} />
      ) : (
        <ul className="result-list">
          {data.map((r) => (
            <li key={`${r.resourceType}-${r.id}`} className="result-row">
              <span className="result-type">{r.resourceType}</span>
              <span>
                {codeLabel((r as { code?: CodeableConcept }).code)}
                {r.resourceType === "Observation" ? `: ${observationValue(r)}` : ""}
              </span>
              <span className="result-meta">
                {resourceStatus(r)} · {resourceDate(r)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <PatientMessages client={client} patient={submitted} />
    </section>
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
            <button type="submit">Send reply</button>
          </form>
        </div>
      )}
      <form className="inline-form" onSubmit={sendNew}>
        <label className="field">
          New message to patient
          <input value={draft} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <button type="submit">Send message</button>
      </form>
    </>
  );
}
