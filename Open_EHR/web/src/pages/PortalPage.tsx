import { useState } from "react";
import type { ApiClient } from "../api/client";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { codeLabel, observationValue, resourceDate, resourceStatus } from "../api/display";
import type {
  CodeableConcept,
  Condition,
  FhirResource,
  MedicationRequest,
  MessageThreadDetail,
  TelehealthWorkspace,
} from "../api/types";
import { FormRenderer } from "../components/FormRenderer";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface PortalData {
  results: FhirResource[];
  conditions: Condition[];
  medications: MedicationRequest[];
}

interface Props {
  client: ApiClient;
}

/** Patient portal — the signed-in patient's own data only. Results come
 *  from GET /portal/my-results; conditions/medications come from FHIR
 *  search, which the server force-scopes to the caller's own record. */
export function PortalPage({ client }: Props) {
  const { branding } = useRuntimeConfig();
  const { data, loading, error } = useAsync<PortalData>(async () => {
    const [results, conditions, medications] = await Promise.all([
      client.myResults(),
      client.search<Condition>("Condition"),
      client.search<MedicationRequest>("MedicationRequest"),
    ]);
    return { results, conditions, medications };
  }, [client]);

  if (loading) return <Loading label="Loading your results…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="Nothing to show yet." />;

  return (
    <section aria-label="Patient portal">
      <h2>{branding.portalTitle}</h2>
      {data.results.length === 0 ? (
        <Empty label="No results yet." />
      ) : (
        <ul className="result-list">
          {data.results.map((r) => (
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

      <h3>My conditions</h3>
      {data.conditions.length === 0 ? (
        <Empty label="No conditions recorded." />
      ) : (
        <ul>
          {data.conditions.map((c) => (
            <li key={c.id}>{codeLabel(c.code)}</li>
          ))}
        </ul>
      )}

      <h3>My medications</h3>
      {data.medications.length === 0 ? (
        <Empty label="No medication requests recorded." />
      ) : (
        <ul>
          {data.medications.map((m) => (
            <li key={m.id}>
              {m.medicationCodeableConcept ? codeLabel(m.medicationCodeableConcept) : "Medication"}
              {m.status ? ` — ${m.status}` : ""}
            </li>
          ))}
        </ul>
      )}

      <MessagesSection client={client} />
      <AppointmentsSection client={client} />
    </section>
  );
}

/** Secure messaging — the patient's own threads only (the server
 *  force-scopes every portal message call to the caller's record). */
function MessagesSection({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [openThread, setOpenThread] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [reply, setReply] = useState("");
  const threads = useAsync(() => client.myMessages(), [client, tick]);
  const detail = useAsync<MessageThreadDetail | null>(
    async () => (openThread ? client.messageThread(openThread) : null),
    [client, openThread, tick],
  );

  async function sendNew(e: React.FormEvent) {
    e.preventDefault();
    if (!draft.trim()) return;
    await client.sendPortalMessage(draft.trim());
    setDraft("");
    setTick((t) => t + 1);
  }

  async function sendReply(e: React.FormEvent) {
    e.preventDefault();
    if (!openThread || !reply.trim()) return;
    await client.replyToThread(openThread, reply.trim());
    await client.markThreadRead(openThread);
    setReply("");
    setTick((t) => t + 1);
  }

  return (
    <>
      <h3>Messages</h3>
      {threads.loading && <Loading label="Loading messages…" />}
      {threads.error != null && <ErrorView error={threads.error} />}
      {threads.data && threads.data.items.length === 0 && (
        <Empty label="No message threads yet." />
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
              <button type="button" onClick={() => setOpenThread(t.thread_id)}>
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
          New message to your care team
          <input value={draft} onChange={(e) => setDraft(e.target.value)} />
        </label>
        <button type="submit">Send message</button>
      </form>
    </>
  );
}

function appointmentTypeCode(a: FhirResource): string | null {
  const concept = (a as { appointmentType?: CodeableConcept }).appointmentType;
  return concept?.coding?.[0]?.code ?? null;
}

/** My appointments + self-scheduling requests + telehealth workspace. */
function AppointmentsSection({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [slotType, setSlotType] = useState("follow-up");
  const [start, setStart] = useState("");
  const [requestNote, setRequestNote] = useState<string | null>(null);
  const [workspaceFor, setWorkspaceFor] = useState<string | null>(null);
  const appointments = useAsync(() => client.myAppointments(), [client, tick]);
  const schedConfig = useAsync(() => client.selfSchedulingConfig(), [client]);
  const workspace = useAsync<TelehealthWorkspace | null>(
    async () => (workspaceFor ? client.telehealthWorkspace(workspaceFor) : null),
    [client, workspaceFor, tick],
  );

  async function request(e: React.FormEvent) {
    e.preventDefault();
    if (!start) return;
    const outcome = await client.requestAppointment({
      slot_type: slotType,
      start: new Date(start).toISOString(),
    });
    setRequestNote(
      outcome.auto_confirmed
        ? "Booked — your appointment is confirmed."
        : "Requested — the practice will confirm your appointment.",
    );
    setTick((t) => t + 1);
  }

  return (
    <>
      <h3>My appointments</h3>
      {appointments.loading && <Loading label="Loading appointments…" />}
      {appointments.error != null && <ErrorView error={appointments.error} />}
      {appointments.data && appointments.data.items.length === 0 && (
        <Empty label="No appointments yet." />
      )}
      {appointments.data && appointments.data.items.length > 0 && (
        <ul className="result-list">
          {appointments.data.items.map((a) => (
            <li key={a.id} className="result-row">
              <span className="result-type">
                {appointmentTypeCode(a) ?? "appointment"}
              </span>
              <span>{(a as { start?: string }).start ?? ""}</span>
              <span className="result-meta">{resourceStatus(a)}</span>
              {appointmentTypeCode(a) === "telehealth" && a.id && (
                <button type="button" onClick={() => setWorkspaceFor(a.id ?? null)}>
                  Visit workspace
                </button>
              )}
            </li>
          ))}
        </ul>
      )}

      {workspaceFor && workspace.data && (
        <div className="card" aria-label="Telehealth visit workspace">
          <h4>Telehealth visit</h4>
          <p>
            Provider: <strong>{workspace.data.provider_name ?? "—"}</strong>
            {workspace.data.join_url && (
              <>
                {" · "}
                <a href={workspace.data.join_url} target="_blank" rel="noreferrer">
                  Join visit
                </a>
              </>
            )}
          </p>
          {workspace.data.disclaimer && (
            <p className="hint" role="note">
              {workspace.data.disclaimer}
            </p>
          )}
          {workspace.data.intake_submitted ? (
            <p role="status">Pre-visit intake submitted.</p>
          ) : workspace.data.intake_form_definition ? (
            <>
              <h5>Pre-visit intake</h5>
              <FormRenderer
                form={workspace.data.intake_form_definition}
                submitLabel="Submit intake"
                onSubmit={async (answers) => {
                  await client.submitTelehealthIntake(workspaceFor, answers);
                  setTick((t) => t + 1);
                }}
              />
            </>
          ) : (
            <p className="hint">Intake form not published.</p>
          )}
        </div>
      )}

      <h4>Request an appointment</h4>
      <form className="inline-form" onSubmit={request}>
        <label className="field">
          Visit type
          <select value={slotType} onChange={(e) => setSlotType(e.target.value)}>
            {(schedConfig.data?.slot_types ?? ["follow-up"]).map((code) => (
              <option key={code} value={code}>
                {code}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          Preferred start
          <input
            type="datetime-local"
            value={start}
            onChange={(e) => setStart(e.target.value)}
          />
        </label>
        <button type="submit">Request appointment</button>
      </form>
      {requestNote && <p role="status">{requestNote}</p>}
      {schedConfig.data && (
        <p className="hint">
          {schedConfig.data.auto_confirm
            ? "This practice auto-confirms portal requests."
            : "Requests are confirmed by the care team."}
        </p>
      )}
    </>
  );
}
