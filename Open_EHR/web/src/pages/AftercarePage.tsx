import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import type { QuestionnaireAnswerItem, Role } from "../api/types";
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

const CALL_OUTCOMES = [
  "reached-well", "reached-concern", "reached-needs-visit",
  "no-answer", "wrong-number",
];

/** Phase 12 aftercare: the 48–72h post-discharge call queue with
 *  scripted outcomes, the deterministic readmission-risk worklist
 *  (config-weighted factors, labelled — not a validated model) and
 *  the hospital-at-home census board. The HaH board is workflow only:
 *  no device feeds exist until Phase 14, and none are fabricated. */
export function AftercarePage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["continuum.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outcomes, setOutcomes] = useState<Record<string, string>>({});
  // Depth pass (Workstream 2): discharge medication reconciliation —
  // the governed discharge-med-rec form, beside teach-back in the
  // discharge workflow (its readiness effect is config-gated
  // server-side by dischargeGuards.requireMedRec).
  const [medRecEncounter, setMedRecEncounter] = useState("");
  const [medRec, setMedRec] = useState({
    homeReviewed: false, changes: "", newPrescriptions: "",
    discontinued: "", allergies: false,
  });
  const [medRecError, setMedRecError] = useState<string | null>(null);
  // UX pass (spec P-7): calls / med-rec / risk / hospital-at-home are
  // tabs, not one stacked scroll.
  const [tab, setTab] = useState("calls");

  const callsState = useAsync(() => client.aftercareCalls(), [client, tick]);
  const risksState = useAsync(() => client.readmissionRisks(), [client, tick]);
  const hahState = useAsync(() => client.hospitalAtHome(), [client, tick]);

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

  const callsPanel = (
    <>
      <h3>Post-discharge calls ({callsState.data?.length ?? 0})</h3>
      {callsState.loading && <Loading label="Loading call queue…" />}
      {callsState.error != null && <ErrorView error={callsState.error} />}
      {callsState.data && callsState.data.length === 0 && (
        <Empty label="No post-discharge calls outstanding." />
      )}
      <ul className="board-list">
        {callsState.data?.map((item) => (
          <li key={item.encounter_id} className="board-card">
            <div>
              <strong>{refText(item.patient_ref)}</strong>
              <StatusBadge
                tone={item.state === "overdue" ? "bad" : item.state === "due" ? "warn" : "info"}
              >
                {item.state}
              </StatusBadge>
              <span className="badge">{item.hours_since_discharge}h since discharge</span>
            </div>
            {canWork && (
              <div className="row-actions">
                <select
                  aria-label={`Call outcome for ${item.encounter_id}`}
                  value={outcomes[item.encounter_id] ?? "reached-well"}
                  onChange={(e) =>
                    setOutcomes((prev) => ({ ...prev, [item.encounter_id]: e.target.value }))}
                >
                  {CALL_OUTCOMES.map((o) => (
                    <option key={o} value={o}>{o}</option>
                  ))}
                </select>
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Call recorded", () =>
                      client.recordAftercareCall({
                        encounterId: item.encounter_id,
                        outcome: outcomes[item.encounter_id] ?? "reached-well",
                        reached: (outcomes[item.encounter_id] ?? "reached-well").startsWith("reached"),
                      }))}
                >
                  Record call
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </>
  );

  const medRecPanel = (
    <>
      <h3>Discharge medication reconciliation</h3>
      <p className="hint">
        Record the medication reconciliation for an admission — home
        medications reviewed, changes explained, allergies rechecked —
        beside the teach-back sign-off in the discharge workflow. The
        readiness board reports it as Med-rec: Done; whether it gates
        readiness is the site's published requireMedRec guard.
      </p>
      {canWork && (
        <form
          noValidate
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            setMedRecError(null);
            if (!medRecEncounter.trim()) {
              setMedRecError("Encounter id is required.");
              return;
            }
            if (!medRec.changes.trim()) {
              setMedRecError("Medication changes is required.");
              return;
            }
            if (typeof client.submitDischargeMedRec !== "function") return;
            const answers: QuestionnaireAnswerItem[] = [
              { linkId: "home-medications-reviewed", answer: [{ valueBoolean: medRec.homeReviewed }] },
              { linkId: "medication-changes", answer: [{ valueString: medRec.changes.trim() }] },
            ];
            if (medRec.newPrescriptions.trim()) {
              answers.push({ linkId: "new-prescriptions", answer: [{ valueString: medRec.newPrescriptions.trim() }] });
            }
            if (medRec.discontinued.trim()) {
              answers.push({ linkId: "discontinued-medications", answer: [{ valueString: medRec.discontinued.trim() }] });
            }
            answers.push({ linkId: "allergies-rechecked", answer: [{ valueBoolean: medRec.allergies }] });
            void (async () => {
              setBusy(true);
              setActionError(null);
              setNotice(null);
              try {
                await client.submitDischargeMedRec(medRecEncounter.trim(), answers);
                setNotice("Medication reconciliation recorded — done.");
                setMedRecEncounter("");
                setMedRec({ homeReviewed: false, changes: "", newPrescriptions: "", discontinued: "", allergies: false });
                setTick((t) => t + 1);
              } catch (err) {
                setMedRecError(errorText(err));
              } finally {
                setBusy(false);
              }
            })();
          }}
        >
          {medRecError && <div className="state state-error" role="alert">{medRecError}</div>}
          <label>
            Encounter id{" "}
            <input
              aria-label="Med-rec encounter id"
              value={medRecEncounter}
              onChange={(e) => setMedRecEncounter(e.target.value)}
              placeholder="encounter id"
            />
          </label>
          <label>
            <input
              type="checkbox"
              aria-label="Home medications reviewed"
              checked={medRec.homeReviewed}
              onChange={(e) => setMedRec({ ...medRec, homeReviewed: e.target.checked })}
            />{" "}
            Home medications reviewed *
          </label>
          <label>
            Medication changes *{" "}
            <input
              aria-label="Medication changes"
              value={medRec.changes}
              onChange={(e) => setMedRec({ ...medRec, changes: e.target.value })}
            />
          </label>
          <label>
            New prescriptions{" "}
            <input
              aria-label="New prescriptions"
              value={medRec.newPrescriptions}
              onChange={(e) => setMedRec({ ...medRec, newPrescriptions: e.target.value })}
            />
          </label>
          <label>
            Discontinued medications{" "}
            <input
              aria-label="Discontinued medications"
              value={medRec.discontinued}
              onChange={(e) => setMedRec({ ...medRec, discontinued: e.target.value })}
            />
          </label>
          <label>
            <input
              type="checkbox"
              aria-label="Allergies rechecked"
              checked={medRec.allergies}
              onChange={(e) => setMedRec({ ...medRec, allergies: e.target.checked })}
            />{" "}
            Allergies rechecked *
          </label>
          <button type="submit" disabled={busy}>Record medication reconciliation</button>
        </form>
      )}
    </>
  );

  const riskPanel = (
    <>
      <h3>Readmission-risk worklist</h3>
      {risksState.data && (
        <p className="hint">{risksState.data.basis}</p>
      )}
      {risksState.data && risksState.data.items.length === 0 && (
        <Empty label="No discharges score at or above the configured threshold." />
      )}
      <ul className="board-list">
        {risksState.data?.items.map((item) => (
          <li key={item.encounter_id} className="board-card">
            <div>
              <strong>{refText(item.patient_ref)}</strong>
              <span className="badge">score {item.score}</span>
            </div>
            <div className="hint">
              Factors: {Object.entries(item.factors)
                .map(([k, v]) => `${k} (+${v})`).join(", ")}
            </div>
          </li>
        ))}
      </ul>
    </>
  );

  const hahPanel = (
    <>
      <h3>Hospital at home ({hahState.data?.total ?? 0})</h3>
      {hahState.data && <p className="hint">{hahState.data.note}</p>}
      {hahState.data && hahState.data.total === 0 && (
        <Empty label="No patients are currently enrolled at home." />
      )}
      <ul className="board-list">
        {hahState.data?.census.map((entry) => (
          <li key={entry.encounter_id} className="board-card">
            <div>
              <strong>{refText(entry.patient_ref)}</strong>
              <span className="badge">{entry.open_visit_tasks} open visit tasks</span>
            </div>
          </li>
        ))}
      </ul>
      {hahState.data && hahState.data.eligible.length > 0 && (
        <p className="hint">
          Flagged eligible (not yet enrolled): {hahState.data.eligible.join(", ")}
        </p>
      )}
    </>
  );

  return (
    <section>
      <ModuleHeader
        icon="route"
        group="Operations"
        title="Aftercare"
        purpose="Post-discharge follow-up: calls, discharge medication reconciliation, readmission risk and hospital at home."
      />
      <p className="hint">
        Post-discharge follow-up as one queue: calls due in the
        configured 48–72h window, readmission-risk outreach and
        hospital-at-home visits. Discharge cannot complete (when the
        site enables the guard in config) until teach-back is signed
        off and a follow-up appointment is booked.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      <Tabs
        tabs={[
          { key: "calls", label: "Post-discharge calls" },
          { key: "medrec", label: "Medication reconciliation" },
          { key: "risk", label: "Readmission risk" },
          { key: "hah", label: "Hospital at home" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Aftercare views"
        panels={{
          calls: callsPanel,
          medrec: medRecPanel,
          risk: riskPanel,
          hah: hahPanel,
        }}
      />
    </section>
  );
}
