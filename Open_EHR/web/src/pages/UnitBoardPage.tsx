import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import { flattenPlacements, getOrgTree } from "../api/org";
import type { QuestionnaireAnswerItem, ReadinessItem, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const IPASS_SECTIONS = [
  "illness-severity", "patient-summary", "action-list",
  "situation-awareness", "synthesis",
];



/** Depth pass (Workstream 2): the interdisciplinary rounds checklist
 *  for one patient — a compact inline form over the governed
 *  rounds-checklist form's items (the server enforces the required
 *  ones through the form machinery; errors surface inline here). */
function RoundsChecklistForm({
  encounterId,
  busy,
  onSubmit,
}: {
  encounterId: string;
  busy: boolean;
  onSubmit: (answers: QuestionnaireAnswerItem[]) => Promise<void>;
}) {
  const [overnight, setOvernight] = useState("");
  const [lines, setLines] = useState(false);
  const [vte, setVte] = useState(false);
  const [glucose, setGlucose] = useState("");
  const [pain, setPain] = useState("");
  const [barriers, setBarriers] = useState("");
  const [family, setFamily] = useState(false);
  const [error, setError] = useState<string | null>(null);

  return (
    <form
      noValidate
      className="inline-form"
      aria-label={`Rounds checklist for ${encounterId}`}
      onSubmit={(e) => {
        e.preventDefault();
        setError(null);
        if (!overnight.trim()) {
          setError("Overnight events is required.");
          return;
        }
        const answers: QuestionnaireAnswerItem[] = [
          { linkId: "overnight-events", answer: [{ valueString: overnight.trim() }] },
          { linkId: "lines-devices-reviewed", answer: [{ valueBoolean: lines }] },
          { linkId: "vte-prophylaxis", answer: [{ valueBoolean: vte }] },
        ];
        if (glucose.trim()) {
          answers.push({ linkId: "glucose-control", answer: [{ valueString: glucose.trim() }] });
        }
        if (pain.trim()) {
          answers.push({ linkId: "pain-control", answer: [{ valueString: pain.trim() }] });
        }
        if (barriers.trim()) {
          answers.push({ linkId: "discharge-barriers", answer: [{ valueString: barriers.trim() }] });
        }
        answers.push({ linkId: "family-updated", answer: [{ valueBoolean: family }] });
        void onSubmit(answers).catch((err: unknown) => setError(errorText(err)));
      }}
    >
      {error && <div className="state state-error" role="alert">{error}</div>}
      <label>
        Overnight events *{" "}
        <input
          aria-label={`Overnight events for ${encounterId}`}
          value={overnight}
          onChange={(e) => setOvernight(e.target.value)}
        />
      </label>
      <label>
        <input
          type="checkbox"
          aria-label={`Lines and devices reviewed for ${encounterId}`}
          checked={lines}
          onChange={(e) => setLines(e.target.checked)}
        />{" "}
        Lines and devices reviewed *
      </label>
      <label>
        <input
          type="checkbox"
          aria-label={`VTE prophylaxis for ${encounterId}`}
          checked={vte}
          onChange={(e) => setVte(e.target.checked)}
        />{" "}
        VTE prophylaxis reviewed *
      </label>
      <label>
        Glucose control{" "}
        <input
          aria-label={`Glucose control for ${encounterId}`}
          value={glucose}
          onChange={(e) => setGlucose(e.target.value)}
        />
      </label>
      <label>
        Pain control{" "}
        <input
          aria-label={`Pain control for ${encounterId}`}
          value={pain}
          onChange={(e) => setPain(e.target.value)}
        />
      </label>
      <label>
        Discharge barriers{" "}
        <input
          aria-label={`Discharge barriers for ${encounterId}`}
          value={barriers}
          onChange={(e) => setBarriers(e.target.value)}
        />
      </label>
      <label>
        <input
          type="checkbox"
          aria-label={`Family updated for ${encounterId}`}
          checked={family}
          onChange={(e) => setFamily(e.target.checked)}
        />{" "}
        Family updated
      </label>
      <button type="submit" disabled={busy}>Submit rounds checklist</button>
    </form>
  );
}

/** Phase 12 inpatient unit board: acuity-based nurse assignments with
 *  load limits, the discharge-readiness board (the data behind the
 *  published discharge-readiness dashboard config), I-PASS handoff
 *  capture and EVS turnaround tasks. */
export function UnitBoardPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["continuum.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [nurseNames, setNurseNames] = useState<Record<string, string>>({});
  const [acuities, setAcuities] = useState<Record<string, string>>({});
  const [handoffText, setHandoffText] = useState<Record<string, string>>({});
  const [roundsUnit, setRoundsUnit] = useState("unit-medsurg");
  const [openRoundsForm, setOpenRoundsForm] = useState<string | null>(null);
  // UX pass (spec P-7): assignments / rounds / readiness / EVS are
  // tabs, not one stacked scroll.
  const [tab, setTab] = useState("assignments");

  const assignmentsState = useAsync(() => client.nurseAssignments(), [client, tick]);
  const readinessState = useAsync(() => client.dischargeReadiness(), [client, tick]);
  const evsState = useAsync(() => client.evsTasks(), [client, tick]);
  // UX pass: the rounds unit options come from the published org
  // hierarchy (real data), not a hard-coded pair. Until the tree
  // loads — or if it cannot be read — the select keeps the current
  // unit as its only option and says so, rather than inventing a
  // list.
  const orgState = useAsync(() => getOrgTree(client), [client]);
  const roundUnits = flattenPlacements(orgState.data ?? null).map((fp) => fp.unit);
  const effectiveRoundsUnit = roundUnits.some((u) => u.code === roundsUnit)
    ? roundsUnit
    : (roundUnits[0]?.code ?? roundsUnit);
  // Depth pass (Workstream 2): the rounds list for the chosen unit.
  const roundsState = useAsync(
    () => (typeof client.inpatientRounds === "function"
      ? client.inpatientRounds(effectiveRoundsUnit)
      : Promise.resolve(null)),
    [client, tick, effectiveRoundsUnit]);
  const assignments = assignmentsState.data;
  const readiness = readinessState.data;

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

  const assignmentsPanel = (
    <>
      <h3>Nurse assignments</h3>
      {assignmentsState.loading && <Loading label="Loading assignments…" />}
      {assignmentsState.error != null && <ErrorView error={assignmentsState.error} />}
      {assignments && (
        <>
          <ul className="board-list">
            {assignments.nurses.map((n) => (
              <li key={n.nurse} className="board-card">
                <strong>{n.nurse}</strong>
                <span className="badge">{n.patients} patients</span>
                <span className="badge">acuity load {n.acuity_load}</span>
                {n.overloaded && <StatusBadge tone="bad">OVER LOAD LIMIT</StatusBadge>}
              </li>
            ))}
          </ul>
          {assignments.unassigned.length > 0 && (
            <>
              <h4>Unassigned ({assignments.unassigned.length})</h4>
              <ul className="board-list">
                {assignments.unassigned.map((u) => (
                  <li key={u.encounter_id} className="board-card">
                    <div>
                      <strong>{refText(u.patient_ref)}</strong>
                      <span className="badge">{u.bed ?? "no bed"}</span>
                    </div>
                    {canWork && (
                      <div className="row-actions">
                        <input
                          aria-label={`Nurse for ${u.encounter_id}`}
                          placeholder="Nurse username"
                          value={nurseNames[u.encounter_id] ?? ""}
                          onChange={(e) =>
                            setNurseNames((prev) => ({ ...prev, [u.encounter_id]: e.target.value }))}
                        />
                        <select
                          aria-label={`Acuity for ${u.encounter_id}`}
                          value={acuities[u.encounter_id] ?? "3"}
                          onChange={(e) =>
                            setAcuities((prev) => ({ ...prev, [u.encounter_id]: e.target.value }))}
                        >
                          {[1, 2, 3, 4, 5].map((n) => (
                            <option key={n} value={n}>{n}</option>
                          ))}
                        </select>
                        <button
                          type="button"
                          disabled={busy || !(nurseNames[u.encounter_id] ?? "").trim()}
                          onClick={() =>
                            perform("Nurse assigned", () =>
                              client.assignNurse({
                                encounterId: u.encounter_id,
                                nurse: nurseNames[u.encounter_id].trim(),
                                acuity: Number(acuities[u.encounter_id] ?? "3"),
                              }))}
                        >
                          Assign
                        </button>
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            </>
          )}
        </>
      )}
    </>
  );

  const roundsPanel = (
    <>
      <h3>Rounds</h3>
      <label>
        Unit{" "}
        <select
          aria-label="Rounds unit"
          value={effectiveRoundsUnit}
          onChange={(e) => setRoundsUnit(e.target.value)}
        >
          {roundUnits.length > 0 ? (
            roundUnits.map((u) => (
              <option key={u.code} value={u.code}>{u.display} ({u.code})</option>
            ))
          ) : (
            <option value={roundsUnit}>{roundsUnit}</option>
          )}
        </select>
      </label>
      {roundUnits.length === 0 && (
        <p className="hint">
          Unit options come from the published org hierarchy; it has not loaded, so only the
          current unit is shown.
        </p>
      )}
      {roundsState.loading && <Loading label="Loading rounds…" />}
      {roundsState.error != null && <ErrorView error={roundsState.error} />}
      {roundsState.data && roundsState.data.total === 0 && (
        <Empty label="No patients on this unit." />
      )}
      <ul className="board-list">
        {roundsState.data?.items.map((item) => (
          <li key={item.encounter_id} className="board-card">
            <div>
              <strong>{refText(item.patient_ref)}</strong>
              <span className="badge">{item.bed ?? "no bed"}</span>
              <StatusBadge tone={item.rounds_checklist_today ? "ok" : "warn"}>
                Rounds today: {item.rounds_checklist_today ? "Done" : "Pending"}
              </StatusBadge>
            </div>
            {item.nurse && (
              <div className="hint">Nurse {item.nurse} · acuity {item.acuity ?? "—"}</div>
            )}
            {canWork && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    setOpenRoundsForm(
                      openRoundsForm === item.encounter_id ? null : item.encounter_id)}
                >
                  Rounds checklist
                </button>
              </div>
            )}
            {canWork && openRoundsForm === item.encounter_id && (
              <RoundsChecklistForm
                encounterId={item.encounter_id}
                busy={busy}
                onSubmit={async (answers) => {
                  await perform("Rounds checklist recorded", () =>
                    client.submitRoundsChecklist(item.encounter_id, answers));
                  setOpenRoundsForm(null);
                }}
              />
            )}
          </li>
        ))}
      </ul>
    </>
  );

  const readinessPanel = (
    <>
      <h3>Discharge readiness ({readiness?.ready ?? 0}/{readiness?.total ?? 0} ready)</h3>
      {readinessState.error != null && <ErrorView error={readinessState.error} />}
      {readiness && readiness.total === 0 && (
        <Empty label="No active admissions." />
      )}
      <ul className="board-list">
        {readiness?.items.map((item) => (
          <li key={item.encounter_id} className="board-card">
            <div>
              <strong>{refText(item.patient_ref)}</strong>
              <span className="badge">{item.bed ?? "no bed"}</span>
              {item.ready
                ? <StatusBadge tone="ok">READY</StatusBadge>
                : <StatusBadge tone="warn">missing: {item.missing.join(", ")}</StatusBadge>}
              {/* Depth pass (Workstream 2): medication reconciliation —
                  the readiness board's fourth element. UX pass: the
                  chip is a toned StatusBadge, not an inline-style span
                  (colour alone is not a status — spec P-3). */}
              <StatusBadge
                tone={(item as ReadinessItem & { medrec_done?: boolean })
                  .medrec_done ? "ok" : "warn"}
              >
                Med-rec: {(item as ReadinessItem & { medrec_done?: boolean })
                  .medrec_done ? "Done" : "Pending"}
              </StatusBadge>
            </div>
            <div className="hint">
              Target {item.target_date ?? "—"} · disposition {item.disposition ?? "—"}
              {item.barriers ? ` · barriers: ${item.barriers}` : ""}
            </div>
            {canWork && (
              <div className="row-actions">
                <input
                  aria-label={`Handoff note for ${item.encounter_id}`}
                  placeholder="I-PASS summary (all sections in one note for this quick capture)"
                  value={handoffText[item.encounter_id] ?? ""}
                  onChange={(e) =>
                    setHandoffText((prev) => ({ ...prev, [item.encounter_id]: e.target.value }))}
                />
                <button
                  type="button"
                  disabled={busy || !(handoffText[item.encounter_id] ?? "").trim()}
                  onClick={() =>
                    perform("Handoff recorded", () =>
                      client.recordHandoff(
                        item.encounter_id,
                        IPASS_SECTIONS.map((linkId) => ({
                          linkId,
                          answer: [{ valueString: handoffText[item.encounter_id].trim() }],
                        })),
                      ))}
                >
                  Record I-PASS handoff
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );

  const evsPanel = (
    <>
      <h3>EVS turnaround</h3>
      {evsState.data && evsState.data.length === 0 && (
        <Empty label="No EVS tasks." />
      )}
      <ul className="board-list">
        {evsState.data?.map((task) => (
          <li key={task.id} className="board-card">
            <div>
              <strong>Bed {task.bed_code}</strong>
              <StatusBadge tone={task.status === "completed" ? "ok" : "info"}>{task.status}</StatusBadge>
              <span className="badge">waiting {task.waiting_minutes} min</span>
              {task.turnaround_minutes != null && (
                <span className="badge">turnaround {task.turnaround_minutes} min</span>
              )}
            </div>
            {canWork && task.status !== "completed" && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("EVS task completed", () => client.completeEvsTask(task.id))}
                >
                  Complete clean
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
        icon="board"
        group="Operations"
        title="Unit board"
        purpose="One unit's work: nurse assignments, rounds, discharge readiness and EVS turnaround."
      />
      <p className="hint">
        Nurse assignments are acuity-weighted; the per-nurse load limit
        and the readiness checklist (discharge plan, teach-back,
        follow-up booked) come from published config and live module
        data. EVS tasks are created automatically when a bed is
        released for cleaning.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      <Tabs
        tabs={[
          { key: "assignments", label: "Nurse assignments" },
          { key: "rounds", label: "Rounds" },
          { key: "readiness", label: "Discharge readiness" },
          { key: "evs", label: "EVS turnaround" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Unit board views"
        panels={{
          assignments: assignmentsPanel,
          rounds: roundsPanel,
          readiness: readinessPanel,
          evs: evsPanel,
        }}
      />
    </section>
  );
}
