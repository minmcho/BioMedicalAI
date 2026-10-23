import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import type { Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { FrontDeskSteps } from "../components/PatientForms";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
  /** Front-desk flow (register → book): a patient id handed over by
   *  the registration step pre-fills the booking form. */
  initialPatient?: string;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function today(): string {
  return new Date().toISOString().slice(0, 10);
}

function arrivalTone(status: string): StatusTone {
  if (status === "arrived") return "ok";
  if (status === "booked") return "info";
  return "neutral";
}

function referralStageTone(stage: string): StatusTone {
  if (stage === "awaiting-report") return "warn";
  if (stage === "closed" || stage === "completed") return "ok";
  return "info";
}

type ClinicTab = "sessions" | "arrivals" | "continuum" | "inbox";

/** Phase 12 clinic day view: session templates + overbooking rules
 *  from published config, the rooming queue, deterministic no-show
 *  flags from each patient's own visit history (no model — the basis
 *  string travels with the data), the referral loop-closure worklist,
 *  panel care gaps and the SLA inbox. UX pass: the day's work is
 *  tabbed (P-7) — Sessions & booking / Arrivals & rooming /
 *  Referrals & care gaps / Async inbox. The landing tab is the
 *  caller's queue: a registration handoff (initialPatient) lands on
 *  Sessions & booking; everyone else lands on Arrivals & rooming,
 *  the front desk's check-in queue (F-1). */
export function ClinicDayPage({ client, role, initialPatient }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["continuum.work"]?.[role] === true;
  // Booking is registration-desk work too: the server accepts
  // continuum.work OR registration.work for POST /clinic/book, and
  // the form follows the same gate (rooming stays continuum.work).
  // Check-in (UX audit F-1) follows the identical rule server-side
  // (POST /clinic/appointments/{id}/check-in), so the Arrivals
  // section's Check in buttons use this same flag.
  const canBook =
    canWork || permissions["registration.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [date, setDate] = useState(today());
  const [bookPatient, setBookPatient] = useState("");
  const [bookSession, setBookSession] = useState("am-clinic");
  const [bookError, setBookError] = useState<string | null>(null);
  const [tab, setTab] = useState<ClinicTab>(
    initialPatient ? "sessions" : "arrivals",
  );
  // Register → book in one flow: a patient handed over from the
  // registration step lands in the booking form, ready to book.
  useEffect(() => {
    if (initialPatient) {
      setBookPatient(initialPatient);
      setTab("sessions");
    }
  }, [initialPatient]);
  const [rooms, setRooms] = useState<Record<string, string>>({});

  const dayState = useAsync(() => client.clinicDay(date), [client, date, tick]);
  const risksState = useAsync(() => client.noShowRisks(date), [client, date, tick]);
  const referralsState = useAsync(() => client.referralLoop(), [client, tick]);
  const gapsState = useAsync(() => client.careGaps(), [client, tick]);
  const inboxState = useAsync(() => client.continuumInbox(), [client, tick]);
  const day = dayState.data;
  const riskByAppointment = new Map(
    (risksState.data ?? []).map((r) => [r.appointment_id, r]),
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

  const sessionsPanel = day ? (
    <>
      <h3>Sessions</h3>
      <ul className="board-list">
        {day.sessions.map((s) => (
          <li key={s.code} className="board-card">
            <strong>{s.display}</strong>
            <span className="badge">starts {s.start_time}</span>
            <span className="badge">{s.booked}/{s.capacity} booked</span>
            {s.overbooked > 0 && (
              <StatusBadge tone="warn">{s.overbooked} overbooked</StatusBadge>
            )}
          </li>
        ))}
      </ul>
      {canBook && (
        <form
          id="clinic-booking"
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!bookPatient.trim()) {
              setBookError("Patient id is required to book an appointment.");
              return;
            }
            setBookError(null);
            void perform("Appointment booked", async () => {
              await client.clinicBook({
                patient: bookPatient.trim(), session: bookSession, date,
              });
              setBookPatient("");
            });
          }}
        >
          <h4>Book into a session</h4>
          <FrontDeskSteps current="book" />
          <label>
            Patient id{" "}
            <input aria-label="Booking patient id" value={bookPatient}
              onChange={(e) => {
                setBookPatient(e.target.value);
                if (bookError) setBookError(null);
              }} placeholder="pat-1" />
            {bookError && (
              <span className="field-error" role="alert">{bookError}</span>
            )}
          </label>
          <label>
            Session{" "}
            <select aria-label="Booking session" value={bookSession}
              disabled={day.sessions.length === 0}
              onChange={(e) => setBookSession(e.target.value)}>
              {day.sessions.map((s) => (
                <option key={s.code} value={s.code}>{s.display}</option>
              ))}
            </select>
            {day.sessions.length === 0 && (
              <span className="field-error">
                No clinic sessions in the published configuration.
              </span>
            )}
          </label>
          <button type="submit" disabled={busy || day.sessions.length === 0}>Book</button>
        </form>
      )}
    </>
  ) : null;

  const arrivalsPanel = day ? (
    <>
      <h3>Arrivals — awaiting check-in ({day.arrivals.length})</h3>
      <FrontDeskSteps current="checkin" />
      {day.arrivals.length === 0 && (
        <Empty label="No booked patients are waiting to check in." />
      )}
      <ul className="board-list">
        {day.arrivals.map((entry) => (
          <li key={entry.appointment_id} className="board-card">
            <div>
              <strong>{refText(entry.patient_ref)}</strong>
              <StatusBadge tone={arrivalTone(entry.status)}>{entry.status}</StatusBadge>
              {entry.session && <span className="badge">{entry.session}</span>}
              {entry.start && (
                <span className="badge">
                  {new Date(entry.start).toLocaleTimeString([], {
                    hour: "2-digit", minute: "2-digit",
                  })}
                </span>
              )}
            </div>
            {canBook && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Patient checked in", () =>
                      client.clinicCheckIn(entry.appointment_id))}
                >
                  Check in
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      <h3>Rooming queue ({day.rooming_queue.length})</h3>
      {day.rooming_queue.length === 0 && <Empty label="Nobody is waiting to be roomed." />}
      <ul className="board-list">
        {day.rooming_queue.map((entry) => {
          const risk = riskByAppointment.get(entry.appointment_id);
          return (
            <li key={entry.appointment_id} className="board-card">
              <div>
                <strong>{refText(entry.patient_ref)}</strong>
                {entry.room
                  ? <StatusBadge tone="ok">room {entry.room}</StatusBadge>
                  : <StatusBadge tone="warn">not roomed</StatusBadge>}
                {risk?.high_risk && <StatusBadge tone="warn">NO-SHOW RISK</StatusBadge>}
              </div>
              {risk && (
                <div className="hint">
                  History: {risk.history_missed} missed of {risk.history_total} — {risk.basis}
                </div>
              )}
              {canWork && !entry.room && (
                <div className="row-actions">
                  <input
                    aria-label={`Room for ${entry.appointment_id}`}
                    placeholder="Room"
                    value={rooms[entry.appointment_id] ?? ""}
                    onChange={(e) =>
                      setRooms((prev) => ({ ...prev, [entry.appointment_id]: e.target.value }))}
                  />
                  <button
                    type="button"
                    disabled={busy || !(rooms[entry.appointment_id] ?? "").trim()}
                    onClick={() =>
                      perform("Patient roomed", () =>
                        client.roomAppointment(
                          entry.appointment_id, rooms[entry.appointment_id].trim()))}
                  >
                    Room patient
                  </button>
                </div>
              )}
            </li>
          );
        })}
      </ul>
    </>
  ) : null;

  const continuumPanel = (
    <>
      <h3>Referral loop closure</h3>
      {referralsState.data && referralsState.data.length === 0 && (
        <Empty label="No referrals tracked." />
      )}
      <ul className="board-list">
        {referralsState.data?.map((r) => (
          <li key={r.id} className="board-card">
            <div>
              <strong>{refText(r.patient_ref)}</strong>
              <span className="badge">{r.service_code ?? "referral"}</span>
              <StatusBadge tone={referralStageTone(r.stage)}>{r.stage}</StatusBadge>
              <span className="badge">{r.age_days} days</span>
              {r.stale && <StatusBadge tone="warn">STALE</StatusBadge>}
            </div>
            {canWork && r.stage === "awaiting-report" && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Report-back recorded", () =>
                      client.referralReportBack(r.id, "Report received and filed"))}
                >
                  Record report-back
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>

      <h3>Panel care gaps</h3>
      {gapsState.data && gapsState.data.length === 0 && (
        <Empty label="No open care gaps on the configured panels." />
      )}
      <ul className="board-list">
        {gapsState.data?.map((gap) => (
          <li key={`${gap.gap_code}-${gap.patient_ref}`} className="board-card">
            <div>
              <strong>{refText(gap.patient_ref)}</strong>
              <span className="badge">{gap.display}</span>
              <span className="badge">{gap.reason}</span>
              {gap.outreach_task_id && (
                <StatusBadge tone="info">outreach open</StatusBadge>
              )}
            </div>
            {canWork && !gap.outreach_task_id && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Outreach task created", () =>
                      client.careGapOutreach(
                        gap.gap_code, gap.patient_ref.replace("Patient/", "")))}
                >
                  Create outreach task
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );

  const inboxPanel = (
    <>
      <h3>Async inbox ({inboxState.data?.total ?? 0}, {inboxState.data?.breached ?? 0} breached)</h3>
      {inboxState.data && inboxState.data.total === 0 && (
        <Empty label="The inbox is clear." />
      )}
      <ul className="board-list">
        {inboxState.data?.items.map((item) => (
          <li key={`${item.type}-${item.id}`} className="board-card">
            <div>
              <strong>{item.title}</strong>
              <span className="badge">{item.type}</span>
              <span className="badge">{item.age_minutes} min old</span>
              {item.breached && <StatusBadge tone="bad">SLA BREACHED</StatusBadge>}
            </div>
            {canWork && item.type === "task" && item.task_id && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Task completed", () =>
                      client.transitionContinuumTask(item.task_id as string, "completed"))}
                >
                  Complete task
                </button>
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
        icon="stethoscope"
        group="Operations"
        title="Clinic day"
        purpose="Sessions, slot lengths, capacities and overbooking limits are published Studio config. No-show flags are deterministic counts over each patient's own appointment history — labelled, never a prediction model."
        primaryAction={
          canBook
            ? {
                label: "Book an appointment",
                onClick: () => setTab("sessions"),
              }
            : undefined
        }
      />
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      <label>
        Date{" "}
        <input
          aria-label="Clinic date"
          type="date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </label>

      {dayState.loading && <Loading label="Loading clinic day…" />}
      {dayState.error != null && <ErrorView error={dayState.error} />}
      <Tabs
        ariaLabel="Clinic day"
        tabs={[
          { key: "sessions", label: "Sessions & booking" },
          { key: "arrivals", label: "Arrivals & rooming" },
          { key: "continuum", label: "Referrals & care gaps" },
          { key: "inbox", label: "Async inbox" },
        ]}
        activeKey={tab}
        onChange={(k) => setTab(k as ClinicTab)}
        panels={{
          sessions: sessionsPanel,
          arrivals: arrivalsPanel,
          continuum: continuumPanel,
          inbox: inboxPanel,
        }}
      />
    </section>
  );
}
