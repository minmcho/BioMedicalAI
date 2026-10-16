import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type { Role, SurgeryCaseEntry } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

// Next states offered per case state — mirrors the published Studio
// workflow config (workflow/surgery-flow). The server enforces the
// config; a removed transition surfaces as a server error (409).
const NEXT_STATES: Record<string, string[]> = {
  "scheduled": ["pre-op", "cancelled"],
  "pre-op": ["in-op", "cancelled"],
  "in-op": ["pacu"],
  "pacu": ["done"],
};

const TERMINAL = new Set(["done", "cancelled"]);

export function SurgeryBoardPage({ client, role }: Props) {
  const canWork = role === "admin" || role === "configurator" || role === "clinician";
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [date, setDate] = useState(todayIso());
  const [moves, setMoves] = useState<Record<string, { start: string; end: string }>>({});
  const [form, setForm] = useState({
    patientId: "", code: "", display: "", room: "OR-1", start: "", end: "",
  });

  const boardState = useAsync(() => client.surgeryBoard(date), [client, date, tick]);
  const roomsState = useAsync(() => client.surgeryRooms(), [client]);
  const board = boardState.data;
  const rooms = (roomsState.data ?? []).filter((r) => r.active !== false);

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

  function renderCase(entry: SurgeryCaseEntry) {
    const caseId = entry.case.id as string;
    const state = entry.state ?? entry.case.status ?? "";
    const nextStates = NEXT_STATES[state] ?? [];
    const move = moves[caseId] ?? { start: "", end: "" };
    return (
      <li key={caseId} className="board-card">
        <div>
          <strong>{codeLabel(entry.case.code)}</strong>{" "}
          <span className="badge">{state}</span>
        </div>
        <div className="hint">
          {entry.start ? new Date(entry.start).toLocaleTimeString() : "—"} –{" "}
          {entry.end ? new Date(entry.end).toLocaleTimeString() : "—"} · Patient{" "}
          {refLabel(entry.case.subject)}
        </div>
        {canWork && nextStates.length > 0 && (
          <div className="row-actions">
            {nextStates.map((target) => (
              <button
                key={target}
                type="button"
                disabled={busy}
                onClick={() =>
                  perform(`Case → ${target}`, () =>
                    client.transitionCase(caseId, target))}
              >
                {target}
              </button>
            ))}
          </div>
        )}
        {canWork && !TERMINAL.has(state) && (
          <div className="row-actions">
            <label>
              New start{" "}
              <input
                type="datetime-local"
                aria-label={`New start for ${caseId}`}
                value={move.start}
                onChange={(e) =>
                  setMoves((prev) => ({
                    ...prev,
                    [caseId]: { ...move, start: e.target.value },
                  }))}
              />
            </label>
            <label>
              New end{" "}
              <input
                type="datetime-local"
                aria-label={`New end for ${caseId}`}
                value={move.end}
                onChange={(e) =>
                  setMoves((prev) => ({
                    ...prev,
                    [caseId]: { ...move, end: e.target.value },
                  }))}
              />
            </label>
            <button
              type="button"
              disabled={busy || !move.start || !move.end}
              onClick={() =>
                perform("Case rescheduled", () =>
                  client.rescheduleCase(
                    caseId,
                    new Date(move.start).toISOString(),
                    new Date(move.end).toISOString(),
                  ))}
            >
              Reschedule
            </button>
          </div>
        )}
      </li>
    );
  }

  return (
    <section>
      <h2>Surgery board</h2>
      <p className="hint">
        The day's operating-room schedule, grouped by room. Rooms and the
        case workflow (scheduled → pre-op → in-op → pacu → done) are
        published Studio configuration (terminology/or-rooms,
        workflow/surgery-flow). Double-booking a room with overlapping
        times is refused by the server on both scheduling and
        rescheduling — conflicts surface below as errors, never as a
        silent overwrite.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      <label>
        Date{" "}
        <input
          type="date"
          aria-label="Board date"
          value={date}
          onChange={(e) => setDate(e.target.value)}
        />
      </label>

      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!form.patientId.trim() || !form.code.trim() || !form.start || !form.end) return;
            void perform("Case scheduled", async () => {
              await client.scheduleCase({
                patientId: form.patientId.trim(),
                code: form.code.trim(),
                display: form.display.trim() || undefined,
                room: form.room,
                start: new Date(form.start).toISOString(),
                end: new Date(form.end).toISOString(),
              });
              setForm({ patientId: "", code: "", display: "", room: form.room, start: "", end: "" });
            });
          }}
        >
          <h3>Schedule a case</h3>
          <label>
            Patient id{" "}
            <input
              aria-label="Case patient id"
              value={form.patientId}
              onChange={(e) => setForm({ ...form, patientId: e.target.value })}
              placeholder="pat-1"
            />
          </label>
          <label>
            Procedure code{" "}
            <input
              aria-label="Procedure code"
              value={form.code}
              onChange={(e) => setForm({ ...form, code: e.target.value })}
              placeholder="27447"
            />
          </label>
          <label>
            Description{" "}
            <input
              aria-label="Procedure description"
              value={form.display}
              onChange={(e) => setForm({ ...form, display: e.target.value })}
              placeholder="Total knee arthroplasty"
            />
          </label>
          <label>
            Room{" "}
            <select
              aria-label="Room"
              value={form.room}
              onChange={(e) => setForm({ ...form, room: e.target.value })}
            >
              {rooms.map((r) => (
                <option key={r.code} value={r.code}>
                  {r.code} — {r.display ?? r.code}
                </option>
              ))}
            </select>
          </label>
          <label>
            Start{" "}
            <input
              type="datetime-local"
              aria-label="Case start"
              value={form.start}
              onChange={(e) => setForm({ ...form, start: e.target.value })}
            />
          </label>
          <label>
            End{" "}
            <input
              type="datetime-local"
              aria-label="Case end"
              value={form.end}
              onChange={(e) => setForm({ ...form, end: e.target.value })}
            />
          </label>
          <button type="submit" disabled={busy}>Schedule case</button>
        </form>
      )}

      {boardState.loading && <Loading label="Loading surgery board…" />}
      {boardState.error != null && <ErrorView error={boardState.error} />}
      {board && board.total === 0 && (
        <Empty label="No cases scheduled for this date." />
      )}
      {board && board.rooms.map((room) => (
        <div key={room.code}>
          <h3>{room.code}{room.display ? ` — ${room.display}` : ""} ({room.cases.length})</h3>
          {room.cases.length === 0
            ? <p className="hint">No cases.</p>
            : <ul className="board-list">{room.cases.map(renderCase)}</ul>}
        </div>
      ))}
    </section>
  );
}
