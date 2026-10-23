import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { refLabel } from "../../api/display";
import type { Role } from "../../api/types";
import { useRuntimeConfig } from "../../auth/runtimeConfig";
import { StatusBadge } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { errorText } from "./shared";

interface Props {
  client: ApiClient;
  role: Role;
}

interface HardSignal {
  type: string;
  signal_key: string;
  detail: string;
}

interface DurDispositionRecord {
  id?: number;
  signal_type: string;
  signal_key: string;
  disposition: string;
  reason_code: string | null;
  note: string | null;
  by: string;
  at: string | null;
}

const FALLBACK_DISPOSITIONS = [
  "override",
  "order-changed",
  "therapy-changed",
  "not-applicable",
];

/** Normalise a governed reason-code list (plain strings or
 *  {code, display} objects) from published config / API payloads. */
function codeList(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const entry of raw) {
    if (typeof entry === "string" && entry) out.push(entry);
    else if (
      entry != null &&
      typeof entry === "object" &&
      typeof (entry as { code?: unknown }).code === "string"
    ) {
      out.push((entry as { code: string }).code);
    }
  }
  return out;
}

/** Verification & DUR tab: the pharmacist work centre. The verification
 *  queue is priority-ordered server-side with SLA posture from the
 *  published pharmacy-ops config; restricted formulary items need an
 *  explicit acknowledgement, non-formulary items cannot be verified.
 *  Wave A (PHARM-M1): verification hard-stops on allergy conflicts,
 *  monitoring blockers and high-alert medications — the panel shows
 *  the signals and collects a governed override (reason code from
 *  the published list + a note) when any are present. The DUR queue
 *  is actionable: every signal can be disposed with a disposition, a
 *  governed reason code and a note, and recorded dispositions ride
 *  the queue items. Drug-interaction checking is INERT — no
 *  knowledge source is configured, and the banner says so rather
 *  than implying pairs were checked. */
export function VerificationTab({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canVerify = permissions["pharmacy.verify"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [acked, setAcked] = useState<Record<string, boolean>>({});
  const [overrideCode, setOverrideCode] = useState<Record<string, string>>({});
  const [overrideNote, setOverrideNote] = useState<Record<string, string>>({});
  const [dispChoice, setDispChoice] = useState<Record<string, string>>({});
  const [dispCode, setDispCode] = useState<Record<string, string>>({});
  const [dispNote, setDispNote] = useState<Record<string, string>>({});

  const queueState = useAsync(() => client.pharmacyVerificationQueue(), [client, tick]);
  const durState = useAsync(() => client.durQueue(), [client, tick]);
  const opsState = useAsync(
    () =>
      typeof client.effectiveConfig === "function"
        ? client.effectiveConfig("terminology", "pharmacy-ops")
        : Promise.resolve(null),
    [client],
  );
  const queue = queueState.data ?? [];
  const dur = durState.data;
  // Governed DUR override reason codes: the DUR response carries the
  // server's list; the published pharmacy-ops config is the fallback.
  const reasonCodes =
    codeList(
      (dur as unknown as { dur_override_reason_codes?: unknown } | null)
        ?.dur_override_reason_codes,
    ).length > 0
      ? codeList(
          (dur as unknown as { dur_override_reason_codes?: unknown })
            .dur_override_reason_codes,
        )
      : codeList(
          (opsState.data as { payload?: { durOverrideReasonCodes?: unknown } } | null)
            ?.payload?.durOverrideReasonCodes,
        );
  const dispositionKinds =
    (dur as unknown as { dur_dispositions?: string[] } | null)
      ?.dur_dispositions ?? FALLBACK_DISPOSITIONS;

  async function verify(
    orderId: string,
    restricted: boolean,
    signals: HardSignal[],
  ) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const needsOverride = signals.length > 0;
      const body = {
        ...(restricted ? { restriction_acknowledged: true } : {}),
        ...(needsOverride
          ? {
              override_reason_code: overrideCode[orderId],
              override_note: overrideNote[orderId]?.trim(),
            }
          : {}),
      };
      let outcome: {
        verification: { verified_by: string };
        rule_firings?: Array<{ rule: string; action: string }>;
      };
      const c = client as unknown as {
        request?: <T>(path: string, init?: RequestInit) => Promise<T>;
      };
      if (typeof c.request === "function") {
        outcome = await c.request(
          `/pharmacy/verification/${encodeURIComponent(orderId)}/verify`,
          { method: "POST", body: JSON.stringify(body) },
        );
      } else {
        outcome = await client.verifyOrder(orderId, {
          restrictionAcknowledged: restricted,
          ...(needsOverride
            ? {
                overrideReasonCode: overrideCode[orderId],
                overrideNote: overrideNote[orderId]?.trim(),
              }
            : {}),
        } as Parameters<ApiClient["verifyOrder"]>[1]);
      }
      const firings = (outcome.rule_firings ?? [])
        .map((f) => `${f.rule} (${f.action})`)
        .join(", ");
      setNotice(
        `Order ${orderId} verified by ${outcome.verification.verified_by}.` +
          (firings ? ` Rule firings: ${firings}.` : " No rules fired."),
      );
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Verify failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function recordDisposition(
    orderId: string,
    signal: { type: string; signal_key?: string },
  ) {
    const key = `${orderId}|${signal.type}|${signal.signal_key ?? signal.type}`;
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const payload = {
        signal_type: signal.type,
        signal_key: signal.signal_key ?? signal.type,
        disposition: dispChoice[key] ?? "override",
        reason_code: dispCode[key],
        note: dispNote[key]?.trim(),
      };
      const c = client as unknown as {
        request?: <T>(path: string, init?: RequestInit) => Promise<T>;
        durDisposition?: (
          id: string,
          input: typeof payload,
        ) => Promise<unknown>;
      };
      if (typeof c.request === "function") {
        await c.request(
          `/pharmacy/dur/${encodeURIComponent(orderId)}/disposition`,
          { method: "POST", body: JSON.stringify(payload) },
        );
      } else if (typeof c.durDisposition === "function") {
        await c.durDisposition(orderId, payload);
      }
      setNotice(
        `DUR disposition recorded for ${orderId} (${signal.type}: ${payload.disposition}).`,
      );
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Disposition failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Verification queue</h3>
      <p className="hint">
        Signed/active medication orders awaiting pharmacist verification,
        ordered by priority then wait. SLA minutes per priority are the
        published pharmacy-ops configuration; breaches are flagged.
        Verifying screens the order through the rules engine — the renal
        rule fires when the patient&apos;s creatinine clearance is below
        the configured threshold. Verification hard-stops on a recorded
        allergy conflict, a monitoring blocker or a high-alert
        medication: the signals are shown on the row, and verifying past
        them needs a governed override reason code and note (or a
        recorded DUR disposition for the signal).
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}
      {queueState.loading && <Loading label="Loading verification queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && queue.length === 0 && (
        <Empty label="No orders awaiting verification." />
      )}
      {queue.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Order</th>
              <th>Patient</th>
              <th>Medication</th>
              <th>Priority</th>
              <th>Wait</th>
              <th>Formulary</th>
              <th>Flags</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {queue.map((item) => {
              const extended = item as unknown as {
                high_alert?: boolean;
                hard_stop_signals?: HardSignal[];
              };
              const restricted = item.formulary_status === "restricted";
              const nonFormulary = item.formulary_status === "non-formulary";
              const signals = extended.hard_stop_signals ?? [];
              const overrideReady = Boolean(
                overrideCode[item.order_id] &&
                  overrideNote[item.order_id]?.trim(),
              );
              return (
                <tr key={item.order_id}>
                  <td>{item.order_id}</td>
                  <td>{refLabel({ reference: item.patient_ref })}</td>
                  <td>
                    {item.medication_display ?? item.medication_code ?? "—"}
                  </td>
                  <td>{item.priority ?? "routine"}</td>
                  <td>
                    {item.wait_minutes ?? "—"} min
                    {item.sla_breached && (
                      <StatusBadge tone="bad">SLA breached</StatusBadge>
                    )}
                  </td>
                  <td>
                    {item.formulary_status}
                    {item.restriction_note && (
                      <div className="hint">{item.restriction_note}</div>
                    )}
                    {restricted && (
                      <label className="hint">
                        <input
                          type="checkbox"
                          aria-label={`Acknowledge restriction for ${item.order_id}`}
                          checked={Boolean(acked[item.order_id])}
                          onChange={(e) =>
                            setAcked((a) => ({
                              ...a,
                              [item.order_id]: e.target.checked,
                            }))}
                        />{" "}
                        Restriction acknowledged
                      </label>
                    )}
                  </td>
                  <td>
                    {item.allergy_conflict && (
                      <StatusBadge tone="bad">
                        allergy: {item.allergy_conflict.display}
                      </StatusBadge>
                    )}
                    {item.renal.flagged && (
                      <StatusBadge tone="warn">
                        renal: CrCl {item.renal.crcl_ml_min} mL/min
                      </StatusBadge>
                    )}
                    {item.monitoring_blockers.length > 0 && (
                      <StatusBadge tone="warn">
                        monitoring: {item.monitoring_blockers[0].problem}
                      </StatusBadge>
                    )}
                    {extended.high_alert && (
                      <StatusBadge tone="bad">high-alert</StatusBadge>
                    )}
                    {signals.map((s, i) => (
                      <div key={i} className="hint">
                        hard stop — {s.type}: {s.detail}
                      </div>
                    ))}
                    {signals.length > 0 && canVerify && (
                      <div>
                        <label>
                          Override reason code{" "}
                          <select
                            aria-label={`Override reason code for ${item.order_id}`}
                            value={overrideCode[item.order_id] ?? ""}
                            disabled={reasonCodes.length === 0}
                            onChange={(e) =>
                              setOverrideCode((m) => ({
                                ...m,
                                [item.order_id]: e.target.value,
                              }))}
                          >
                            <option value="">Select a governed reason code</option>
                            {reasonCodes.map((code) => (
                              <option key={code} value={code}>
                                {code}
                              </option>
                            ))}
                          </select>
                        </label>
                        {reasonCodes.length === 0 && (
                          <span className="hint">
                            {" "}
                            No governed override reason codes are published
                            in the pharmacy-ops config — overrides are
                            disabled until a site publishes codes.
                          </span>
                        )}
                        <label>
                          Override note{" "}
                          <input
                            aria-label={`Override note for ${item.order_id}`}
                            value={overrideNote[item.order_id] ?? ""}
                            onChange={(e) =>
                              setOverrideNote((m) => ({
                                ...m,
                                [item.order_id]: e.target.value,
                              }))}
                          />
                        </label>
                      </div>
                    )}
                  </td>
                  <td>
                    {canVerify && (
                      <button
                        type="button"
                        disabled={
                          busy ||
                          nonFormulary ||
                          (restricted && !acked[item.order_id]) ||
                          (signals.length > 0 && !overrideReady)
                        }
                        title={
                          nonFormulary
                            ? "Non-formulary items cannot be verified"
                            : restricted && !acked[item.order_id]
                              ? "Acknowledge the restriction first"
                              : signals.length > 0 && !overrideReady
                                ? "Hard-stop signal(s) present: choose a governed override reason code and enter a note, or record a DUR disposition for the signal"
                                : undefined
                        }
                        onClick={() =>
                          void verify(item.order_id, restricted, signals)}
                      >
                        Verify
                      </button>
                    )}
                    {signals.length > 0 && !overrideReady && canVerify && (
                      <span className="hint">
                        {" "}
                        hard stop: governed override required
                      </span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>DUR review queue</h3>
      {durState.loading && <Loading label="Loading DUR queue…" />}
      {durState.error != null && <ErrorView error={durState.error} />}
      {dur && (
        <>
          <p className="hint" role="status">
            Drug-interaction checking:{" "}
            <strong>{dur.interaction_checking.status}</strong> —{" "}
            {dur.interaction_checking.message}
          </p>
          {dur.items.length === 0 ? (
            <Empty label="No open orders carry DUR signals." />
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th>Order</th>
                  <th>Patient</th>
                  <th>Medication</th>
                  <th>Signals</th>
                  <th>Disposition</th>
                </tr>
              </thead>
              <tbody>
                {dur.items.map((item) => {
                  const dispositions =
                    (item as unknown as { dispositions?: DurDispositionRecord[] })
                      .dispositions ?? [];
                  return (
                    <tr key={item.order_id}>
                      <td>{item.order_id}</td>
                      <td>{refLabel({ reference: item.patient_ref })}</td>
                      <td>
                        {item.medication_display ?? item.medication_code ?? "—"}
                      </td>
                      <td>
                        {item.signals.map((s, i) => (
                          <div key={i}>
                            <span className="badge">{s.type}</span> {s.detail}
                          </div>
                        ))}
                        {dispositions.map((d) => (
                          <div key={d.id ?? `${d.signal_type}-${d.signal_key}`} className="hint">
                            disposition: {d.signal_type} → {d.disposition}
                            {d.reason_code ? ` (${d.reason_code})` : ""} by{" "}
                            {d.by}
                            {d.note ? ` — ${d.note}` : ""}
                          </div>
                        ))}
                      </td>
                      <td>
                        {canVerify &&
                          item.signals.map((s, i) => {
                            const ext = s as unknown as {
                              signal_key?: string;
                            };
                            const signalKey = ext.signal_key ?? s.type;
                            const key = `${item.order_id}|${s.type}|${signalKey}`;
                            return (
                              <div key={i}>
                                <label>
                                  Disposition for {s.type}{" "}
                                  <select
                                    aria-label={`Disposition for ${item.order_id} ${s.type}`}
                                    value={dispChoice[key] ?? "override"}
                                    onChange={(e) =>
                                      setDispChoice((m) => ({
                                        ...m,
                                        [key]: e.target.value,
                                      }))}
                                  >
                                    {dispositionKinds.map((d) => (
                                      <option key={d} value={d}>
                                        {d}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                <label>
                                  Reason code{" "}
                                  <select
                                    aria-label={`DUR reason code for ${item.order_id} ${s.type}`}
                                    value={dispCode[key] ?? ""}
                                    disabled={reasonCodes.length === 0}
                                    onChange={(e) =>
                                      setDispCode((m) => ({
                                        ...m,
                                        [key]: e.target.value,
                                      }))}
                                  >
                                    <option value="">
                                      Select a governed reason code
                                    </option>
                                    {reasonCodes.map((code) => (
                                      <option key={code} value={code}>
                                        {code}
                                      </option>
                                    ))}
                                  </select>
                                </label>
                                {reasonCodes.length === 0 && (
                                  <span className="hint">
                                    {" "}
                                    No governed reason codes are published —
                                    dispositions are disabled until a site
                                    publishes codes.
                                  </span>
                                )}
                                <label>
                                  Note{" "}
                                  <input
                                    aria-label={`DUR note for ${item.order_id} ${s.type}`}
                                    value={dispNote[key] ?? ""}
                                    onChange={(e) =>
                                      setDispNote((m) => ({
                                        ...m,
                                        [key]: e.target.value,
                                      }))}
                                  />
                                </label>
                                <button
                                  type="button"
                                  disabled={
                                    busy ||
                                    reasonCodes.length === 0 ||
                                    !dispCode[key] ||
                                    !dispNote[key]?.trim()
                                  }
                                  title={
                                    reasonCodes.length === 0
                                      ? "No governed reason codes are published"
                                      : !dispCode[key] || !dispNote[key]?.trim()
                                        ? "Choose a governed reason code and enter a note to record the disposition"
                                        : undefined
                                  }
                                  onClick={() =>
                                    void recordDisposition(item.order_id, {
                                      type: s.type,
                                      signal_key: signalKey,
                                    })}
                                >
                                  Record disposition
                                </button>
                              </div>
                            );
                          })}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </div>
  );
}
