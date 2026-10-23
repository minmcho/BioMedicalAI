import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  createAntibodyWorkup, createPreparation, createReactionWorkup,
  createRequirement, createReservation, createUnitAttribute,
  deactivateRequirement, fileHaemovigilance, getTransfusionConfig,
  getWastageReport, gradeReactionWorkup, listHistorical, listPreparations,
  listReactionWorkups, listRequirements, listReservations, listTsSpecimens,
  listWorkups, recordDisposition, recordPrepStep, recordReactionStep,
  registerTsSpecimen, releaseReservation,
  type AntibodyWorkup, type HistoricalAntibody, type Preparation,
  type ReactionWorkup, type Reservation, type SpecialRequirement,
  type TransfusionConfig, type TsSpecimen, type WastageReport,
} from "../api/labTransfusion";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

// LAB-D13 — transfusion service completion surface (app.lab_transfusion).
// Mounted by the coordinator as a Lab page section (see the W7
// INTEGRATION NOTE). Every dropdown is fed by the governed config
// endpoint; a control whose governed source is empty is disabled
// WITH the reason shown — no dead controls. Standing requirements,
// historical antibodies, specimen validity, reservations, post-thaw
// expiry and the reaction-workup sequence all come from the server;
// this surface records and renders, it never computes gate verdicts
// locally (the Wave A gate stays the only verdict).

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  }
  return err instanceof Error ? err.message : String(err);
}

function Options({ options }: { options: { code: string; display: string }[] }) {
  return (
    <>
      {options.map((o) => (
        <option key={o.code} value={o.code}>{o.display}</option>
      ))}
    </>
  );
}

const splitList = (text: string) =>
  text.split(",").map((s) => s.trim()).filter(Boolean);

export function LabTransfusion({ client }: Props) {
  const configState = useAsync<TransfusionConfig>(
    () => getTransfusionConfig(client), [client]);
  const [tick, setTick] = useState(0);
  const boardState = useAsync(
    async () => {
      const [specimens, reservations, preparations, reactions, wastage] =
        await Promise.all([
          listTsSpecimens(client), listReservations(client),
          listPreparations(client), listReactionWorkups(client),
          getWastageReport(client),
        ]);
      return { specimens, reservations, preparations, reactions, wastage };
    }, [client, tick]);
  const [error, setError] = useState<string | null>(null);

  // Patient-scoped state
  const [patientRef, setPatientRef] = useState("");
  const [loadedPatient, setLoadedPatient] = useState("");
  const patientState = useAsync(
    async () => (loadedPatient ? {
      historical: await listHistorical(client, loadedPatient),
      workups: await listWorkups(client, loadedPatient),
      requirements: await listRequirements(client, loadedPatient),
    } : null), [client, loadedPatient, tick]);

  // Forms
  const [tsCollectedAt, setTsCollectedAt] = useState("");
  const [tsKind, setTsKind] = useState("standard");
  const [tsTransfusion, setTsTransfusion] = useState(false);
  const [tsPregnancy, setTsPregnancy] = useState(false);
  const [abMethod, setAbMethod] = useState("");
  const [abCells, setAbCells] = useState("");
  const [abIdentified, setAbIdentified] = useState("");
  const [abRuledOut, setAbRuledOut] = useState("");
  const [abConclusion, setAbConclusion] = useState("");
  const [reqCode, setReqCode] = useState("");
  const [reqSpecificity, setReqSpecificity] = useState("");
  const [attrUnit, setAttrUnit] = useState("");
  const [attrCode, setAttrCode] = useState("");
  const [attrSpecificity, setAttrSpecificity] = useState("");
  const [resUnit, setResUnit] = useState("");
  const [releaseReason, setReleaseReason] = useState("");
  const [prepUnit, setPrepUnit] = useState("");
  const [prepKind, setPrepKind] = useState("");
  const [dispUnit, setDispUnit] = useState("");
  const [dispCode, setDispCode] = useState("");
  const [dispReason, setDispReason] = useState("");
  const [rxnUnit, setRxnUnit] = useState("");
  const [rxnResults, setRxnResults] = useState<Record<string, string>>({});
  const [rxnSeverity, setRxnSeverity] = useState<Record<string, string>>({});
  const [rxnImputability, setRxnImputability] = useState<Record<string, string>>({});

  if (configState.loading) return <Loading label="Loading transfusion config…" />;
  if (configState.error) return <ErrorView error={configState.error} />;
  const config = configState.data;
  if (!config) return <Empty label="No transfusion configuration available." />;
  const board = boardState.data;
  const patient = patientState.data;

  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      setTick((t) => t + 1);
    } catch (err) {
      setError(errorText(err));
    }
  };

  return (
    <section aria-label="Transfusion service">
      <h2>Transfusion service (LAB-D13)</h2>
      <p>
        Governed windows: T&amp;S standard {config.standard_validity_hours} h;
        extended presurgical {config.extended_validity_days} days (no
        transfusion/pregnancy in {config.extended_lookback_days} days);
        reservation expiry {config.reservation_expiry_hours} h.
      </p>
      {error && <p role="alert">{error}</p>}
      {boardState.loading && <Loading label="Loading transfusion boards…" />}
      {boardState.error ? <ErrorView error={boardState.error} /> : null}

      <div>
        <label>
          Patient reference{" "}
          <input aria-label="Patient reference" value={patientRef}
                 placeholder="Patient/pat-1"
                 onChange={(e) => setPatientRef(e.target.value)} />
        </label>
        <button type="button" disabled={!patientRef.trim()}
                onClick={() => setLoadedPatient(patientRef.trim())}>
          Load patient transfusion record
        </button>
        {!patientRef.trim() && (
          <span> Enter a patient reference to load its record.</span>)}
      </div>

      <h3>T&amp;S specimen-validity board</h3>
      {board && board.specimens.items.length === 0 && (
        <p>No type-and-screen specimens registered yet.</p>)}
      {board && board.specimens.items.map((s: TsSpecimen) => (
        <p key={s.specimen_id}>
          {s.specimen_id} — {s.patient_ref}; collected {s.collected_at};
          expires {s.expires_at};{" "}
          <strong>{s.status === "expired" ? "EXPIRED" : "valid"}</strong>
          {s.extended_granted && "; extended presurgical validity granted"}
          {s.extended_denial_reason &&
            `; extended denied: ${s.extended_denial_reason}`}
        </p>
      ))}
      <fieldset>
        <legend>Register T&amp;S specimen</legend>
        <label>
          Collected at{" "}
          <input aria-label="T&S collected at" value={tsCollectedAt}
                 placeholder="2026-10-22T08:00:00+00:00"
                 onChange={(e) => setTsCollectedAt(e.target.value)} />
        </label>
        <label>
          Validity kind{" "}
          <select aria-label="T&S validity kind" value={tsKind}
                  onChange={(e) => setTsKind(e.target.value)}>
            <option value="standard">Standard</option>
            <option value="presurgical-extended">Presurgical (extended)</option>
          </select>
        </label>
        <label>
          <input type="checkbox" aria-label="Transfusion in lookback"
                 checked={tsTransfusion}
                 onChange={(e) => setTsTransfusion(e.target.checked)} />
          Transfusion in lookback window
        </label>
        <label>
          <input type="checkbox" aria-label="Pregnancy in lookback"
                 checked={tsPregnancy}
                 onChange={(e) => setTsPregnancy(e.target.checked)} />
          Pregnancy in lookback window
        </label>
        <button type="button"
                disabled={!loadedPatient || !tsCollectedAt.trim()}
                onClick={() => run(() => registerTsSpecimen(client, {
                  patient_ref: loadedPatient, collected_at: tsCollectedAt,
                  validity_kind: tsKind,
                  transfusion_in_lookback: tsTransfusion,
                  pregnancy_in_lookback: tsPregnancy }))}>
          Register specimen
        </button>
        {!loadedPatient && <span> Load a patient first.</span>}
      </fieldset>

      <h3>Antibody identification &amp; historical record</h3>
      {patient && patient.historical.items.length === 0 && (
        <p>No historical antibodies on file for {loadedPatient}.</p>)}
      {patient && patient.historical.items.map((h: HistoricalAntibody) => (
        <p key={h.id}>
          Historical antibody: <strong>{h.specificity}</strong>
          {h.clinically_significant
            ? " (clinically significant — the issue gate consults this record)"
            : " (not clinically significant)"}
          ; source {h.source}
          {h.first_identified_on && `; first identified ${h.first_identified_on}`}
        </p>
      ))}
      {patient && patient.workups.items.map((w: AntibodyWorkup) => (
        <p key={w.workup_id}>
          Workup {w.workup_id} ({w.method}): identified{" "}
          {w.identified_specificities.join(", ") || "none"}; ruled out{" "}
          {w.ruled_out.join(", ") || "none"} — {w.conclusion}
        </p>
      ))}
      <fieldset>
        <legend>Record antibody-identification workup</legend>
        <label>
          Method{" "}
          <select aria-label="Antibody ID method" value={abMethod}
                  disabled={config.antibody_id_methods.length === 0}
                  onChange={(e) => setAbMethod(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.antibody_id_methods} />
          </select>
        </label>
        {config.antibody_id_methods.length === 0 && (
          <span> No governed antibody-ID methods are published.</span>)}
        <label>
          Panel reactions (cell:reaction, comma-separated){" "}
          <input aria-label="Panel reactions" value={abCells}
                 placeholder="Cell 1:2+, Cell 2:0"
                 onChange={(e) => setAbCells(e.target.value)} />
        </label>
        <label>
          Identified specificities{" "}
          <input aria-label="Identified specificities" value={abIdentified}
                 placeholder="anti-E"
                 onChange={(e) => setAbIdentified(e.target.value)} />
        </label>
        <label>
          Ruled out{" "}
          <input aria-label="Ruled out specificities" value={abRuledOut}
                 placeholder="anti-K"
                 onChange={(e) => setAbRuledOut(e.target.value)} />
        </label>
        <label>
          Conclusion{" "}
          <input aria-label="Workup conclusion" value={abConclusion}
                 onChange={(e) => setAbConclusion(e.target.value)} />
        </label>
        <button type="button"
                disabled={!loadedPatient || !abMethod || !abIdentified.trim()
                          || !abConclusion.trim()}
                onClick={() => run(() => createAntibodyWorkup(client, {
                  patient_ref: loadedPatient, method: abMethod,
                  panel_reactions: splitList(abCells).map((pair) => {
                    const [cell, reaction] = pair.split(":");
                    return { cell: (cell || "").trim(),
                             reaction: (reaction || "").trim() };
                  }),
                  identified_specificities: splitList(abIdentified),
                  ruled_out: splitList(abRuledOut),
                  conclusion: abConclusion }))}>
          Record workup &amp; file historical antibody
        </button>
        {!loadedPatient && <span> Load a patient first.</span>}
      </fieldset>

      <h3>Special requirements</h3>
      {patient && patient.requirements.items.map((r: SpecialRequirement) => (
        <p key={r.id}>
          {r.requirement_code}
          {r.specificity && ` (${r.specificity})`} —{" "}
          {r.active ? "active" : "inactive"}
          {r.active && (
            <button type="button"
                    onClick={() => run(() => deactivateRequirement(
                      client, r.id, { reason: "No longer required (web)" }))}>
              Deactivate requirement {r.requirement_code}
            </button>)}
        </p>
      ))}
      <fieldset>
        <legend>Add standing requirement / attest unit attribute</legend>
        <label>
          Requirement{" "}
          <select aria-label="Requirement code" value={reqCode}
                  disabled={config.special_requirements.length === 0}
                  onChange={(e) => setReqCode(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.special_requirements} />
          </select>
        </label>
        <label>
          Requirement specificity{" "}
          <input aria-label="Requirement specificity" value={reqSpecificity}
                 placeholder="anti-E (antigen-negative only)"
                 onChange={(e) => setReqSpecificity(e.target.value)} />
        </label>
        <button type="button" disabled={!loadedPatient || !reqCode}
                onClick={() => run(() => createRequirement(client, {
                  patient_ref: loadedPatient, requirement_code: reqCode,
                  specificity: reqSpecificity || undefined }))}>
          Add requirement
        </button>
        <label>
          Unit ID for attribute{" "}
          <input aria-label="Attribute unit ID" value={attrUnit}
                 onChange={(e) => setAttrUnit(e.target.value)} />
        </label>
        <label>
          Unit attribute{" "}
          <select aria-label="Unit attribute" value={attrCode}
                  disabled={config.unit_attributes.length === 0}
                  onChange={(e) => setAttrCode(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.unit_attributes} />
          </select>
        </label>
        <label>
          Attribute specificity{" "}
          <input aria-label="Attribute specificity" value={attrSpecificity}
                 onChange={(e) => setAttrSpecificity(e.target.value)} />
        </label>
        <button type="button" disabled={!attrUnit.trim() || !attrCode}
                onClick={() => run(() => createUnitAttribute(client, {
                  unit_id: attrUnit.trim(), attribute_code: attrCode,
                  specificity: attrSpecificity || undefined }))}>
          Attest unit attribute
        </button>
      </fieldset>

      <h3>Unit reservations</h3>
      {board && board.reservations.items.length === 0 && (
        <p>No reservations recorded.</p>)}
      {board && board.reservations.items.map((r: Reservation) => (
        <p key={r.reservation_id}>
          {r.reservation_id} — unit {r.unit_id} reserved for{" "}
          {r.patient_ref}; status {r.status}; expires {r.expires_at ?? "—"}
          {r.status === "active" && (
            <button type="button"
                    disabled={!releaseReason.trim()}
                    onClick={() => run(() => releaseReservation(
                      client, r.reservation_id,
                      { reason: releaseReason }))}>
              Release reservation {r.reservation_id}
            </button>)}
        </p>
      ))}
      <fieldset>
        <legend>Reserve a crossmatched unit / release</legend>
        <label>
          Unit ID{" "}
          <input aria-label="Reservation unit ID" value={resUnit}
                 onChange={(e) => setResUnit(e.target.value)} />
        </label>
        <button type="button" disabled={!loadedPatient || !resUnit.trim()}
                onClick={() => run(() => createReservation(client, {
                  unit_id: resUnit.trim(), patient_ref: loadedPatient }))}>
          Reserve unit
        </button>
        <label>
          Release reason{" "}
          <input aria-label="Reservation release reason" value={releaseReason}
                 onChange={(e) => setReleaseReason(e.target.value)} />
        </label>
        {!releaseReason.trim() && (
          <span> A reason is required to release a reservation.</span>)}
      </fieldset>

      <h3>Thaw / preparation queue</h3>
      {board && board.preparations.items.map((p: Preparation) => (
        <div key={p.prep_id}>
          <p>
            {p.prep_id} — unit {p.unit_id} ({p.product_type}), {p.prep_kind};
            status <strong>{p.status}</strong>
            {p.expires_at && `; post-thaw expiry ${p.expires_at}`}
            {p.next_step && `; next step ${p.next_step}`}
          </p>
          {p.next_step && (
            <button type="button"
                    onClick={() => run(() => recordPrepStep(
                      client, p.prep_id, { step: p.next_step }))}>
              Record step {p.next_step} for {p.unit_id}
            </button>)}
        </div>
      ))}
      <fieldset>
        <legend>Start preparation</legend>
        <label>
          Unit ID{" "}
          <input aria-label="Preparation unit ID" value={prepUnit}
                 onChange={(e) => setPrepUnit(e.target.value)} />
        </label>
        <label>
          Preparation kind{" "}
          <select aria-label="Preparation kind" value={prepKind}
                  disabled={config.prep_kinds.length === 0}
                  onChange={(e) => setPrepKind(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.prep_kinds} />
          </select>
        </label>
        <button type="button" disabled={!prepUnit.trim() || !prepKind}
                onClick={() => run(() => createPreparation(client, {
                  unit_id: prepUnit.trim(), prep_kind: prepKind }))}>
          Start preparation
        </button>
      </fieldset>

      <h3>Reaction workups</h3>
      {board && board.reactions.items.map((w: ReactionWorkup) => (
        <div key={w.workup_id}>
          <p>
            {w.workup_id} — unit {w.unit_id}, {w.patient_ref}; status{" "}
            {w.status}; next step {w.next_step ?? "none"}
            {w.severity && `; severity ${w.severity}`}
            {w.imputability && `; imputability ${w.imputability}`}
            {w.haemovigilance_report_id &&
              `; haemovigilance report ${w.haemovigilance_report_id}`}
          </p>
          <ol>
            {w.steps.map((s) => (
              <li key={s.code}>
                {s.display}: {s.done
                  ? `done — ${s.result} (by ${s.recorded_by})`
                  : "pending"}
              </li>
            ))}
          </ol>
          {w.next_step && (
            <>
              <label>
                Result for {w.next_step}{" "}
                <input aria-label={`Reaction step result ${w.workup_id}`}
                       value={rxnResults[w.workup_id] ?? ""}
                       onChange={(e) => setRxnResults(
                         { ...rxnResults, [w.workup_id]: e.target.value })} />
              </label>
              <button type="button"
                      disabled={!(rxnResults[w.workup_id] ?? "").trim()}
                      onClick={() => run(() => recordReactionStep(
                        client, w.workup_id,
                        { step: w.next_step,
                          result: rxnResults[w.workup_id] }))}>
                Record next step
              </button>
            </>
          )}
          {!w.next_step && !w.severity && (
            <>
              <label>
                Severity{" "}
                <select aria-label={`Severity ${w.workup_id}`}
                        value={rxnSeverity[w.workup_id] ?? ""}
                        onChange={(e) => setRxnSeverity(
                          { ...rxnSeverity, [w.workup_id]: e.target.value })}>
                  <option value="">Select…</option>
                  <Options options={config.severity_grades} />
                </select>
              </label>
              <label>
                Imputability{" "}
                <select aria-label={`Imputability ${w.workup_id}`}
                        value={rxnImputability[w.workup_id] ?? ""}
                        onChange={(e) => setRxnImputability(
                          { ...rxnImputability, [w.workup_id]: e.target.value })}>
                  <option value="">Select…</option>
                  <Options options={config.imputability_grades} />
                </select>
              </label>
              <button type="button"
                      disabled={!rxnSeverity[w.workup_id]
                                || !rxnImputability[w.workup_id]}
                      onClick={() => run(() => gradeReactionWorkup(
                        client, w.workup_id,
                        { severity: rxnSeverity[w.workup_id],
                          imputability: rxnImputability[w.workup_id] }))}>
                Grade workup
              </button>
            </>
          )}
          {w.severity && !w.haemovigilance_report_id && (
            <button type="button"
                    onClick={() => run(() => fileHaemovigilance(
                      client, w.workup_id, {}))}>
              Generate haemovigilance report
            </button>)}
        </div>
      ))}
      <fieldset>
        <legend>Open reaction workup (containment first)</legend>
        <label>
          Quarantined unit ID{" "}
          <input aria-label="Reaction unit ID" value={rxnUnit}
                 onChange={(e) => setRxnUnit(e.target.value)} />
        </label>
        <button type="button" disabled={!rxnUnit.trim()}
                onClick={() => run(() => createReactionWorkup(
                  client, { unit_id: rxnUnit.trim() }))}>
          Open workup
        </button>
        <span> The unit must already be quarantined by a reaction
          report (Wave A containment) — the workup refuses otherwise.</span>
      </fieldset>

      <h3>Wastage / discard accounting</h3>
      {board && (
        <p>
          Wasted/discarded/expired total: {board.wastage.total}; by reason:{" "}
          {Object.entries(board.wastage.by_reason)
            .map(([k, v]) => `${k}: ${v}`).join(", ") || "none"}
        </p>)}
      <fieldset>
        <legend>Record disposition</legend>
        <label>
          Unit ID{" "}
          <input aria-label="Disposition unit ID" value={dispUnit}
                 onChange={(e) => setDispUnit(e.target.value)} />
        </label>
        <label>
          Disposition{" "}
          <select aria-label="Disposition" value={dispCode}
                  disabled={config.dispositions.length === 0}
                  onChange={(e) => setDispCode(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.dispositions} />
          </select>
        </label>
        <label>
          Reason code{" "}
          <select aria-label="Wastage reason code" value={dispReason}
                  disabled={config.wastage_reason_codes.length === 0}
                  onChange={(e) => setDispReason(e.target.value)}>
            <option value="">Select…</option>
            <Options options={config.wastage_reason_codes} />
          </select>
        </label>
        <button type="button" disabled={!dispUnit.trim() || !dispCode}
                onClick={() => run(() => recordDisposition(client, {
                  unit_id: dispUnit.trim(), disposition: dispCode,
                  reason_code: dispReason || undefined }))}>
          Record disposition
        </button>
      </fieldset>
    </section>
  );
}
