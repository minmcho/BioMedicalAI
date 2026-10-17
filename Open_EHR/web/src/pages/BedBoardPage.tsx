import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import type {
  BedEntry,
  FormDefinition,
  QuestionnaireAnswerItem,
  Role,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { FormRenderer } from "../components/FormRenderer";
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

/** Flatten FormRenderer answers to {linkId: scalar} for plan fields. */
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

export function BedBoardPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["inpatient.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [admitForm, setAdmitForm] = useState({ patient: "", bed: "" });
  const [transferBed, setTransferBed] = useState<Record<string, string>>({});
  const [dischargeFor, setDischargeFor] = useState<BedEntry | null>(null);
  const [checklistForm, setChecklistForm] = useState<FormDefinition | null>(null);

  const censusState = useAsync(() => client.inpatientCensus(), [client, tick]);
  const census = censusState.data;

  // The discharge checklist is a published Studio form — load its
  // definition when a discharge panel opens (runtime config, like the
  // note templates in the chart).
  useEffect(() => {
    let cancelled = false;
    if (!dischargeFor) {
      setChecklistForm(null);
      return;
    }
    if (typeof client.effectiveConfig === "function") {
      client
        .effectiveConfig("form", "discharge-checklist")
        .then((cfg) => {
          if (!cancelled) {
            setChecklistForm(cfg.payload as unknown as FormDefinition);
          }
        })
        .catch(() => {
          if (!cancelled) setChecklistForm(null);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [client, dischargeFor]);

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

  const availableBeds = (census?.units ?? []).flatMap((u) =>
    u.beds.filter((b) => b.status === "available"),
  );

  async function submitChecklist(answers: QuestionnaireAnswerItem[]) {
    if (!dischargeFor?.encounter_id || !dischargeFor.patient_ref) return;
    const scalars = answerScalars(answers);
    await perform("Discharge recorded", async () => {
      const qr = await client.create("QuestionnaireResponse", {
        resourceType: "QuestionnaireResponse",
        questionnaire: "Questionnaire/discharge-checklist",
        status: "completed",
        subject: { reference: dischargeFor.patient_ref },
        encounter: { reference: `Encounter/${dischargeFor.encounter_id}` },
        item: answers,
      });
      await client.setDischargePlan(dischargeFor.encounter_id as string, {
        targetDate: scalars["target-discharge-date"],
        disposition: scalars["disposition"],
        barriers: scalars["barriers"],
        checklistResponseId: qr.id,
      });
      await client.dischargePatient(
        dischargeFor.encounter_id as string,
        scalars["disposition"],
      );
      setDischargeFor(null);
    });
  }

  function renderBed(bed: BedEntry) {
    const transferTarget = transferBed[bed.encounter_id ?? ""] ?? "";
    return (
      <li key={bed.code} className="board-card">
        <div>
          <strong>{bed.code}</strong>{" "}
          <span className="badge">{bed.status}</span>
        </div>
        <div className="hint">{bed.display ?? bed.code}</div>
        {bed.status === "occupied" && (
          <div className="hint">
            Patient {bed.patient_ref ? refLabel({ reference: bed.patient_ref }) : "—"}
            {bed.los_days != null ? ` · LOS ${bed.los_days} d` : ""}
            {bed.admitted_at
              ? ` · admitted ${new Date(bed.admitted_at).toLocaleDateString()}`
              : ""}
          </div>
        )}
        {canWork && bed.status === "cleaning" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} marked available`, () =>
                  client.setBedStatus(bed.code, "available"))}
            >
              Mark available
            </button>
          </div>
        )}
        {canWork && bed.status === "available" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} blocked`, () =>
                  client.setBedStatus(bed.code, "blocked"))}
            >
              Block bed
            </button>
          </div>
        )}
        {canWork && bed.status === "blocked" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} marked available`, () =>
                  client.setBedStatus(bed.code, "available"))}
            >
              Mark available
            </button>
          </div>
        )}
        {canWork && bed.status === "occupied" && bed.encounter_id && (
          <div className="row-actions">
            <label>
              Transfer to{" "}
              <select
                aria-label={`Transfer target for ${bed.code}`}
                value={transferTarget}
                onChange={(e) =>
                  setTransferBed((prev) => ({
                    ...prev,
                    [bed.encounter_id as string]: e.target.value,
                  }))}
              >
                <option value="">Select bed…</option>
                {availableBeds.map((b) => (
                  <option key={b.code} value={b.code}>{b.code}</option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={busy || !transferTarget}
              onClick={() =>
                perform("Patient transferred", () =>
                  client.transferPatient(bed.encounter_id as string, transferTarget))}
            >
              Transfer
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setDischargeFor(bed)}
            >
              Discharge…
            </button>
          </div>
        )}
      </li>
    );
  }

  return (
    <section>
      <h2>Bed board</h2>
      <p className="hint">
        Hospital locations and beds are published Studio configuration
        (terminology/locations) materialised as FHIR Location resources;
        every admission, transfer and discharge writes an ADT event
        (A01/A02/A03) and an audited bed-status change — beds are never
        edited silently. A discharged or transferred-out bed goes to
        cleaning until housekeeping marks it available.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      {censusState.loading && <Loading label="Loading bed board…" />}
      {censusState.error != null && <ErrorView error={censusState.error} />}
      {census && (
        <p className="hint" data-testid="census-totals">
          {census.totals.beds_total} beds · {census.totals.occupied} occupied ·{" "}
          {census.totals.available} available · {census.totals.cleaning} cleaning ·{" "}
          {census.totals.blocked} blocked · occupancy {census.totals.occupancy_pct}%
        </p>
      )}

      {census && census.expected_discharges_today.total > 0 && (
        <div>
          <h3>Expected discharges today ({census.expected_discharges_today.total})</h3>
          <ul className="board-list">
            {census.expected_discharges_today.discharges.map((d) => (
              <li key={d.encounter_id} className="board-card">
                <strong>{d.bed_code ?? "—"}</strong>{" "}
                <span className="hint">
                  Patient {d.patient_ref ? refLabel({ reference: d.patient_ref }) : "—"}
                  {d.disposition ? ` · disposition ${d.disposition}` : ""}
                  {d.barriers ? ` · barriers: ${d.barriers}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {canWork && census && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!admitForm.patient.trim() || !admitForm.bed) return;
            void perform("Patient admitted", async () => {
              await client.admitPatient({
                patient: admitForm.patient.trim(),
                bed: admitForm.bed,
              });
              setAdmitForm({ patient: "", bed: "" });
            });
          }}
        >
          <h3>Admit a patient</h3>
          <label>
            Patient id{" "}
            <input
              aria-label="Admit patient id"
              value={admitForm.patient}
              onChange={(e) => setAdmitForm({ ...admitForm, patient: e.target.value })}
              placeholder="pat-1"
            />
          </label>
          <label>
            Bed{" "}
            <select
              aria-label="Admit bed"
              value={admitForm.bed}
              onChange={(e) => setAdmitForm({ ...admitForm, bed: e.target.value })}
            >
              <option value="">Select bed…</option>
              {availableBeds.map((b) => (
                <option key={b.code} value={b.code}>
                  {b.code} — {b.display ?? b.code}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy}>Admit</button>
        </form>
      )}

      {dischargeFor && (
        <div className="inline-form">
          <h3>Discharge — {dischargeFor.code}</h3>
          <p className="hint">
            Patient {dischargeFor.patient_ref
              ? refLabel({ reference: dischargeFor.patient_ref })
              : "—"}. The checklist is the published Studio form
            (form/discharge-checklist); submitting it records the
            discharge plan and discharges the patient (the bed goes to
            cleaning).
          </p>
          {checklistForm ? (
            <FormRenderer
              form={checklistForm}
              submitLabel="Submit checklist & discharge"
              onSubmit={submitChecklist}
            />
          ) : (
            <p className="hint">Loading the published discharge checklist…</p>
          )}
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform("Patient discharged", async () => {
                  await client.dischargePatient(dischargeFor.encounter_id as string);
                  setDischargeFor(null);
                })}
            >
              Discharge without checklist
            </button>
            <button type="button" onClick={() => setDischargeFor(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {census && census.units.length === 0 && (
        <Empty label="No units in the published locations configuration." />
      )}
      {census && census.units.map((unit) => (
        <div key={unit.code}>
          <h3>
            {unit.display ?? unit.code} — {unit.occupied}/{unit.beds_total} occupied
            ({unit.occupancy_pct}%)
          </h3>
          <ul className="board-list">{unit.beds.map(renderBed)}</ul>
        </div>
      ))}
    </section>
  );
}
