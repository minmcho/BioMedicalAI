import { useEffect, useState } from "react";
import {
  allowedTransitions,
  isTerminal,
  machineFromWorkflowPayload,
  TRANSITION_LABELS,
} from "../api/adt";
import type { ApiClient } from "../api/client";
import { ApiError } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type {
  Appointment,
  EligibilityCheckResult,
  PriorAuth,
  Referral,
  Role,
  WaitlistEntry,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function toIso(local: string): string | undefined {
  if (!local) return undefined;
  const d = new Date(local);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

/** Scheduling — Appointment search by practitioner + date range, with ADT
 *  transition buttons driven by the real state machine. Buttons only ever
 *  offer transitions the server allows; terminal states offer none. */
export function SchedulingPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canReadAccess = permissions["access.read"]?.[role] === true;
  const canWorkAccess = permissions["access.work"]?.[role] === true;
  const [practitioner, setPractitioner] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [params, setParams] = useState<{ practitioner?: string; start?: string; end?: string }>({});
  const [busyId, setBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [overrides, setOverrides] = useState<Record<string, Appointment>>({});

  const { data, loading, error, reload } = useAsync<Appointment[]>(
    () => client.search<Appointment>("Appointment", params),
    [client, params],
  );

  // The ADT machine is Studio configuration (workflow/appointment-adt):
  // when the published payload is fetchable, the offered transitions
  // come from it; otherwise the built-in map (identical defaults)
  // drives the buttons.
  const [machine, setMachine] = useState<Record<string, string[]> | null>(null);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        if (typeof client.effectiveConfig !== "function") return;
        const cfg = await client.effectiveConfig("workflow", "appointment-adt");
        const built = machineFromWorkflowPayload(cfg?.payload);
        if (!cancelled && built) setMachine(built);
      } catch {
        // keep the built-in machine
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);

  async function doTransition(appt: Appointment, toStatus: string) {
    if (!appt.id) return;
    setBusyId(appt.id);
    setActionError(null);
    try {
      const updated = await client.transitionAppointment(appt.id, toStatus);
      setOverrides((prev) => ({ ...prev, [appt.id as string]: updated }));
    } catch (err) {
      setActionError(
        err instanceof ApiError ? err.detail : "Transition failed. Reload the list and try again.",
      );
      reload();
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section>
      <h2>Scheduling</h2>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          setOverrides({});
          setParams({
            practitioner: practitioner.trim() || undefined,
            start: toIso(start),
            end: toIso(end),
          });
        }}
      >
        <label className="field">
          Practitioner id or reference
          <input
            value={practitioner}
            onChange={(e) => setPractitioner(e.target.value)}
            placeholder="dr-1 or Practitioner/dr-1"
          />
        </label>
        <label className="field">
          From
          <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
        </label>
        <label className="field">
          To
          <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} />
        </label>
        <button type="submit">Search appointments</button>
      </form>

      {actionError ? (
        <div className="state state-error" role="alert">
          <strong>Transition failed.</strong> {actionError}
        </div>
      ) : null}

      {loading ? (
        <Loading label="Loading appointments…" />
      ) : error ? (
        <ErrorView error={error} />
      ) : !data || data.length === 0 ? (
        <Empty label="No appointments match." />
      ) : (
        <ul className="appt-list">
          {data.map((raw) => {
            const appt = (raw.id && overrides[raw.id]) || raw;
            const targets = machine
              ? allowedTransitions(appt.status, machine)
              : allowedTransitions(appt.status);
            return (
              <li key={appt.id} className="appt-row">
                <div className="appt-main">
                  <strong>{appt.description ?? `Appointment ${appt.id}`}</strong>
                  <span className="appt-meta">
                    status: <span data-testid="appt-status">{appt.status ?? "—"}</span>
                    {appt.start ? ` · starts ${new Date(appt.start).toLocaleString()}` : ""}
                  </span>
                  <span className="appt-meta">
                    {(appt.participant ?? [])
                      .map((p) => refLabel(p.actor))
                      .filter((v) => v !== "—")
                      .join(" · ")}
                  </span>
                </div>
                <div className="appt-actions">
                  {isTerminal(appt.status) || targets.length === 0 ? (
                    <span className="hint">No further transitions</span>
                  ) : (
                    targets.map((target) => (
                      <button
                        key={target}
                        type="button"
                        disabled={busyId === appt.id}
                        onClick={() => doTransition(appt, target)}
                      >
                        {TRANSITION_LABELS[target] ?? target}
                      </button>
                    ))
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {canReadAccess && (
        <AccessSections client={client} canWork={canWorkAccess} />
      )}
    </section>
  );
}

// ---------------------------------------------------------------------------
// Phase 7 access sections (app.access): referrals + triage,
// authorisations, eligibility, waitlist. Services, payers, slot types
// and the lifecycles are Studio configuration on the server; the
// selects below mirror the seeded terminology and the server
// validates everything against the published sets.
// ---------------------------------------------------------------------------

const REFERRAL_SERVICES = [
  "cardiology", "ophthalmology", "orthopaedics", "gastroenterology",
  "rehabilitation", "psychiatry", "neurology", "dermatology",
];
const SLOT_TYPES = ["new-patient", "follow-up", "procedure"];

// The seeded referral-flow machine (workflow/referral-flow) — buttons
// only; the server enforces the published config.
const REFERRAL_NEXT: Record<string, string[]> = {
  received: ["triaged", "cancelled"],
  triaged: ["authorised", "denied", "cancelled"],
  authorised: ["scheduled", "cancelled"],
  scheduled: ["completed", "cancelled"],
};

function accessError(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function AccessSections({ client, canWork }: { client: ApiClient; canWork: boolean }) {
  const [tick, setTick] = useState(0);
  const bump = () => setTick((t) => t + 1);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const payersState = useAsync(() => client.payers(), [client]);
  const payerCodes: string[] = (
    payersState.data?.payers?.concepts ?? []
  ).map((c) => c.code);

  const queueState = useAsync(() => client.referralQueue(), [client, tick]);
  const authsState = useAsync(() => client.authorisations(), [client, tick]);
  const waitlistState = useAsync(() => client.waitlist(), [client, tick]);
  const queue: Referral[] = queueState.data ?? [];
  const auths: PriorAuth[] = authsState.data ?? [];
  const entries: WaitlistEntry[] = waitlistState.data ?? [];

  // Referral intake form
  const [refPatient, setRefPatient] = useState("");
  const [refService, setRefService] = useState(REFERRAL_SERVICES[0]);
  const [refOrg, setRefOrg] = useState("");
  const [refReason, setRefReason] = useState("");
  const [refPayer, setRefPayer] = useState("");
  // Authorisation form
  const [authPatient, setAuthPatient] = useState("");
  const [authPayer, setAuthPayer] = useState("");
  const [authService, setAuthService] = useState("");
  const [authUnits, setAuthUnits] = useState("");
  // Eligibility form + result
  const [eligPatient, setEligPatient] = useState("");
  const [eligPayer, setEligPayer] = useState("");
  const [eligService, setEligService] = useState("");
  const [eligDate, setEligDate] = useState("");
  const [eligResult, setEligResult] = useState<EligibilityCheckResult | null>(null);
  // Waitlist form + offer/accept
  const [wlPatient, setWlPatient] = useState("");
  const [wlSlot, setWlSlot] = useState(SLOT_TYPES[1]);
  const [wlPriority, setWlPriority] = useState("");
  const [offerAppt, setOfferAppt] = useState("");
  const [acceptStart, setAcceptStart] = useState<Record<string, string>>({});

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      bump();
    } catch (err) {
      setError(`${label} failed: ${accessError(err)}`);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}

      <h3>Referrals — triage queue</h3>
      <p className="hint">
        Inbound referrals arrive as ServiceRequests with a triage task and
        move through the published <code>workflow/referral-flow</code>:
        received → triaged → authorised / denied → scheduled → completed.
        Authorising is a clinical decision in the seeded config.
      </p>
      {canWork && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void act("Referral intake", () =>
              client.createReferral({
                patient: refPatient.trim(),
                service_code: refService,
                referring_organization: refOrg.trim(),
                reason: refReason.trim() || undefined,
                payer: refPayer || undefined,
              }));
          }}
        >
          <label>
            Patient id{" "}
            <input aria-label="Referral patient id" value={refPatient}
              onChange={(e) => setRefPatient(e.target.value)} placeholder="pat-1" />
          </label>
          <label>
            Service{" "}
            <select aria-label="Referral service" value={refService}
              onChange={(e) => setRefService(e.target.value)}>
              {REFERRAL_SERVICES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>
            Referring organisation{" "}
            <input aria-label="Referring organisation" value={refOrg}
              onChange={(e) => setRefOrg(e.target.value)} />
          </label>
          <label>
            Reason{" "}
            <input aria-label="Referral reason" value={refReason}
              onChange={(e) => setRefReason(e.target.value)} />
          </label>
          <label>
            Payer{" "}
            <select aria-label="Referral payer" value={refPayer}
              onChange={(e) => setRefPayer(e.target.value)}>
              <option value="">—</option>
              {payerCodes.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy || !refPatient.trim() || !refOrg.trim()}>
            Record referral
          </button>
        </form>
      )}
      {queueState.loading && <Loading label="Loading referral queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && queue.length === 0 && (
        <Empty label="No referrals awaiting triage." />
      )}
      {queue.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Patient</th>
              <th>Service</th>
              <th>From</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {queue.map((ref) => (
              <tr key={ref.id}>
                <td>{refLabel(ref.subject)}</td>
                <td>{codeLabel(ref.code)}</td>
                <td>{ref.referringOrganization ?? "—"}</td>
                <td><span className="badge">{ref.status}</span></td>
                <td>
                  {canWork &&
                    (REFERRAL_NEXT[ref.status ?? ""] ?? []).map((target) => (
                      <button
                        key={target}
                        type="button"
                        disabled={busy}
                        onClick={() =>
                          void act(`Referral → ${target}`, () =>
                            client.transitionReferral(ref.id as string, target))
                        }
                      >
                        {target}
                      </button>
                    ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Prior authorisations</h3>
      <p className="hint">
        Authorisations are payer decisions recorded as data (number, payer,
        approved units, validity window). A service flagged in the published
        payer config (<code>terminology/payers</code> →{" "}
        <code>authRequiredServices</code>) cannot be signed without an
        approved authorisation covering it — the gate is inert until a site
        publishes such a config.
      </p>
      {canWork && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void act("Authorisation requested", () =>
              client.createAuthorisation({
                patient: authPatient.trim(),
                payer: authPayer || payerCodes[0] || "",
                service_code: authService.trim(),
                approved_units: authUnits ? Number(authUnits) : undefined,
              }));
          }}
        >
          <label>
            Patient id{" "}
            <input aria-label="Authorisation patient id" value={authPatient}
              onChange={(e) => setAuthPatient(e.target.value)} placeholder="pat-1" />
          </label>
          <label>
            Payer{" "}
            <select aria-label="Authorisation payer" value={authPayer}
              onChange={(e) => setAuthPayer(e.target.value)}>
              <option value="">—</option>
              {payerCodes.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label>
            Service code{" "}
            <input aria-label="Authorisation service code" value={authService}
              onChange={(e) => setAuthService(e.target.value)} placeholder="imaging" />
          </label>
          <label>
            Approved units{" "}
            <input aria-label="Approved units" value={authUnits} size={4}
              onChange={(e) => setAuthUnits(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !authPatient.trim() || !authService.trim()}>
            Request authorisation
          </button>
        </form>
      )}
      {auths.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Number</th>
              <th>Patient</th>
              <th>Payer</th>
              <th>Service</th>
              <th>Status</th>
              <th>Units</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {auths.map((auth) => (
              <tr key={auth.id}>
                <td>{auth.auth_number}</td>
                <td>{auth.patient.replace("Patient/", "")}</td>
                <td>{auth.payer}</td>
                <td>{auth.service_code}</td>
                <td><span className="badge">{auth.status}</span></td>
                <td>{auth.used_units} / {auth.approved_units ?? "∞"}</td>
                <td>
                  {canWork && auth.status === "requested" && (
                    <>
                      <button type="button" disabled={busy}
                        onClick={() =>
                          void act("Authorisation approved", () =>
                            client.transitionAuthorisation(auth.id, "approved"))}>
                        Approve
                      </button>
                      <button type="button" disabled={busy}
                        onClick={() =>
                          void act("Authorisation denied", () =>
                            client.transitionAuthorisation(auth.id, "denied"))}>
                        Deny
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Eligibility check</h3>
      <p className="hint">
        A deterministic evaluation of the published payer configuration,
        recorded for audit. <strong>Local configuration check only — this
        system has no live payer connection (no EDI 270/271)</strong>, and
        the result is never a real-time payer response.
      </p>
      {canWork && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void (async () => {
              setBusy(true);
              setError(null);
              try {
                const res = await client.checkEligibility({
                  patient: eligPatient.trim(),
                  payer: eligPayer || payerCodes[0] || "",
                  service_code: eligService.trim() || undefined,
                  service_date: eligDate || undefined,
                });
                setEligResult(res);
              } catch (err) {
                setError(`Eligibility check failed: ${accessError(err)}`);
              } finally {
                setBusy(false);
              }
            })();
          }}
        >
          <label>
            Patient id{" "}
            <input aria-label="Eligibility patient id" value={eligPatient}
              onChange={(e) => setEligPatient(e.target.value)} placeholder="pat-1" />
          </label>
          <label>
            Payer{" "}
            <select aria-label="Eligibility payer" value={eligPayer}
              onChange={(e) => setEligPayer(e.target.value)}>
              <option value="">—</option>
              {payerCodes.map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </label>
          <label>
            Service code{" "}
            <input aria-label="Eligibility service code" value={eligService}
              onChange={(e) => setEligService(e.target.value)} placeholder="imaging" />
          </label>
          <label>
            Service date{" "}
            <input aria-label="Eligibility service date" type="date" value={eligDate}
              onChange={(e) => setEligDate(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !eligPatient.trim()}>
            Check eligibility
          </button>
        </form>
      )}
      {eligResult && (
        <p className="state state-empty" role="status">
          {eligResult.covered ? "Covered" : "Not covered"} —{" "}
          {eligResult.payer_display ?? eligResult.payer}
          {eligResult.copay != null ? ` · copay USD ${eligResult.copay.toFixed(2)}` : ""}
          {eligResult.coinsurance_percent != null
            ? ` · coinsurance ${eligResult.coinsurance_percent}%`
            : ""}{" "}
          · basis: {eligResult.basis}. {eligResult.disclaimer}
        </p>
      )}

      <h3>Waitlist</h3>
      <p className="hint">
        Patients queue for a slot type (<code>terminology/slot-types</code>);
        the queue runs in priority order. When an appointment is cancelled,
        offer its slot here — accepting an offer books a real appointment.
      </p>
      {canWork && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void act("Added to waitlist", () =>
              client.joinWaitlist({
                patient: wlPatient.trim(),
                slot_type: wlSlot,
                priority: wlPriority ? Number(wlPriority) : undefined,
              }));
          }}
        >
          <label>
            Patient id{" "}
            <input aria-label="Waitlist patient id" value={wlPatient}
              onChange={(e) => setWlPatient(e.target.value)} placeholder="pat-1" />
          </label>
          <label>
            Slot type{" "}
            <select aria-label="Waitlist slot type" value={wlSlot}
              onChange={(e) => setWlSlot(e.target.value)}>
              {SLOT_TYPES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>
            Priority{" "}
            <input aria-label="Waitlist priority" value={wlPriority} size={4}
              onChange={(e) => setWlPriority(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !wlPatient.trim()}>
            Join waitlist
          </button>
        </form>
      )}
      {canWork && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void act("Slot offered", async () => {
              const res = await client.offerWaitlistSlot(offerAppt.trim());
              if (!res.entry) {
                throw new Error(res.reason ?? "No waiting entry matched");
              }
            });
          }}
        >
          <label>
            Cancelled appointment id{" "}
            <input aria-label="Cancelled appointment id" value={offerAppt}
              onChange={(e) => setOfferAppt(e.target.value)} />
          </label>
          <button type="submit" disabled={busy || !offerAppt.trim()}>
            Offer freed slot
          </button>
        </form>
      )}
      {entries.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Patient</th>
              <th>Slot type</th>
              <th>Priority</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {entries.map((entry) => (
              <tr key={entry.id}>
                <td>{entry.patient.replace("Patient/", "")}</td>
                <td>{entry.slot_type}</td>
                <td>{entry.priority}</td>
                <td><span className="badge">{entry.status}</span></td>
                <td>
                  {canWork && entry.status === "offered" && (
                    <span>
                      <input
                        aria-label={`Accept start for ${entry.id}`}
                        type="datetime-local"
                        value={acceptStart[entry.id] ?? ""}
                        onChange={(e) =>
                          setAcceptStart((s) => ({ ...s, [entry.id]: e.target.value }))
                        }
                      />
                      <button
                        type="button"
                        disabled={busy || !(acceptStart[entry.id] ?? "")}
                        onClick={() =>
                          void act("Offer accepted — appointment booked", () =>
                            client.acceptWaitlistOffer(entry.id, {
                              start: new Date(acceptStart[entry.id]).toISOString(),
                            }))
                        }
                      >
                        Accept & book
                      </button>
                    </span>
                  )}
                  {canWork && (entry.status === "waiting" || entry.status === "offered") && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void act("Waitlist entry cancelled", () =>
                          client.transitionWaitlist(entry.id, "cancelled"))
                      }
                    >
                      Cancel entry
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
