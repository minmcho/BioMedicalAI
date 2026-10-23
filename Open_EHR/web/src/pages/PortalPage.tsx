import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  PAYMENT_TEST_MODE_LABEL,
  PROXY_SCOPES,
  createProxyGrant,
  listMyProxyGrants,
  listMyTelehealthVisits,
  listProxyPatients,
  portalBillingBalance,
  portalBillingPay,
  portalTelehealthVisit,
  proxyAppointments,
  proxyMessages,
  proxyResults,
  revokeProxyGrant,
  submitTelehealthOutcome,
  telehealthOutcomeForm,
  type PortalPaymentReceipt,
  type ProxyGrant,
  type TelehealthVisit,
} from "../api/accessServices";
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
import { ModuleHeader } from "../components/ModuleHeader";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
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
  const [tab, setTab] = useState("results");
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
      <ModuleHeader
        icon="heart"
        group="Patient Care"
        title={branding.portalTitle}
        purpose="Your own results, conditions and medications, messages with your care team, appointments and virtual visits, bills and proxy access."
        primaryAction={{
          label: "Request an appointment",
          onClick: () => setTab("visits"),
        }}
      />
      <Tabs
        ariaLabel="Patient portal"
        tabs={[
          { key: "results", label: "My results" },
          { key: "messages", label: "Messages" },
          { key: "visits", label: "Appointments & visits" },
          { key: "billing", label: "Pay a bill" },
          { key: "proxies", label: "Proxies" },
        ]}
        activeKey={tab}
        onChange={setTab}
        panels={{
          results: (
            <>
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

            </>
          ),
          messages: <MessagesSection client={client} />,
          visits: (
            <>
              <AppointmentsSection client={client} />
              <TelehealthSection client={client} />
            </>
          ),
          billing: <BillPaySection client={client} />,
          proxies: <ProxySection client={client} />,
        }}
      />
    </section>
  );
}

/** Phase 15 (BD-28) — My virtual visits. Lists the patient's own
 *  Phase 15 telehealth visits (GET /portal/telehealth/visits); the
 *  join link comes from the visit workspace, and a COMPLETED visit
 *  without an outcome offers the governed outcome form
 *  (form/telehealth-outcome) via the shared FormRenderer, submitted
 *  to POST /portal/telehealth/visits/{id}/outcome (the server stores
 *  a real QuestionnaireResponse; a second outcome is a 409). */
function TelehealthSection({ client }: Props) {
  const [tick, setTick] = useState(0);
  // Loader guard (the LabPage sections' established pattern): stub
  // clients without `request` render the empty state, never a throw.
  const visits = useAsync(
    () => (typeof client.request === "function"
      ? listMyTelehealthVisits(client)
      : Promise.resolve({ items: [] as TelehealthVisit[], total: 0 })),
    [client, tick]);
  const [openFormFor, setOpenFormFor] = useState<string | null>(null);
  const [form, setForm] = useState<import("../api/types").FormDefinition | null>(null);
  const [joinUrls, setJoinUrls] = useState<Record<string, string>>({});
  const [note, setNote] = useState<string | null>(null);
  const [errorText, setErrorText] = useState<string | null>(null);

  async function openOutcome(visitId: string) {
    setErrorText(null);
    const definition = await telehealthOutcomeForm(client);
    if (!definition) {
      setErrorText("The outcome form could not be loaded.");
      return;
    }
    setForm(definition);
    setOpenFormFor(visitId);
  }

  async function loadJoin(visit: TelehealthVisit) {
    setErrorText(null);
    try {
      const ws = await portalTelehealthVisit(client, visit.visit_id);
      if (ws.join_url) {
        setJoinUrls((m) => ({ ...m, [visit.visit_id]: ws.join_url as string }));
      } else {
        setErrorText("No join link is configured for this visit yet.");
      }
    } catch (err) {
      setErrorText(portalErrorText(err));
    }
  }

  return (
    <>
      <h3>My virtual visits</h3>
      {visits.loading && <Loading label="Loading your virtual visits…" />}
      {visits.error != null && <ErrorView error={visits.error} />}
      {visits.data && visits.data.items.length === 0 && (
        <Empty label="No virtual visits yet." />
      )}
      {visits.data && visits.data.items.length > 0 && (
        <ul className="result-list">
          {visits.data.items.map((v) => (
            <li key={v.visit_id} className="result-row">
              <span className="result-type">{v.service_line || "telehealth"}</span>
              <span>Virtual visit {v.visit_id}</span>
              <span className="result-meta">
                {v.status}
                {v.outcome_submitted ? " · outcome submitted" : ""}
              </span>
              {v.status !== "completed" && v.status !== "virtual-no-show" && (
                joinUrls[v.visit_id] ? (
                  <a href={joinUrls[v.visit_id]} target="_blank" rel="noreferrer">
                    Join visit
                  </a>
                ) : (
                  <button type="button" onClick={() => void loadJoin(v)}>
                    Get join link
                  </button>
                )
              )}
              {v.status === "completed" && !v.outcome_submitted && (
                <button type="button" onClick={() => void openOutcome(v.visit_id)}>
                  Submit outcome
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      {note && <p className="hint">{note}</p>}
      {errorText && (
        <div className="state state-error" role="alert">{errorText}</div>
      )}
      {openFormFor && form && (
        <FormRenderer
          form={form}
          submitLabel="Submit outcome"
          onSubmit={async (answers) => {
            try {
              await submitTelehealthOutcome(client, openFormFor, answers);
              setNote("Outcome submitted — thank you.");
              setOpenFormFor(null);
              setForm(null);
              setTick((t) => t + 1);
            } catch (err) {
              setErrorText(portalErrorText(err));
            }
          }}
        />
      )}
    </>
  );
}

function portalErrorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function money(value?: number): string {
  return typeof value === "number" ? `USD ${value.toFixed(2)}` : "—";
}

/** Phase 15 (BD-22) — Pay a bill. The balance is the PATIENT token's
 *  own statement (GET /portal/billing/balance, computed by
 *  revenue.patient_statement server-side) and payments post through
 *  the governed payment provider, which is TEST MODE
 *  (integration/payment-provider {mode:"test", live:false}): the
 *  test-mode label and the server disclaimer are shown verbatim
 *  wherever payment is offered and on the receipt. */
function BillPaySection({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [amount, setAmount] = useState("");
  const [claimId, setClaimId] = useState("");
  const [method, setMethod] = useState("card");
  const [receipt, setReceipt] = useState<PortalPaymentReceipt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const balanceState = useAsync(
    async () =>
      typeof client.request === "function" ? portalBillingBalance(client) : null,
    [client, tick],
  );
  const balance = balanceState.data;
  const payableClaims = (balance?.claims ?? []).filter(
    (c) => (c.patient_due ?? 0) > 0,
  );
  const effectiveClaimId =
    claimId || (payableClaims.length === 1 ? payableClaims[0].claim_id : "");

  async function pay(e: React.FormEvent) {
    e.preventDefault();
    const value = Number(amount);
    if (!Number.isFinite(value) || value <= 0) {
      setError("Enter an amount greater than zero to pay.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await portalBillingPay(client, {
        amount: value,
        ...(effectiveClaimId ? { claim_id: effectiveClaimId } : {}),
        method,
      });
      setReceipt(result);
      setAmount("");
      setTick((t) => t + 1);
    } catch (err) {
      setError(`Payment failed: ${portalErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <h3>Pay a bill</h3>
      <p className="hint">
        <strong>{PAYMENT_TEST_MODE_LABEL}</strong> — the payment provider is
        in test mode (provider: test-mode, live: false). Paying here records
        the payment in your statement ledger only.
      </p>
      {balanceState.loading && <Loading label="Loading your balance…" />}
      {balanceState.error != null && <ErrorView error={balanceState.error} />}
      {balance && (
        <>
          <p>
            Balance due: <strong>{money(balance.patient_due)}</strong>
            {typeof balance.patient_paid === "number"
              ? ` · paid so far ${money(balance.patient_paid)}`
              : ""}
            {` · provider mode: ${balance.provider_mode} (live: false)`}
          </p>
          {balance.disclaimer && <p className="hint">{balance.disclaimer}</p>}
          {payableClaims.length > 0 ? (
            <form className="inline-form" onSubmit={pay}>
              <label className="field">
                Claim
                <select
                  aria-label="Claim to pay"
                  value={effectiveClaimId}
                  onChange={(e) => setClaimId(e.target.value)}
                >
                  {payableClaims.length > 1 && <option value="">First claim with a balance</option>}
                  {payableClaims.map((c) => (
                    <option key={c.claim_id} value={c.claim_id}>
                      {c.claim_id} — {money(c.patient_due)} due
                    </option>
                  ))}
                </select>
              </label>
              <label className="field">
                Amount (USD)
                <input
                  aria-label="Payment amount"
                  type="number"
                  step="0.01"
                  min="0"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                />
              </label>
              <label className="field">
                Method
                <select
                  aria-label="Payment method"
                  value={method}
                  onChange={(e) => setMethod(e.target.value)}
                >
                  {["card", "ach", "cash", "check"].map((m) => (
                    <option key={m} value={m}>{m}</option>
                  ))}
                </select>
              </label>
              <button type="submit" disabled={busy || !amount.trim()}>
                Pay bill (test mode)
              </button>
            </form>
          ) : (
            <Empty label="No balance due — nothing to pay." />
          )}
        </>
      )}
      {error && <div className="state state-error" role="alert">{error}</div>}
      {receipt && (
        <div className="card" aria-label="Payment receipt">
          <h4>Payment receipt</h4>
          <p>
            Paid <strong>{money(receipt.amount)}</strong> on claim{" "}
            {receipt.claim_id} · provider: {receipt.provider} · provider
            mode: {receipt.provider_mode} · live: {String(receipt.live)} ·
            balance after: <strong>{money(receipt.balance_after)}</strong>
          </p>
          <p className="hint">{receipt.disclaimer}</p>
        </div>
      )}
    </>
  );
}

const PROXY_SCOPE_LABELS: Record<string, string> = {
  schedule: "View appointments",
  messages: "View message threads",
  results: "View released results",
};

type ProxyScopeView = (typeof PROXY_SCOPES)[number];

/** Phase 15 (BD-23) — portal proxy access. "My proxies" lists the
 *  grants this patient has given (create + revoke ride
 *  /portal/proxy/grants); "I am a proxy for" lists the patients whose
 *  active grants name this account, with scoped views of their
 *  appointments / messages / results limited to the granted scopes
 *  (terminology/proxy-scopes). Every proxy read is audited
 *  server-side on-behalf-of the patient. */
function ProxySection({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [proxyUsername, setProxyUsername] = useState("");
  const [scopes, setScopes] = useState<string[]>(["schedule"]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [selected, setSelected] = useState<ProxyGrant | null>(null);
  const [scopeView, setScopeView] = useState<ProxyScopeView>("schedule");

  const hasRequest = typeof client.request === "function";
  const grantsState = useAsync(
    async () => (hasRequest ? listMyProxyGrants(client) : null),
    [client, tick],
  );
  const patientsState = useAsync(
    async () => (hasRequest ? listProxyPatients(client) : null),
    [client, tick],
  );
  const scopedState = useAsync(
    async () => {
      if (!hasRequest || !selected) return null;
      const patientRef = selected.patient_ref;
      if (scopeView === "schedule")
        return { kind: "schedule" as const, data: await proxyAppointments(client, patientRef) };
      if (scopeView === "messages")
        return { kind: "messages" as const, data: await proxyMessages(client, patientRef) };
      return { kind: "results" as const, data: await proxyResults(client, patientRef) };
    },
    [client, selected, scopeView, tick],
  );

  function toggleScope(code: string, checked: boolean) {
    setScopes((prev) =>
      checked ? [...prev, code] : prev.filter((s) => s !== code),
    );
  }

  async function createGrant(e: React.FormEvent) {
    e.preventDefault();
    if (!proxyUsername.trim() || scopes.length === 0) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const grant = await createProxyGrant(client, {
        proxy_username: proxyUsername.trim(),
        scopes,
      });
      setNotice(`Proxy access granted to ${grant.proxy_username}.`);
      setProxyUsername("");
      setTick((t) => t + 1);
    } catch (err) {
      setError(`Grant failed: ${portalErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function revoke(grant: ProxyGrant) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await revokeProxyGrant(client, grant.grant_id);
      setNotice(`Proxy access for ${grant.proxy_username} revoked.`);
      setTick((t) => t + 1);
    } catch (err) {
      setError(`Revoke failed: ${portalErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const myGrants = grantsState.data?.items ?? [];
  const proxyFor = patientsState.data?.items ?? [];
  const scoped = scopedState.data;

  return (
    <>
      <h3>My proxies</h3>
      <p className="hint">
        People you have granted proxy access to your record. Scopes come
        from the published <code>terminology/proxy-scopes</code> config;
        a proxy only ever sees the scopes you grant, and every proxy read
        is audited on your behalf.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      {grantsState.loading && <Loading label="Loading your proxy grants…" />}
      {grantsState.error != null && <ErrorView error={grantsState.error} />}
      {grantsState.data && myGrants.length === 0 && (
        <Empty label="You have not granted proxy access to anyone." />
      )}
      {myGrants.length > 0 && (
        <ul className="result-list">
          {myGrants.map((g) => (
            <li key={g.grant_id} className="result-row">
              <span>{g.proxy_username}</span>
              <span className="result-meta">
                scopes: {g.scopes.join(", ")} · {g.effective_status}
                {g.granted_by ? ` · granted by ${g.granted_by}` : ""}
              </span>
              {g.effective_status === "active" && (
                <button type="button" disabled={busy} onClick={() => void revoke(g)}>
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <form className="inline-form" onSubmit={createGrant}>
        <label className="field">
          Proxy username
          <input
            aria-label="Proxy username"
            value={proxyUsername}
            onChange={(e) => setProxyUsername(e.target.value)}
          />
        </label>
        <fieldset>
          <legend>Scopes</legend>
          {PROXY_SCOPES.map((code) => (
            <label key={code}>
              <input
                type="checkbox"
                aria-label={`Proxy scope ${code}`}
                checked={scopes.includes(code)}
                onChange={(e) => toggleScope(code, e.target.checked)}
              />{" "}
              {PROXY_SCOPE_LABELS[code] ?? code}
            </label>
          ))}
        </fieldset>
        <button type="submit" disabled={busy || !proxyUsername.trim() || scopes.length === 0}>
          Grant proxy access
        </button>
      </form>

      <h3>I am a proxy for</h3>
      {patientsState.loading && <Loading label="Loading patients you proxy for…" />}
      {patientsState.error != null && <ErrorView error={patientsState.error} />}
      {patientsState.data && proxyFor.length === 0 && (
        <Empty label="You are not a proxy for any patient." />
      )}
      {proxyFor.length > 0 && (
        <ul className="result-list">
          {proxyFor.map((g) => (
            <li key={g.grant_id} className="result-row">
              <span>{g.patient_ref}</span>
              <span className="result-meta">scopes: {g.scopes.join(", ")}</span>
              <button
                type="button"
                onClick={() => {
                  setSelected(g);
                  setScopeView(
                    (PROXY_SCOPES.find((s) => g.scopes.includes(s)) ?? "schedule") as ProxyScopeView,
                  );
                }}
              >
                View as proxy
              </button>
            </li>
          ))}
        </ul>
      )}
      {selected && (
        <div className="card" aria-label="Proxy view">
          <h4>Proxy view — {selected.patient_ref}</h4>
          <p className="hint">
            Acting on behalf of {selected.patient_ref} as{" "}
            {patientsState.data?.proxy ?? "proxy"}; reads are limited to the
            granted scopes ({selected.scopes.join(", ")}) and are audited.
          </p>
          <div className="toolbar">
            {PROXY_SCOPES.filter((s) => selected.scopes.includes(s)).map((s) => (
              <button
                key={s}
                type="button"
                aria-pressed={scopeView === s}
                onClick={() => setScopeView(s)}
              >
                {PROXY_SCOPE_LABELS[s] ?? s}
              </button>
            ))}
          </div>
          {scopedState.loading && <Loading label="Loading proxy view…" />}
          {scopedState.error != null && <ErrorView error={scopedState.error} />}
          {scoped?.kind === "schedule" && (
            scoped.data.items.length === 0 ? (
              <Empty label="No appointments for this patient." />
            ) : (
              <ul className="result-list">
                {scoped.data.items.map((a) => (
                  <li key={a.id} className="result-row">
                    <span>{(a as { start?: string }).start ?? ""}</span>
                    <span className="result-meta">{resourceStatus(a)}</span>
                  </li>
                ))}
              </ul>
            )
          )}
          {scoped?.kind === "messages" && (
            scoped.data.items.length === 0 ? (
              <Empty label="No message threads for this patient." />
            ) : (
              <ul className="result-list">
                {scoped.data.items.map((t) => (
                  <li key={t.thread_id} className="result-row">
                    <span>{t.topic ?? "Message thread"}</span>
                    <span className="result-meta">{t.last_text ?? ""}</span>
                  </li>
                ))}
              </ul>
            )
          )}
          {scoped?.kind === "results" && (
            (scoped.data.entry ?? []).length === 0 ? (
              <Empty label="No released results for this patient." />
            ) : (
              <ul className="result-list">
                {(scoped.data.entry ?? []).map(({ resource: r }) => (
                  <li key={`${r.resourceType}-${r.id}`} className="result-row">
                    <span className="result-type">{r.resourceType}</span>
                    <span>
                      {codeLabel((r as { code?: never }).code)}
                      {r.resourceType === "Observation" ? `: ${observationValue(r)}` : ""}
                    </span>
                    <span className="result-meta">
                      {resourceStatus(r)} · {resourceDate(r)}
                    </span>
                  </li>
                ))}
              </ul>
            )
          )}
        </div>
      )}
    </>
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
    if (!start) {
      setRequestNote("Choose a preferred start time first, then request.");
      return;
    }
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
