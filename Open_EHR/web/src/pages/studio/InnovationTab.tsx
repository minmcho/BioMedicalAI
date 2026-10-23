import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import { RichTextEditor } from "../../components/RichTextEditor";
import { StatusBadge, type StatusTone } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { Stepper } from "../../components/Stepper";
import { useAsync } from "../../hooks";

// Innovation pipeline tab (Phase 16, WS-K1) — the digital-health
// innovation pipeline (backend/app/innovation.py, BD-43). A proposed
// capability (pack, agent or config) is triaged, approved for a
// pilot, exercised ONLY inside the pilot's scope (one unit and/or
// one cohort, inside its date window), evaluated from the pilot's
// audit stream, and decided: scale / kill / extend. A scale decision
// never enables anything here — it creates a hand-off record naming
// the governed Studio step a human must take.

export interface InnovationIntake {
  id: number;
  title: string;
  description: string;
  capability_type: "pack" | "agent" | "config";
  capability_key: string;
  proposer: string;
  status: string;
  triage_notes: string;
  triaged_by: string;
  created_at: string | null;
}

export interface InnovationPilot {
  id: number;
  intake_id: number;
  capability_type: string | null;
  capability_key: string | null;
  scope: { unit?: string; cohortKey?: string };
  starts_on: string;
  ends_on: string;
  status: string;
  outcome: Record<string, unknown> | null;
  outcome_by: string | null;
  created_by: string;
}

export interface PilotEvaluation {
  pilot: InnovationPilot;
  intake: { id: number; title: string; capability_type: string; capability_key: string; status: string };
  exercises: {
    total: number;
    succeeded: number;
    refused: number;
    refusals: Array<{ patient: string | null; reason: string | null; at: string | null }>;
    patients: string[];
  };
  runs: { total: number; run_ids: string[]; dispositions: Record<string, number> };
  outcome: Record<string, unknown> | null;
  decisions: Array<{ id: number; decision: string; rationale: string; decided_by: string }>;
  handoffs: Array<{ id: number; required_step: string; status: string; capability_type: string; capability_key: string }>;
}

const LIFECYCLE_STEPS = [
  "Proposed",
  "Triaged",
  "Pilot approved",
  "Piloting",
  "Evaluated",
  "Decided",
];

const STATUS_INDEX: Record<string, number> = {
  proposed: 0,
  triaged: 1,
  "pilot-approved": 2,
  piloting: 3,
  evaluated: 4,
  decided: 5,
};

export function lifecycleIndex(status: string): number {
  return STATUS_INDEX[status] ?? 0;
}

export function pilotStatusTone(status: string): StatusTone {
  switch (status) {
    case "piloting":
      return "info";
    case "evaluated":
      return "warn";
    case "decided":
      return "ok";
    default:
      return "neutral";
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

export function InnovationTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const intakes = useAsync<{ items: InnovationIntake[]; total: number }>(
    () => client.request("/innovation/intakes"),
    [client, reloadKey],
  );
  const pilots = useAsync<{ items: InnovationPilot[]; total: number }>(
    () => client.request("/innovation/pilots"),
    [client, reloadKey],
  );

  const [notice, setNotice] = useState<string | null>(null);

  // Intake create
  const [title, setTitle] = useState("");
  const [capType, setCapType] = useState<"pack" | "agent" | "config">("agent");
  const [capKey, setCapKey] = useState("");
  const [description, setDescription] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  // Selected intake + triage
  const [selectedIntakeId, setSelectedIntakeId] = useState<number | null>(null);
  const [triageNotes, setTriageNotes] = useState("");
  const [intakeError, setIntakeError] = useState<string | null>(null);

  // Pilot create
  const [pilotUnit, setPilotUnit] = useState("");
  const [pilotCohort, setPilotCohort] = useState("");
  const [pilotStarts, setPilotStarts] = useState("");
  const [pilotEnds, setPilotEnds] = useState("");
  const [pilotError, setPilotError] = useState<string | null>(null);

  // Selected pilot + exercise / evaluation / decision
  const [selectedPilotId, setSelectedPilotId] = useState<number | null>(null);
  const [exercisePatient, setExercisePatient] = useState("");
  const [exerciseInput, setExerciseInput] = useState("");
  const [exerciseResult, setExerciseResult] = useState<Record<string, unknown> | null>(null);
  const [exerciseError, setExerciseError] = useState<string | null>(null);
  const [evaluation, setEvaluation] = useState<PilotEvaluation | null>(null);
  const [evaluationError, setEvaluationError] = useState<string | null>(null);
  const [outcomeText, setOutcomeText] = useState(
    '{\n  "summary": "",\n  "metrics": {}\n}',
  );
  const [outcomeError, setOutcomeError] = useState<string | null>(null);
  const [decision, setDecision] = useState<"scale" | "kill" | "extend">("scale");
  const [rationale, setRationale] = useState("");
  const [newEndsOn, setNewEndsOn] = useState("");
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [decisionResult, setDecisionResult] = useState<string | null>(null);

  if (intakes.loading) return <Loading label="Loading innovation pipeline…" />;
  if (intakes.error) return <ErrorView error={intakes.error} />;

  const intakeList = intakes.data?.items ?? [];
  const pilotList = pilots.data?.items ?? [];
  const selectedIntake =
    intakeList.find((i) => i.id === selectedIntakeId) ?? null;
  const selectedPilot =
    pilotList.find((p) => p.id === selectedPilotId) ?? null;

  async function createIntake() {
    setCreateError(null);
    try {
      const created = await client.request<InnovationIntake>("/innovation/intakes", {
        method: "POST",
        body: JSON.stringify({
          title: title.trim(),
          capabilityType: capType,
          capabilityKey: capKey.trim(),
          description: description.trim(),
        }),
      });
      setNotice(`Intake #${created.id} proposed: ${created.title}.`);
      setSelectedIntakeId(created.id);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setCreateError(errText(err));
    }
  }

  async function triage() {
    if (!selectedIntake) return;
    setIntakeError(null);
    try {
      await client.request(`/innovation/intakes/${selectedIntake.id}/triage`, {
        method: "POST",
        body: JSON.stringify({ notes: triageNotes }),
      });
      setNotice(`Intake #${selectedIntake.id} triaged.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setIntakeError(errText(err));
    }
  }

  async function approvePilot() {
    if (!selectedIntake) return;
    setIntakeError(null);
    try {
      await client.request(`/innovation/intakes/${selectedIntake.id}/approve-pilot`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      setNotice(`Intake #${selectedIntake.id} approved for a pilot.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setIntakeError(errText(err));
    }
  }

  async function createPilot() {
    if (!selectedIntake) return;
    setPilotError(null);
    try {
      const created = await client.request<InnovationPilot>("/innovation/pilots", {
        method: "POST",
        body: JSON.stringify({
          intakeId: selectedIntake.id,
          scope: {
            ...(pilotUnit.trim() ? { unit: pilotUnit.trim() } : {}),
            ...(pilotCohort.trim() ? { cohortKey: pilotCohort.trim() } : {}),
          },
          startsOn: pilotStarts,
          endsOn: pilotEnds,
        }),
      });
      setNotice(
        `Pilot #${created.id} scoped — window ${created.starts_on} to ${created.ends_on}.`,
      );
      setSelectedPilotId(created.id);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setPilotError(errText(err));
    }
  }

  async function exercise() {
    if (!selectedPilot) return;
    setExerciseError(null);
    setExerciseResult(null);
    try {
      const out = await client.request<Record<string, unknown>>(
        `/innovation/pilots/${selectedPilot.id}/exercise`,
        {
          method: "POST",
          body: JSON.stringify({
            patientRef: exercisePatient.trim() || undefined,
            input: exerciseInput,
          }),
        },
      );
      setExerciseResult(out);
    } catch (err) {
      // Out-of-scope and outside-window refusals are audited 403s;
      // agent no-model/unreachable is the runtime's honest 503.
      setExerciseError(errText(err));
    }
  }

  async function loadEvaluation() {
    if (!selectedPilot) return;
    setEvaluationError(null);
    try {
      setEvaluation(
        await client.request<PilotEvaluation>(
          `/innovation/pilots/${selectedPilot.id}/evaluation`,
        ),
      );
    } catch (err) {
      setEvaluationError(errText(err));
    }
  }

  async function submitOutcome() {
    if (!selectedPilot) return;
    setOutcomeError(null);
    let outcome: unknown;
    try {
      outcome = JSON.parse(outcomeText);
    } catch (err) {
      setOutcomeError(`Outcome is not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    try {
      const out = await client.request<PilotEvaluation>(
        `/innovation/pilots/${selectedPilot.id}/evaluation`,
        { method: "POST", body: JSON.stringify(outcome) },
      );
      setEvaluation(out);
      setNotice(`Pilot #${selectedPilot.id} evaluated.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setOutcomeError(errText(err));
    }
  }

  async function decide() {
    if (!selectedPilot) return;
    setDecisionError(null);
    setDecisionResult(null);
    try {
      const out = await client.request<{
        decision: string;
        pilot_status: string;
        handoff: { required_step: string; status: string } | null;
      }>(`/innovation/pilots/${selectedPilot.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          rationale: rationale.trim(),
          newEndsOn: decision === "extend" && newEndsOn ? newEndsOn : undefined,
        }),
      });
      setDecisionResult(
        `Decision recorded: ${out.decision} (pilot now ${out.pilot_status}).` +
          (out.handoff
            ? ` Scale hand-off created (${out.handoff.status}): a human must take this Studio step — ${out.handoff.required_step}`
            : ""),
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setDecisionError(errText(err));
    }
  }

  return (
    <div>
      <p className="hint">
        New capabilities earn deployment: propose → triage → approve a
        pilot → exercise inside the pilot scope only → evaluate from
        the audit stream → decide. Nothing in this pipeline enables a
        pack, agent or config globally; a <strong>scale</strong>{" "}
        decision only records the governed hand-off step a human takes
        in Studio.
      </p>
      {notice && <p className="hint" role="status">{notice}</p>}

      <h3>Intakes</h3>
      {intakeList.length === 0 ? (
        <Empty label="No innovation intakes yet — propose one below." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Intake</th>
              <th>Capability</th>
              <th>Proposer</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {intakeList.map((i) => (
              <tr key={i.id}>
                <td>
                  <strong>{i.title}</strong>
                  {i.description ? (
                    <>
                      <br />
                      <span className="hint">{i.description}</span>
                    </>
                  ) : null}
                </td>
                <td>
                  {i.capability_type}: <code>{i.capability_key}</code>
                </td>
                <td>{i.proposer}</td>
                <td>
                  <StatusBadge tone={pilotStatusTone(i.status)}>
                    {i.status}
                  </StatusBadge>
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedIntakeId(i.id);
                      setIntakeError(null);
                    }}
                  >
                    Manage
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Propose an intake</h4>
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void createIntake();
        }}
      >
        <p>
          <label>
            Title{" "}
            <input
              aria-label="Intake title"
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              size={40}
            />
          </label>{" "}
          <label>
            Capability type{" "}
            <select
              aria-label="Capability type"
              value={capType}
              onChange={(e) => setCapType(e.target.value as "pack" | "agent" | "config")}
            >
              <option value="agent">agent</option>
              <option value="pack">pack</option>
              <option value="config">config</option>
            </select>
          </label>{" "}
          <label>
            Capability key{" "}
            <input
              aria-label="Capability key"
              value={capKey}
              onChange={(e) => setCapKey(e.target.value)}
              size={28}
              placeholder="agent key, pack name, or kind/key for config"
            />
          </label>
        </p>
        <p>
          <label>
            Description{" "}
            <input
              aria-label="Intake description"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              size={64}
            />
          </label>
        </p>
        {createError && (
          <p className="error-note" role="alert">
            {createError}
          </p>
        )}
        <p>
          <button type="submit" disabled={!title.trim() || !capKey.trim()}>
            Propose intake
          </button>
        </p>
      </form>

      {selectedIntake && (
        <div>
          <h4>
            Intake #{selectedIntake.id} — {selectedIntake.title}{" "}
            <StatusBadge tone={pilotStatusTone(selectedIntake.status)}>
              {selectedIntake.status}
            </StatusBadge>
          </h4>
          <Stepper
            steps={LIFECYCLE_STEPS}
            current={lifecycleIndex(selectedIntake.status)}
          />
          {selectedIntake.triage_notes && (
            <p className="hint">
              Triage notes by {selectedIntake.triaged_by}:{" "}
              {selectedIntake.triage_notes}
            </p>
          )}
          {intakeError && (
            <p className="error-note" role="alert">
              {intakeError}
            </p>
          )}
          {selectedIntake.status === "proposed" && (
            <p>
              <label>
                Triage notes{" "}
                <input
                  aria-label="Triage notes"
                  value={triageNotes}
                  onChange={(e) => setTriageNotes(e.target.value)}
                  size={48}
                />
              </label>{" "}
              <button type="button" onClick={() => void triage()}>
                Triage
              </button>
            </p>
          )}
          {selectedIntake.status === "triaged" && (
            <p>
              <button type="button" onClick={() => void approvePilot()}>
                Approve pilot
              </button>{" "}
              <span className="hint">
                Approval authorises creating a pilot — it starts nothing
                and enables nothing.
              </span>
            </p>
          )}
          {selectedIntake.status === "pilot-approved" && (
            <div>
              <h5>Scope the pilot</h5>
              <p>
                <label>
                  Unit{" "}
                  <input
                    aria-label="Pilot unit"
                    value={pilotUnit}
                    onChange={(e) => setPilotUnit(e.target.value)}
                    placeholder="e.g. MS"
                  />
                </label>{" "}
                <label>
                  Cohort key{" "}
                  <input
                    aria-label="Pilot cohort key"
                    value={pilotCohort}
                    onChange={(e) => setPilotCohort(e.target.value)}
                    placeholder="published cohort config key"
                  />
                </label>{" "}
                <label>
                  Starts{" "}
                  <input
                    aria-label="Pilot start date"
                    type="date"
                    value={pilotStarts}
                    onChange={(e) => setPilotStarts(e.target.value)}
                  />
                </label>{" "}
                <label>
                  Ends{" "}
                  <input
                    aria-label="Pilot end date"
                    type="date"
                    value={pilotEnds}
                    onChange={(e) => setPilotEnds(e.target.value)}
                  />
                </label>
              </p>
              {pilotError && (
                <p className="error-note" role="alert">
                  {pilotError}
                </p>
              )}
              <p>
                <button
                  type="button"
                  disabled={
                    (!pilotUnit.trim() && !pilotCohort.trim()) ||
                    !pilotStarts ||
                    !pilotEnds
                  }
                  onClick={() => void createPilot()}
                >
                  Create pilot
                </button>{" "}
                <span className="hint">
                  Scope requires a unit and/or a published cohort; the
                  window is bounded by the published maxPilotDays.
                </span>
              </p>
            </div>
          )}
        </div>
      )}

      <h3>Pilots</h3>
      {pilots.loading ? (
        <Loading label="Loading pilots…" />
      ) : pilotList.length === 0 ? (
        <Empty label="No pilots yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Pilot</th>
              <th>Capability</th>
              <th>Scope</th>
              <th>Window</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {pilotList.map((p) => (
              <tr key={p.id}>
                <td>#{p.id} (intake #{p.intake_id})</td>
                <td>
                  {p.capability_type}: <code>{p.capability_key}</code>
                </td>
                <td>
                  {p.scope.unit ? `unit: ${p.scope.unit}` : ""}
                  {p.scope.unit && p.scope.cohortKey ? " · " : ""}
                  {p.scope.cohortKey ? `cohort: ${p.scope.cohortKey}` : ""}
                  {!p.scope.unit && !p.scope.cohortKey ? "—" : ""}
                </td>
                <td>
                  {p.starts_on} → {p.ends_on}
                </td>
                <td>
                  <StatusBadge tone={pilotStatusTone(p.status)}>
                    {p.status}
                  </StatusBadge>
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() => {
                      setSelectedPilotId(p.id);
                      setEvaluation(null);
                      setExerciseError(null);
                    }}
                  >
                    Open
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedPilot && (
        <div>
          <h4>
            Pilot #{selectedPilot.id} — {selectedPilot.capability_type}:{" "}
            <code>{selectedPilot.capability_key}</code>{" "}
            <StatusBadge tone={pilotStatusTone(selectedPilot.status)}>
              {selectedPilot.status}
            </StatusBadge>
          </h4>
          <p className="hint">
            Scope:{" "}
            {selectedPilot.scope.unit ? `unit ${selectedPilot.scope.unit}` : ""}
            {selectedPilot.scope.unit && selectedPilot.scope.cohortKey ? " and " : ""}
            {selectedPilot.scope.cohortKey
              ? `cohort ${selectedPilot.scope.cohortKey}`
              : ""}
            {" "}· window {selectedPilot.starts_on} to {selectedPilot.ends_on}.
            Exercise outside this scope or window is refused and the
            refusal is audited.
          </p>

          {selectedPilot.status === "piloting" && (
            <div>
              <h5>Exercise in trial scope</h5>
              <p>
                <label>
                  Patient reference{" "}
                  <input
                    aria-label="Exercise patient reference"
                    value={exercisePatient}
                    onChange={(e) => setExercisePatient(e.target.value)}
                    placeholder="optional — Patient/…"
                  />
                </label>
              </p>
              <div>
                <RichTextEditor
                  ariaLabel="Exercise input"
                  value={exerciseInput}
                  onChange={setExerciseInput}
                  minRows={3}
                />
              </div>
              <p className="hint">
                Prompt for an agent capability (ignored for pack/config reports)
              </p>
              {exerciseError && (
                <p className="error-note" role="alert">
                  {exerciseError}
                </p>
              )}
              <p>
                <button type="button" onClick={() => void exercise()}>
                  Exercise pilot
                </button>
              </p>
              {exerciseResult && (
                <div>
                  <h6>Exercise report</h6>
                  <pre>{JSON.stringify(exerciseResult, null, 2)}</pre>
                </div>
              )}
            </div>
          )}

          <h5>Evaluation</h5>
          <p>
            <button type="button" onClick={() => void loadEvaluation()}>
              Load evaluation
            </button>
          </p>
          {evaluationError && (
            <p className="error-note" role="alert">
              {evaluationError}
            </p>
          )}
          {evaluation && (
            <div>
              <p className="hint" role="status">
                Exercises: {evaluation.exercises.succeeded} succeeded,{" "}
                {evaluation.exercises.refused} refused · pilot runs:{" "}
                {evaluation.runs.total} (dispositions:{" "}
                {Object.entries(evaluation.runs.dispositions)
                  .map(([k, v]) => `${k} ${v}`)
                  .join(", ") || "none"})
              </p>
              {evaluation.exercises.refusals.length > 0 && (
                <ul>
                  {evaluation.exercises.refusals.map((r, i) => (
                    <li key={i}>
                      Refused {r.patient ?? "—"}: {r.reason ?? "no reason recorded"}
                    </li>
                  ))}
                </ul>
              )}
              {evaluation.outcome && (
                <div>
                  <h6>Submitted outcome</h6>
                  <pre>{JSON.stringify(evaluation.outcome, null, 2)}</pre>
                </div>
              )}
              {evaluation.decisions.length > 0 && (
                <ul>
                  {evaluation.decisions.map((d) => (
                    <li key={d.id}>
                      Decision: <strong>{d.decision}</strong> by {d.decided_by} —{" "}
                      {d.rationale}
                    </li>
                  ))}
                </ul>
              )}
              {evaluation.handoffs.map((h) => (
                <p key={h.id} className="hint">
                  Scale hand-off ({h.status}): {h.required_step}
                </p>
              ))}
            </div>
          )}

          {selectedPilot.status === "piloting" && (
            <div>
              <h5>Submit the evaluation outcome</h5>
              <p>
                <textarea
                  aria-label="Evaluation outcome JSON"
                  value={outcomeText}
                  onChange={(e) => setOutcomeText(e.target.value)}
                  rows={5}
                  cols={72}
                />
              </p>
              {outcomeError && (
                <p className="error-note" role="alert">
                  {outcomeError}
                </p>
              )}
              <p>
                <button type="button" onClick={() => void submitOutcome()}>
                  Submit outcome (moves pilot to evaluated)
                </button>
              </p>
            </div>
          )}

          {selectedPilot.status === "evaluated" && (
            <div>
              <h5>Decide</h5>
              <p>
                <label>
                  Decision{" "}
                  <select
                    aria-label="Pilot decision"
                    value={decision}
                    onChange={(e) => setDecision(e.target.value as "scale" | "kill" | "extend")}
                  >
                    <option value="scale">scale</option>
                    <option value="kill">kill</option>
                    <option value="extend">extend</option>
                  </select>
                </label>{" "}
                <label>
                  Rationale{" "}
                  <input
                    aria-label="Decision rationale"
                    value={rationale}
                    onChange={(e) => setRationale(e.target.value)}
                    size={44}
                  />
                </label>{" "}
                {decision === "extend" && (
                  <label>
                    New end date{" "}
                    <input
                      aria-label="New end date"
                      type="date"
                      value={newEndsOn}
                      onChange={(e) => setNewEndsOn(e.target.value)}
                    />
                  </label>
                )}
              </p>
              {decisionError && (
                <p className="error-note" role="alert">
                  {decisionError}
                </p>
              )}
              {decisionResult && (
                <p className="hint" role="status">
                  {decisionResult}
                </p>
              )}
              <p>
                <button
                  type="button"
                  disabled={!rationale.trim()}
                  onClick={() => void decide()}
                >
                  Record decision
                </button>{" "}
                <span className="hint">
                  Scale creates a hand-off record only — a human takes
                  the named Studio step; nothing is enabled here.
                </span>
              </p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
