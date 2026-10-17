import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../../api/client";
import type {
  FormDefinition,
  QuestionnaireAnswerItem,
} from "../../api/types";
import { FormRenderer } from "../../components/FormRenderer";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

interface Props {
  client: ApiClient;
  patientId: string;
  canWork: boolean;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function answerScalars(answers: QuestionnaireAnswerItem[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of answers) {
    const first = item.answer?.[0];
    if (!first) continue;
    const coding = first.valueCoding as { code?: string } | undefined;
    if (coding?.code) out[item.linkId] = coding.code;
    else if (typeof first.valueDate === "string") out[item.linkId] = first.valueDate;
    else if (typeof first.valueString === "string") out[item.linkId] = first.valueString;
  }
  return out;
}

const NEXT_TASK_STATES: Record<string, string[]> = {
  requested: ["in-progress", "cancelled"],
  "in-progress": ["completed", "cancelled"],
};

/** Phase 6 chart tab: the patient's current inpatient admission —
 *  bed + LOS, the case-management care plan (FHIR Tasks), the
 *  discharge plan, the published discharge-checklist form, and the
 *  ADT event history for the encounter. Everything site-specific
 *  (dispositions, the checklist form) is Studio config read at
 *  runtime. */
export function AdmissionTab({ client, patientId, canWork }: Props) {
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [newItem, setNewItem] = useState("");
  const [planForm, setPlanForm] = useState({ targetDate: "", disposition: "", barriers: "" });
  const [checklistForm, setChecklistForm] = useState<FormDefinition | null>(null);
  const [showChecklist, setShowChecklist] = useState(false);

  const admissionState = useAsync(
    () => client.patientAdmission(patientId),
    [client, patientId, tick],
  );
  const admission = admissionState.data?.admission ?? null;
  const encounterId = admission?.encounter.id as string | undefined;

  const carePlanState = useAsync(
    async () => (encounterId ? client.carePlan(encounterId) : null),
    [client, encounterId, tick],
  );
  const eventsState = useAsync(
    async () => (encounterId ? client.adtEvents({ encounterId }) : null),
    [client, encounterId, tick],
  );
  const dispositionsState = useAsync(
    () => client.dischargeDispositions(),
    [client],
  );

  useEffect(() => {
    if (admission?.discharge_plan) {
      setPlanForm({
        targetDate: admission.discharge_plan.targetDate ?? "",
        disposition: admission.discharge_plan.disposition ?? "",
        barriers: admission.discharge_plan.barriers ?? "",
      });
    }
  }, [admission?.encounter.id]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false;
    if (!showChecklist || typeof client.effectiveConfig !== "function") return;
    client
      .effectiveConfig("form", "discharge-checklist")
      .then((cfg) => {
        if (!cancelled) setChecklistForm(cfg.payload as unknown as FormDefinition);
      })
      .catch(() => {
        if (!cancelled) setChecklistForm(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, showChecklist]);

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

  if (admissionState.loading) return <Loading label="Loading admission…" />;
  if (admissionState.error != null) return <ErrorView error={admissionState.error} />;
  if (!admission || !encounterId) {
    return <Empty label="No current inpatient admission for this patient." />;
  }

  const plan = admission.discharge_plan;
  const dispositions = (dispositionsState.data?.dispositions ?? []).filter(
    (d) => d.active !== false,
  );

  async function submitChecklist(answers: QuestionnaireAnswerItem[]) {
    const scalars = answerScalars(answers);
    await perform("Discharge checklist saved", async () => {
      const qr = await client.create("QuestionnaireResponse", {
        resourceType: "QuestionnaireResponse",
        questionnaire: "Questionnaire/discharge-checklist",
        status: "completed",
        subject: { reference: `Patient/${patientId}` },
        encounter: { reference: `Encounter/${encounterId}` },
        item: answers,
      });
      await client.setDischargePlan(encounterId as string, {
        targetDate: scalars["target-discharge-date"],
        disposition: scalars["disposition"],
        barriers: scalars["barriers"],
        checklistResponseId: qr.id,
      });
      setShowChecklist(false);
    });
  }

  return (
    <div>
      <h3>Current admission</h3>
      <p className="hint">
        Bed <strong>{admission.bed_code ?? "not assigned"}</strong>
        {admission.admitted_at
          ? ` · admitted ${new Date(admission.admitted_at).toLocaleString()}`
          : ""}
        {admission.los_days != null ? ` · LOS ${admission.los_days} d` : ""}
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      <h4>Discharge plan</h4>
      {plan ? (
        <p className="hint">
          Target {plan.targetDate ?? "—"}
          {plan.disposition ? ` · disposition ${plan.disposition}` : ""}
          {plan.barriers ? ` · barriers: ${plan.barriers}` : ""}
          {plan.checklistResponseId ? " · checklist on file" : ""}
        </p>
      ) : (
        <p className="hint">No discharge plan recorded yet.</p>
      )}
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Discharge plan saved", () =>
              client.setDischargePlan(encounterId, {
                targetDate: planForm.targetDate || undefined,
                disposition: planForm.disposition || undefined,
                barriers: planForm.barriers || undefined,
              }));
          }}
        >
          <label>
            Target date{" "}
            <input
              type="date"
              aria-label="Target discharge date"
              value={planForm.targetDate}
              onChange={(e) => setPlanForm({ ...planForm, targetDate: e.target.value })}
            />
          </label>
          <label>
            Disposition{" "}
            <select
              aria-label="Discharge disposition"
              value={planForm.disposition}
              onChange={(e) => setPlanForm({ ...planForm, disposition: e.target.value })}
            >
              <option value="">Select…</option>
              {dispositions.map((d) => (
                <option key={d.code} value={d.code}>
                  {d.display ?? d.code}
                </option>
              ))}
            </select>
          </label>
          <label>
            Barriers{" "}
            <input
              aria-label="Discharge barriers"
              value={planForm.barriers}
              onChange={(e) => setPlanForm({ ...planForm, barriers: e.target.value })}
            />
          </label>
          <button type="submit" disabled={busy}>Save plan</button>
          <button
            type="button"
            disabled={busy}
            onClick={() => setShowChecklist((s) => !s)}
          >
            Discharge checklist
          </button>
        </form>
      )}
      {canWork && showChecklist && (
        checklistForm ? (
          <FormRenderer
            form={checklistForm}
            submitLabel="Save checklist"
            onSubmit={submitChecklist}
          />
        ) : (
          <p className="hint">Loading the published discharge checklist…</p>
        )
      )}

      <h4>Care plan</h4>
      {carePlanState.data && carePlanState.data.tasks.length === 0 && (
        <Empty label="No care-plan items yet." />
      )}
      {carePlanState.data && carePlanState.data.tasks.length > 0 && (
        <ul className="board-list">
          {carePlanState.data.tasks.map((task) => (
            <li key={task.id} className="board-card">
              <div>
                {task.description ?? task.id}{" "}
                <span className="badge">{task.status}</span>
              </div>
              {canWork && (NEXT_TASK_STATES[task.status ?? ""] ?? []).length > 0 && (
                <div className="row-actions">
                  {(NEXT_TASK_STATES[task.status ?? ""] ?? []).map((target) => (
                    <button
                      key={target}
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        perform(`Care-plan item → ${target}`, () =>
                          client.transitionCarePlanItem(task.id as string, target))}
                    >
                      {target}
                    </button>
                  ))}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!newItem.trim()) return;
            void perform("Care-plan item added", async () => {
              await client.addCarePlanItem(encounterId, newItem.trim());
              setNewItem("");
            });
          }}
        >
          <label>
            New item{" "}
            <input
              aria-label="New care-plan item"
              value={newItem}
              onChange={(e) => setNewItem(e.target.value)}
              placeholder="Arrange transport"
            />
          </label>
          <button type="submit" disabled={busy}>Add item</button>
        </form>
      )}

      <h4>ADT history</h4>
      {eventsState.data && eventsState.data.events.length === 0 && (
        <Empty label="No ADT events recorded for this admission." />
      )}
      {eventsState.data && eventsState.data.events.length > 0 && (
        <ul>
          {eventsState.data.events.map((event) => (
            <li key={event.id} className="hint">
              <strong>{event.event_type}</strong>
              {event.event_display ? ` — ${event.event_display}` : ""}
              {event.from_location ? ` · from ${event.from_location}` : ""}
              {event.to_location ? ` · to ${event.to_location}` : ""}
              {event.at ? ` · ${new Date(event.at).toLocaleString()}` : ""}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
