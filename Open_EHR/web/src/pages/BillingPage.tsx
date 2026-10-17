import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type {
  Claim,
  CodingQueueItem,
  DenialItem,
  PatientStatement,
  RemittanceIngestResult,
  Role,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
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

type ClaimAction = { to: string; label: string; note?: boolean };

interface ClaimMachine {
  actions: Record<string, ClaimAction[]>;
  states: string[];
  exportable: string[];
  maxAppeals: number;
}

const DEFAULT_CLAIM_MACHINE: ClaimMachine = {
  actions: NEXT_ACTIONS,
  states: CLAIM_STATES,
  exportable: EXPORTABLE,
  maxAppeals: 1,
};

const ACTION_LABELS: Record<string, string> = {
  ready: "Mark ready",
  submitted: "Submit claim",
  paid: "Record paid",
  denied: "Record denied",
};

/** Derive the claim buttons from the published claim-lifecycle
 *  payload (transitions + exportableStates + maxAppeals). The appeal
 *  action's label follows maxAppeals; resubmission after an appeal
 *  keeps its distinct label. */
export function claimMachineFromPayload(payload: unknown): ClaimMachine | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const p = payload as Record<string, unknown>;
  if (!Array.isArray(p.transitions)) return null;
  const maxAppeals =
    typeof p.maxAppeals === "number" && Number.isInteger(p.maxAppeals) && p.maxAppeals >= 0
      ? p.maxAppeals
      : 1;
  const actions: Record<string, ClaimAction[]> = {};
  for (const t of p.transitions) {
    if (!t || typeof t !== "object") continue;
    const { from, to } = t as { from?: unknown; to?: unknown };
    if (typeof from !== "string" || typeof to !== "string") continue;
    let label = ACTION_LABELS[to] ?? to;
    if (to === "appealed") {
      label = maxAppeals === 1 ? "Appeal (once, with note)" : "Appeal (with note)";
    }
    if (to === "submitted" && from === "appealed") label = "Resubmit after appeal";
    (actions[from] ??= []).push({ to, label, ...(to === "appealed" ? { note: true } : {}) });
  }
  if (Object.keys(actions).length === 0) return null;
  return {
    actions,
    states:
      Array.isArray(p.states) && p.states.every((s) => typeof s === "string")
        ? (p.states as string[])
        : CLAIM_STATES,
    exportable:
      Array.isArray(p.exportableStates) &&
      p.exportableStates.every((s) => typeof s === "string")
        ? (p.exportableStates as string[])
        : EXPORTABLE,
    maxAppeals,
  };
}

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
  const { permissions } = useRuntimeConfig();
  const canManage = permissions["billing.manage"]?.[role] === true;
  const canReadCoding = permissions["coding.read"]?.[role] === true;
  const canCode = permissions["coding.work"]?.[role] === true;
  const isPatient = role === "patient";
  const [machine, setMachine] = useState<ClaimMachine>(DEFAULT_CLAIM_MACHINE);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        if (typeof client.effectiveConfig !== "function") return;
        const cfg = await client.effectiveConfig("workflow", "claim-lifecycle");
        const built = claimMachineFromPayload(cfg?.payload);
        if (!cancelled && built) setMachine(built);
      } catch {
        // keep the built-in lifecycle
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);
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
        denied claim may be appealed with a note, as many times as the
        workflow config's maxAppeals allows (once by default). The 837 export is a
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
            {machine.states.map((s) => (
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
                  (machine.actions[detail.status ?? ""] ?? [])
                    .filter((a) => !(a.to === "appealed" && (detail.appealCount ?? 0) >= machine.maxAppeals))
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
                {machine.exportable.includes(detail.status ?? "") && (
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

      {!isPatient && canReadCoding && (
        <CodingSection client={client} canCode={canCode} tick={tick} bump={() => setTick((t) => t + 1)} />
      )}
      {!isPatient && (
        <RemittanceSection client={client} canManage={canManage} tick={tick} bump={() => setTick((t) => t + 1)} />
      )}
      {!isPatient && (
        <DenialsSection client={client} canManage={canManage} tick={tick} bump={() => setTick((t) => t + 1)} />
      )}
      <StatementSection
        client={client}
        role={role}
        ownPatientRef={claims[0]?.patient?.reference ?? null}
        tick={tick}
        bump={() => setTick((t) => t + 1)}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Phase 7 sections (app.revenue): HIM coding, 835 remittance, denials,
// statements. Each section owns its form state; the server is the
// authority for every validation, and its errors surface inline.
// ---------------------------------------------------------------------------

function sectionError(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function splitCodes(text: string): Array<{ code: string }> {
  return text
    .split(/[,\s]+/)
    .map((c) => c.trim())
    .filter(Boolean)
    .map((code) => ({ code }));
}

function CodingSection({
  client,
  canCode,
  tick,
  bump,
}: {
  client: ApiClient;
  canCode: boolean;
  tick: number;
  bump: () => void;
}) {
  const queueState = useAsync(() => client.codingQueue(), [client, tick]);
  const [drafts, setDrafts] = useState<Record<string, { dx: string; px: string }>>({});
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const queue: CodingQueueItem[] = queueState.data ?? [];

  async function assign(encounterId: string) {
    const draft = drafts[encounterId] ?? { dx: "", px: "" };
    setBusy(true);
    setError(null);
    try {
      await client.assignCodes(encounterId, {
        diagnoses: splitCodes(draft.dx),
        procedures: splitCodes(draft.px),
      });
      setNotice(`Coded encounter ${encounterId}.`);
      bump();
    } catch (err) {
      setError(`Coding ${encounterId} failed: ${sectionError(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>HIM coding queue</h3>
      <p className="hint">
        Finished encounters and encounters with signed notes wait here for
        ICD-10 / CPT assignment. Codes validate against the published Studio
        value sets (<code>terminology/coding-diagnoses</code>,{" "}
        <code>terminology/coding-procedures</code>); a coded encounter's
        diagnoses and procedures feed its claim. When the claim-lifecycle
        config sets <code>requireCoding</code>, uncoded encounters cannot
        generate claims at all.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      {queueState.loading && <Loading label="Loading coding queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && queue.length === 0 && (
        <Empty label="No encounters awaiting coding." />
      )}
      {queue.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Encounter</th>
              <th>Patient</th>
              <th>Why queued</th>
              <th>Diagnoses (ICD-10)</th>
              <th>Procedures (CPT)</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {queue.map((item) => {
              const draft = drafts[item.encounter_id] ?? { dx: "", px: "" };
              return (
                <tr key={item.encounter_id}>
                  <td>{item.encounter_id}</td>
                  <td>{item.patient?.replace("Patient/", "") ?? "—"}</td>
                  <td>{item.reason}</td>
                  <td>
                    <input
                      aria-label={`Diagnosis codes for ${item.encounter_id}`}
                      value={draft.dx}
                      disabled={!canCode}
                      placeholder="E11.9, I10"
                      onChange={(e) =>
                        setDrafts((d) => ({
                          ...d,
                          [item.encounter_id]: { ...draft, dx: e.target.value },
                        }))
                      }
                    />
                  </td>
                  <td>
                    <input
                      aria-label={`Procedure codes for ${item.encounter_id}`}
                      value={draft.px}
                      disabled={!canCode}
                      placeholder="99214"
                      onChange={(e) =>
                        setDrafts((d) => ({
                          ...d,
                          [item.encounter_id]: { ...draft, px: e.target.value },
                        }))
                      }
                    />
                  </td>
                  <td>
                    {canCode && (
                      <button
                        type="button"
                        disabled={busy || !draft.dx.trim()}
                        onClick={() => void assign(item.encounter_id)}
                      >
                        Assign codes
                      </button>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function RemittanceSection({
  client,
  canManage,
  tick,
  bump,
}: {
  client: ApiClient;
  canManage: boolean;
  tick: number;
  bump: () => void;
}) {
  const [text, setText] = useState("");
  const [result, setResult] = useState<RemittanceIngestResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const batchesState = useAsync(() => client.remittanceBatches(), [client, tick]);
  const exceptionsState = useAsync(() => client.remittanceExceptions(), [client, tick]);
  const batches = batchesState.data ?? [];
  const exceptions = exceptionsState.data ?? [];

  async function post() {
    setBusy(true);
    setError(null);
    try {
      const res = await client.ingest835(text);
      setResult(res);
      setText("");
      bump();
    } catch (err) {
      setError(`835 ingest failed: ${sectionError(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Remittance (X12 835)</h3>
      <p className="hint">
        Paste an 835 remittance file to post payments against submitted
        claims. This is a local file ingest — <strong>no clearinghouse
        connection exists</strong>. A malformed file posts nothing; CLP
        loops that cannot post (unknown claim, unmapped status, duplicate
        check) are recorded as exceptions below, never dropped.
      </p>
      {error && <div className="state state-error" role="alert">{error}</div>}
      {canManage && (
        <div>
          <textarea
            aria-label="835 remittance text"
            rows={6}
            cols={80}
            value={text}
            placeholder={"ISA*00*...~\nST*835*0001~\n..."}
            onChange={(e) => setText(e.target.value)}
          />
          <div className="toolbar">
            <button type="button" disabled={busy || !text.trim()} onClick={() => void post()}>
              Post 835 remittance
            </button>
          </div>
        </div>
      )}
      {result && (
        <p className="state state-empty" role="status">
          Batch {result.batch.id}: check {result.batch.check_number || "—"} —{" "}
          {result.batch.posted} posted, {result.batch.exceptions} exception(s).
        </p>
      )}
      {batches.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Check</th>
              <th>Payer</th>
              <th>Total</th>
              <th>Posted</th>
              <th>Exceptions</th>
            </tr>
          </thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.id}>
                <td>{b.check_number || "—"}</td>
                <td>{b.payer || "—"}</td>
                <td>{money(b.total_amount)}</td>
                <td>{b.posted}</td>
                <td>{b.exceptions}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {exceptions.length > 0 && (
        <>
          <h4>Remittance exceptions</h4>
          <table className="table">
            <thead>
              <tr>
                <th>Claim reference</th>
                <th>Reason</th>
              </tr>
            </thead>
            <tbody>
              {exceptions.map((e, i) => (
                <tr key={`${e.batch_id ?? ""}-${e.claim_ref}-${i}`}>
                  <td>{e.claim_ref}</td>
                  <td>{e.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}

function DenialsSection({
  client,
  canManage,
  tick,
  bump,
}: {
  client: ApiClient;
  canManage: boolean;
  tick: number;
  bump: () => void;
}) {
  const denialsState = useAsync(() => client.denials(), [client, tick]);
  const [assignees, setAssignees] = useState<Record<string, string>>({});
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  const [rebilled, setRebilled] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const items: DenialItem[] = denialsState.data?.items ?? [];
  const outcomeOptions: string[] = denialsState.data?.outcomes ?? [];

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await fn();
      bump();
    } catch (err) {
      setError(`${label} failed: ${sectionError(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Denials workqueue</h3>
      <p className="hint">
        Denied and partially-paid claims with their remittance reasons
        (categories from <code>terminology/denial-categories</code>; reason
        codes are stored verbatim from the 835). Appealing runs through the
        claim lifecycle above — the workqueue records the work, it never
        forks the lifecycle. Rebilling creates a new corrected claim; the
        original is preserved.
      </p>
      {error && <div className="state state-error" role="alert">{error}</div>}
      {denialsState.loading && <Loading label="Loading denials…" />}
      {denialsState.error != null && <ErrorView error={denialsState.error} />}
      {!denialsState.loading && !denialsState.error && items.length === 0 && (
        <Empty label="No denied or partially-paid claims to work." />
      )}
      {items.map((item) => {
        const claimId = item.claim.id as string;
        return (
          <div key={claimId}>
            <p>
              <strong>{claimId}</strong> · <span className="badge">{item.kind}</span>{" "}
              · status {item.claim.status} · balance{" "}
              <strong>{money(item.claim.financials.balance)}</strong>
              {item.work.assignee ? ` · assigned to ${item.work.assignee}` : ""}
              {item.work.outcome ? ` · outcome: ${item.work.outcome}` : ""}
            </p>
            {item.reasons.length > 0 && (
              <p className="hint">
                Reasons:{" "}
                {item.reasons
                  .map(
                    (r) =>
                      `${r.group}/${r.reason} (${r.category}) ${money(r.amount)}`,
                  )
                  .join("; ")}
              </p>
            )}
            {item.work.notes.length > 0 && (
              <p className="hint">
                Notes: {item.work.notes.map((n) => `${n.by}: ${n.text}`).join(" · ")}
              </p>
            )}
            {canManage && (
              <div className="toolbar">
                <input
                  aria-label={`Assignee for ${claimId}`}
                  value={assignees[claimId] ?? ""}
                  placeholder="Assignee"
                  onChange={(e) =>
                    setAssignees((s) => ({ ...s, [claimId]: e.target.value }))
                  }
                />
                <button
                  type="button"
                  disabled={busy || !(assignees[claimId] ?? "").trim()}
                  onClick={() =>
                    void act("Assign", () =>
                      client.assignDenial(claimId, (assignees[claimId] ?? "").trim()))
                  }
                >
                  Assign
                </button>
                <input
                  aria-label={`Workqueue note for ${claimId}`}
                  value={notes[claimId] ?? ""}
                  placeholder="Workqueue note"
                  onChange={(e) =>
                    setNotes((s) => ({ ...s, [claimId]: e.target.value }))
                  }
                />
                <button
                  type="button"
                  disabled={busy || !(notes[claimId] ?? "").trim()}
                  onClick={() =>
                    void act("Add note", () =>
                      client.noteDenial(claimId, (notes[claimId] ?? "").trim()))
                  }
                >
                  Add note
                </button>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void act("Rebill", async () => {
                      const created = await client.rebillClaim(claimId);
                      setRebilled((s) => ({ ...s, [claimId]: created.id as string }));
                    })
                  }
                >
                  Rebill (corrected claim)
                </button>
                <select
                  aria-label={`Outcome for ${claimId}`}
                  value={outcomes[claimId] ?? ""}
                  onChange={(e) =>
                    setOutcomes((s) => ({ ...s, [claimId]: e.target.value }))
                  }
                >
                  <option value="">Outcome…</option>
                  {outcomeOptions.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy || !outcomes[claimId]}
                  onClick={() =>
                    void act("Resolve", () =>
                      client.resolveDenial(
                        claimId,
                        outcomes[claimId],
                        rebilled[claimId],
                      ))
                  }
                >
                  Resolve
                </button>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// Mirrors the seeded terminology/payment-methods config; the server
// validates against the published set.
const PAYMENT_METHODS = ["card", "ach", "cash", "check"];

function StatementSection({
  client,
  role,
  ownPatientRef,
  tick,
  bump,
}: {
  client: ApiClient;
  role: Role;
  ownPatientRef: string | null;
  tick: number;
  bump: () => void;
}) {
  const isPatient = role === "patient";
  const [patientInput, setPatientInput] = useState("");
  const [patient, setPatient] = useState<string | null>(null);
  const [amounts, setAmounts] = useState<Record<string, string>>({});
  const [methods, setMethods] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const target = isPatient ? ownPatientRef : patient;
  const stmtState = useAsync(
    () => (target ? client.statement(target) : Promise.resolve(null)),
    [client, target, tick],
  );
  const stmt: PatientStatement | null = stmtState.data ?? null;

  async function pay(claimId: string) {
    const amount = Number(amounts[claimId]);
    if (!Number.isFinite(amount) || amount <= 0) return;
    setBusy(true);
    setError(null);
    try {
      await client.recordPatientPayment(claimId, amount, methods[claimId] ?? "card");
      bump();
    } catch (err) {
      setError(`Recording payment failed: ${sectionError(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>{isPatient ? "Your statement" : "Patient statement"}</h3>
      <p className="hint">
        Billed, paid, adjusted and patient-responsibility amounts derive
        from posted remittances. Recording a payment writes a ledger entry
        for a payment the practice has taken — <strong>this system has no
        card processor or payment gateway</strong>.
      </p>
      {!isPatient && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            if (patientInput.trim()) setPatient(patientInput.trim());
          }}
        >
          <label>
            Patient id{" "}
            <input
              aria-label="Statement patient id"
              value={patientInput}
              onChange={(e) => setPatientInput(e.target.value)}
              placeholder="pat-1"
            />
          </label>
          <button type="submit" disabled={!patientInput.trim()}>
            Load statement
          </button>
        </form>
      )}
      {isPatient && !ownPatientRef && (
        <Empty label="No claims yet, so no statement to show." />
      )}
      {error && <div className="state state-error" role="alert">{error}</div>}
      {stmtState.loading && target && <Loading label="Loading statement…" />}
      {stmtState.error != null && <ErrorView error={stmtState.error} />}
      {stmt && (
        <div>
          <table className="table">
            <thead>
              <tr>
                <th>Claim</th>
                <th>Status</th>
                <th>Billed</th>
                <th>Paid</th>
                <th>Adjusted</th>
                <th>Your share</th>
                <th>Share paid</th>
                <th>Share due</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {stmt.claims.map((line) => (
                <tr key={line.claim_id}>
                  <td>{line.claim_id}</td>
                  <td><span className="badge">{line.status ?? "—"}</span></td>
                  <td>{money(line.total)}</td>
                  <td>{money(line.paid)}</td>
                  <td>{money(line.adjustments)}</td>
                  <td>{money(line.patient_responsibility)}</td>
                  <td>{money(line.patient_paid)}</td>
                  <td><strong>{money(line.patient_due)}</strong></td>
                  <td>
                    {line.patient_due > 0 && (
                      <span>
                        <input
                          aria-label={`Payment amount for ${line.claim_id}`}
                          value={amounts[line.claim_id] ?? ""}
                          placeholder="0.00"
                          size={7}
                          onChange={(e) =>
                            setAmounts((s) => ({
                              ...s,
                              [line.claim_id]: e.target.value,
                            }))
                          }
                        />
                        <select
                          aria-label={`Payment method for ${line.claim_id}`}
                          value={methods[line.claim_id] ?? "card"}
                          onChange={(e) =>
                            setMethods((s) => ({
                              ...s,
                              [line.claim_id]: e.target.value,
                            }))
                          }
                        >
                          {PAYMENT_METHODS.map((m) => (
                            <option key={m} value={m}>{m}</option>
                          ))}
                        </select>
                        <button
                          type="button"
                          disabled={busy || !(amounts[line.claim_id] ?? "").trim()}
                          onClick={() => void pay(line.claim_id)}
                        >
                          Record payment
                        </button>
                      </span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p>
            Total patient share due:{" "}
            <strong>{money(stmt.totals.patient_due)}</strong> · total claim
            balance outstanding: <strong>{money(stmt.totals.balance)}</strong>
          </p>
          <p className="hint">{stmt.disclaimer}</p>
        </div>
      )}
    </div>
  );
}
