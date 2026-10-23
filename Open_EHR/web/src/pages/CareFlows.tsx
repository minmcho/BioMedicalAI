import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refText } from "../api/display";
import {
  ECONSULT_SPECIALTY_FALLBACK,
  addEpisodeContribution,
  answerEConsult,
  assignCoordinator,
  closeEConsult,
  coordinatorForPatient,
  coordinatorPanel,
  createEConsult,
  createEpisode,
  createInfectionCase,
  episodeAssembly,
  infectionDefinitions,
  infectionRates,
  listEConsults,
  listEpisodes,
  listInfectionCases,
  listPrecautions,
  scanEConsultTriggers,
  ssiFeed,
  stewardshipReferral,
  transitionInfectionCase,
  type EConsultItem,
} from "../api/flows";
import type { Role } from "../api/types";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

export interface FlowPanelProps {
  client: ApiClient;
  role?: Role;
}

export function flowErrorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Shared one-action-at-a-time runner for the Phase 15 panels: surfaces
 *  the server's error text verbatim (e.g. a staffing-overlap 409) and
 *  bumps the panel's refresh tick on success. */
export function useFlowActions(onDone: () => void) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  async function perform(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      onDone();
    } catch (err) {
      setActionError(`${label} failed: ${flowErrorText(err)}`);
    } finally {
      setBusy(false);
    }
  }
  return { busy, notice, actionError, perform };
}

export function stateTone(state: string): StatusTone {
  switch (state) {
    case "open":
    case "reported":
      return "warn";
    case "answered":
    case "active":
    case "confirmed":
      return "ok";
    case "under-review":
      return "info";
    default:
      return "neutral";
  }
}

export function StateBadge({ state }: { state: string }) {
  return <StatusBadge tone={stateTone(state)}>{state}</StatusBadge>;
}

function Notices({ notice, actionError }: { notice: string | null; actionError: string | null }) {
  return (
    <>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
    </>
  );
}

// ---------------------------------------------------------------------------
// eConsults (BD-27) — advice-only consults, answered in chart
// ---------------------------------------------------------------------------

const ECONSULT_STATES = ["all", "open", "answered", "closed", "cancelled"];

export function EConsultsPanel({ client }: FlowPanelProps) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [stateFilter, setStateFilter] = useState("all");
  const [ask, setAsk] = useState({ patient: "", specialty: "endocrinology", question: "" });
  const [advice, setAdvice] = useState<Record<string, string>>({});
  const [needsVisit, setNeedsVisit] = useState<Record<string, boolean>>({});
  const [scanCreated, setScanCreated] = useState<EConsultItem[] | null>(null);

  const listState = useAsync(() => listEConsults(client), [client, tick]);
  const items = (listState.data?.items ?? []).filter(
    (e) => stateFilter === "all" || e.state === stateFilter,
  );

  return (
    <div>
      <h3>eConsults — advice-only consults</h3>
      <p className="hint">
        A specialist answers in chart; no visit is created. An answer
        flagged “needs visit” creates a real draft referral
        (ServiceRequest) from the eConsult. Rule-triggered drafts from
        the trigger scan cite their triggering Observation.
      </p>
      <Notices notice={notice} actionError={actionError} />

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!ask.patient.trim() || !ask.question.trim()) return;
          void perform("eConsult asked", async () => {
            await createEConsult(client, {
              patient: ask.patient.trim(),
              specialty: ask.specialty,
              question: ask.question.trim(),
            });
            setAsk({ ...ask, question: "" });
          });
        }}
      >
        <h4>Ask an eConsult</h4>
        <label>
          Patient{" "}
          <input aria-label="eConsult patient" value={ask.patient}
            placeholder="Patient/…"
            onChange={(e) => setAsk({ ...ask, patient: e.target.value })} />
        </label>
        <label>
          Specialty{" "}
          <select aria-label="eConsult specialty" value={ask.specialty}
            onChange={(e) => setAsk({ ...ask, specialty: e.target.value })}>
            {ECONSULT_SPECIALTY_FALLBACK.map((s) => (
              <option key={s.code} value={s.code}>{s.display}</option>
            ))}
          </select>
        </label>
        <label>
          Question{" "}
          <input aria-label="eConsult question" value={ask.question}
            onChange={(e) => setAsk({ ...ask, question: e.target.value })} />
        </label>
        <button type="submit" disabled={busy}>Ask eConsult</button>
      </form>

      <div className="row-actions">
        <button
          type="button"
          disabled={busy}
          onClick={() =>
            void perform("Trigger scan", async () => {
              const result = await scanEConsultTriggers(client);
              setScanCreated(result.created);
            })}
        >
          Run trigger scan
        </button>
        <label>
          Worklist state{" "}
          <select aria-label="eConsult state filter" value={stateFilter}
            onChange={(e) => setStateFilter(e.target.value)}>
            {ECONSULT_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
      </div>
      {scanCreated && (
        <div>
          <h4>Trigger scan created {scanCreated.length} draft(s)</h4>
          {scanCreated.length === 0 && (
            <Empty label="The scan created no new eConsults (idempotent while one is open)." />
          )}
          <ul className="board-list">
            {scanCreated.map((e) => (
              <li key={e.id} className="board-card">
                <strong>{e.specialty}</strong> for {refText(e.patient_ref)} — trigger{" "}
                {e.trigger_id ?? "—"}
                {e.triggering_ref ? ` · citing ${e.triggering_ref}` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {listState.loading && <Loading label="Loading eConsults…" />}
      {listState.error != null && <ErrorView error={listState.error} />}
      {listState.data && items.length === 0 && <Empty label="No eConsults in this state." />}
      <ul className="board-list">
        {items.map((e) => (
          <li key={e.id} className="board-card">
            <div>
              <strong>{e.specialty}</strong>
              <StateBadge state={e.state} />
              {e.sla_breached && <StatusBadge tone="bad">SLA breached</StatusBadge>}
              {e.trigger_id && <StatusBadge tone="info">rule-triggered: {e.trigger_id}</StatusBadge>}
              {e.needs_visit === true && (
                <StatusBadge tone="warn">
                  Needs visit{e.referral_ref ? ` — referral ${e.referral_ref}` : ""}
                </StatusBadge>
              )}
              {e.needs_visit === false && <StatusBadge tone="neutral">No visit needed</StatusBadge>}
            </div>
            <div className="hint">
              {refText(e.patient_ref)} · asked by {e.requester}
              {e.sla_hours ? ` · SLA ${e.sla_hours}h` : ""}
            </div>
            <div>{e.question}</div>
            {e.answer_text && (
              <div className="hint">
                Answer by {e.answered_by ?? "—"}: {e.answer_text}
                {e.recommendations ? ` Recommendations: ${e.recommendations}` : ""}
              </div>
            )}
            {e.state === "open" && (
              <div className="row-actions">
                <input
                  aria-label={`Advice for ${e.id}`}
                  placeholder="Specialist advice (in chart)"
                  value={advice[e.id] ?? ""}
                  onChange={(ev) =>
                    setAdvice((prev) => ({ ...prev, [e.id]: ev.target.value }))}
                />
                <label>
                  <input
                    type="checkbox"
                    aria-label={`Needs visit for ${e.id}`}
                    checked={needsVisit[e.id] ?? false}
                    onChange={(ev) =>
                      setNeedsVisit((prev) => ({ ...prev, [e.id]: ev.target.checked }))}
                  />{" "}
                  Needs visit
                </label>
                <button
                  type="button"
                  disabled={busy || !(advice[e.id] ?? "").trim()}
                  onClick={() =>
                    void perform("eConsult answered", () =>
                      answerEConsult(client, e.id, {
                        advice: (advice[e.id] ?? "").trim(),
                        needs_visit: needsVisit[e.id] ?? false,
                      }))}
                >
                  Answer eConsult
                </button>
              </div>
            )}
            {e.state === "answered" && (
              <div className="row-actions">
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void perform("eConsult closed", () => closeEConsult(client, e.id))}
                >
                  Close eConsult
                </button>
              </div>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Multispecialty episodes (BD-25) — assembly view
// ---------------------------------------------------------------------------

export function EpisodesPanel({ client }: FlowPanelProps) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [form, setForm] = useState({ patient: "", title: "" });
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [contribution, setContribution] = useState({ specialty: "cardiology", summary: "" });

  const listState = useAsync(() => listEpisodes(client), [client, tick]);
  const assemblyState = useAsync(
    () => (selectedId ? episodeAssembly(client, selectedId) : Promise.resolve(null)),
    [client, selectedId, tick],
  );
  const assembly = assemblyState.data;

  return (
    <div>
      <h3>Multispecialty episodes</h3>
      <p className="hint">
        One episode per case spanning specialties (own-table record —
        FHIR EpisodeOfCare is not in the supported set). The assembly
        view is the whole coordinated plan: coordinator, linked
        references and every specialty's contribution.
      </p>
      <Notices notice={notice} actionError={actionError} />

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!form.patient.trim() || !form.title.trim()) return;
          void perform("Episode created", async () => {
            await createEpisode(client, {
              patient: form.patient.trim(),
              title: form.title.trim(),
            });
            setForm({ patient: "", title: "" });
          });
        }}
      >
        <h4>New episode</h4>
        <label>
          Patient{" "}
          <input aria-label="Episode patient" value={form.patient}
            placeholder="Patient/…"
            onChange={(e) => setForm({ ...form, patient: e.target.value })} />
        </label>
        <label>
          Title{" "}
          <input aria-label="Episode title" value={form.title}
            onChange={(e) => setForm({ ...form, title: e.target.value })} />
        </label>
        <button type="submit" disabled={busy}>Create episode</button>
      </form>

      {listState.loading && <Loading label="Loading episodes…" />}
      {listState.error != null && <ErrorView error={listState.error} />}
      {listState.data && listState.data.items.length === 0 && (
        <Empty label="No episodes recorded." />
      )}
      <ul className="board-list">
        {listState.data?.items.map((ep) => (
          <li key={ep.id} className="board-card">
            <div>
              <strong>{ep.title}</strong>
              <StateBadge state={ep.status} />
            </div>
            <div className="hint">
              {refText(ep.patient_ref)}
              {ep.coordinator_username ? ` · coordinator ${ep.coordinator_username}` : ""}
            </div>
            <div className="row-actions">
              <button type="button" onClick={() => setSelectedId(ep.id)}>
                View assembly
              </button>
            </div>
          </li>
        ))}
      </ul>

      {selectedId && assemblyState.loading && <Loading label="Loading episode assembly…" />}
      {selectedId && assemblyState.error != null && <ErrorView error={assemblyState.error} />}
      {assembly && (
        <div>
          <h4>Assembly — {assembly.episode.title}</h4>
          <p className="hint">
            Coordinator:{" "}
            {assembly.coordinator
              ? `${assembly.coordinator.name ?? assembly.coordinator.username ?? "—"}` +
                `${assembly.coordinator.role_label ? ` (${assembly.coordinator.role_label})` : ""}`
              : "none recorded"}
            {" · "}Status: {assembly.episode.status}
          </p>
          {assembly.links.length > 0 && (
            <>
              <h5>Linked references</h5>
              <ul className="board-list">
                {assembly.links.map((l, i) => (
                  <li key={i} className="board-card">
                    <span className="badge">{l.type ?? "link"}</span> {l.ref ?? l.reference}
                  </li>
                ))}
              </ul>
            </>
          )}
          <h5>Contributions by specialty</h5>
          {Object.keys(assembly.contributions_by_specialty).length === 0 && (
            <Empty label="No specialty contributions yet." />
          )}
          {Object.entries(assembly.contributions_by_specialty).map(([specialty, rows]) => (
            <div key={specialty}>
              <h6>{specialty}</h6>
              <ul className="board-list">
                {rows.map((c) => (
                  <li key={c.id} className="board-card">
                    <div>{c.summary}</div>
                    <div className="hint">
                      by {c.author}
                      {c.linked_refs && c.linked_refs.length > 0
                        ? ` · ${c.linked_refs.join(", ")}`
                        : ""}
                    </div>
                  </li>
                ))}
              </ul>
            </div>
          ))}
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (!contribution.summary.trim()) return;
              void perform("Contribution added", async () => {
                await addEpisodeContribution(client, assembly.episode.id, {
                  specialty: contribution.specialty,
                  summary: contribution.summary.trim(),
                });
                setContribution({ ...contribution, summary: "" });
              });
            }}
          >
            <h5>Add a contribution</h5>
            <label>
              Specialty{" "}
              <select aria-label="Contribution specialty" value={contribution.specialty}
                onChange={(e) =>
                  setContribution({ ...contribution, specialty: e.target.value })}>
                {ECONSULT_SPECIALTY_FALLBACK.map((s) => (
                  <option key={s.code} value={s.code}>{s.display}</option>
                ))}
              </select>
            </label>
            <label>
              Summary{" "}
              <input aria-label="Contribution summary" value={contribution.summary}
                onChange={(e) =>
                  setContribution({ ...contribution, summary: e.target.value })} />
            </label>
            <button type="submit" disabled={busy}>Add contribution</button>
          </form>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Coordinating physician (BD-24) — my panel + assignment history
// ---------------------------------------------------------------------------

export function CoordinatorPanel({ client }: FlowPanelProps) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [panelUserInput, setPanelUserInput] = useState("");
  const [panelUser, setPanelUser] = useState<string | null>(null);
  const [patientInput, setPatientInput] = useState("");
  const [patientRef, setPatientRef] = useState<string | null>(null);
  const [reassign, setReassign] = useState({ staff: "", roleLabel: "Coordinating physician" });

  const panelState = useAsync(
    () => (panelUser ? coordinatorPanel(client, panelUser) : Promise.resolve(null)),
    [client, panelUser, tick],
  );
  const patientState = useAsync(
    () => (patientRef ? coordinatorForPatient(client, patientRef) : Promise.resolve(null)),
    [client, patientRef, tick],
  );
  const panel = panelState.data;
  const patientCoord = patientState.data;

  return (
    <div>
      <h3>Coordinator panels</h3>
      <p className="hint">
        A coordinator's panel is every patient they currently
        coordinate, with the active episode and attending of record.
        Assignments are history: reassigning supersedes the previous
        record — it is never edited or deleted.
      </p>
      <Notices notice={notice} actionError={actionError} />

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (panelUserInput.trim()) setPanelUser(panelUserInput.trim());
        }}
      >
        <h4>My panel</h4>
        <label>
          Coordinator username{" "}
          <input aria-label="Coordinator username" value={panelUserInput}
            onChange={(e) => setPanelUserInput(e.target.value)} />
        </label>
        <button type="submit" disabled={busy}>Load my panel</button>
      </form>
      {panelState.loading && <Loading label="Loading coordinator panel…" />}
      {panelState.error != null && <ErrorView error={panelState.error} />}
      {panel && (
        <>
          <p className="hint">
            Panel for {panel.staff_name ?? panel.staff_username} — {panel.total} patient(s).
          </p>
          {panel.items.length === 0 && <Empty label="No patients on this panel." />}
          <ul className="board-list">
            {panel.items.map((item) => (
              <li key={item.patient_ref} className="board-card">
                <div>
                  <strong>{refText(item.patient_ref)}</strong>
                  {item.active_episode && (
                    <span className="badge">episode: {item.active_episode.title}</span>
                  )}
                </div>
                <div className="hint">
                  {item.coordinator_role ?? ""}
                  {item.attending
                    ? ` · attending ${item.attending_name ?? item.attending}`
                    : " · no attending of record"}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (patientInput.trim()) setPatientRef(patientInput.trim());
        }}
      >
        <h4>Patient coordinator & history</h4>
        <label>
          Patient{" "}
          <input aria-label="Coordinator patient" value={patientInput}
            placeholder="Patient/…"
            onChange={(e) => setPatientInput(e.target.value)} />
        </label>
        <button type="submit" disabled={busy}>Load assignment history</button>
      </form>
      {patientState.error != null && <ErrorView error={patientState.error} />}
      {patientCoord && (
        <>
          <p className="hint">
            Current coordinator:{" "}
            {patientCoord.coordinator
              ? `${patientCoord.coordinator.staff_name ?? patientCoord.coordinator.staff_username} (${patientCoord.coordinator.role_label})`
              : "none assigned"}
          </p>
          <ul className="board-list">
            {patientCoord.history.map((h) => (
              <li key={h.assignment_id} className="board-card">
                <div>
                  <strong>{h.staff_name ?? h.staff_username}</strong>
                  <StateBadge state={h.active ? "active" : "superseded"} />
                  <span className="badge">{h.role_label}</span>
                </div>
                <div className="hint">
                  assigned {h.created_at ?? "—"}
                  {h.superseded_at ? ` · superseded ${h.superseded_at}` : ""}
                  {h.assigned_by ? ` · by ${h.assigned_by}` : ""}
                </div>
              </li>
            ))}
          </ul>
          <form
            className="inline-form"
            onSubmit={(e) => {
              e.preventDefault();
              if (!reassign.staff.trim() || !patientRef) return;
              void perform("Coordinator reassigned", async () => {
                await assignCoordinator(client, patientRef, {
                  staff_username: reassign.staff.trim(),
                  role_label: reassign.roleLabel.trim() || undefined,
                });
                setReassign({ ...reassign, staff: "" });
              });
            }}
          >
            <h5>Reassign coordinator (supersedes the current assignment)</h5>
            <label>
              New coordinator (staff username or id){" "}
              <input aria-label="New coordinator" value={reassign.staff}
                onChange={(e) => setReassign({ ...reassign, staff: e.target.value })} />
            </label>
            <label>
              Role label{" "}
              <input aria-label="Coordinator role label" value={reassign.roleLabel}
                onChange={(e) => setReassign({ ...reassign, roleLabel: e.target.value })} />
            </label>
            <button type="submit" disabled={busy}>Reassign coordinator</button>
          </form>
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Infection prevention — HAI surveillance, SSI feed, stewardship
// ---------------------------------------------------------------------------

export function InfectionPreventionPanel({ client }: FlowPanelProps) {
  const [tick, setTick] = useState(0);
  const { busy, notice, actionError, perform } = useFlowActions(() => setTick((t) => t + 1));
  const [caseForm, setCaseForm] = useState({ type: "ssi", patient: "", onset: "", unit: "" });
  const [referralTask, setReferralTask] = useState<Record<string, string>>({});

  const definitionsState = useAsync(() => infectionDefinitions(client), [client]);
  const casesState = useAsync(() => listInfectionCases(client), [client, tick]);
  const feedState = useAsync(() => ssiFeed(client), [client, tick]);
  const ratesState = useAsync(() => infectionRates(client), [client, tick]);
  const precautionsState = useAsync(() => listPrecautions(client), [client, tick]);

  const definitions = definitionsState.data;
  const haiConcepts = definitions?.hai_definitions.concepts ?? [];
  const stewardshipDisplay =
    definitions?.hai_definitions.stewardship?.display ??
    "Pharmacy — antimicrobial stewardship team";
  const feed = feedState.data;

  return (
    <div>
      <h3>Infection prevention</h3>
      <p className="hint">
        HAI surveillance against the published hai-definitions config.
        Stewardship referrals are real FHIR Tasks addressed to{" "}
        {stewardshipDisplay} (the audience named in that config).
        Rates are exact arithmetic over confirmed cases and recorded
        device-days; a type with no device-day denominator reports no
        rate, with the reason shown.
      </p>
      <Notices notice={notice} actionError={actionError} />

      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          if (!caseForm.patient.trim() || !caseForm.onset) return;
          void perform("Infection case recorded", async () => {
            await createInfectionCase(client, {
              type: caseForm.type,
              patient: caseForm.patient.trim(),
              onset_date: caseForm.onset,
              unit: caseForm.unit.trim() || undefined,
            });
            setCaseForm({ ...caseForm, patient: "" });
          });
        }}
      >
        <h4>Record a surveillance case</h4>
        <label>
          HAI type{" "}
          <select aria-label="HAI type" value={caseForm.type}
            onChange={(e) => setCaseForm({ ...caseForm, type: e.target.value })}>
            {(haiConcepts.length > 0
              ? haiConcepts
              : [{ code: "ssi", display: "Surgical site infection (SSI)" }]
            ).map((c) => (
              <option key={c.code} value={c.code}>{c.display}</option>
            ))}
          </select>
        </label>
        <label>
          Patient{" "}
          <input aria-label="Infection case patient" value={caseForm.patient}
            placeholder="Patient/…"
            onChange={(e) => setCaseForm({ ...caseForm, patient: e.target.value })} />
        </label>
        <label>
          Onset date{" "}
          <input aria-label="Infection onset date" type="date" value={caseForm.onset}
            onChange={(e) => setCaseForm({ ...caseForm, onset: e.target.value })} />
        </label>
        <label>
          Unit{" "}
          <input aria-label="Infection case unit" value={caseForm.unit}
            onChange={(e) => setCaseForm({ ...caseForm, unit: e.target.value })} />
        </label>
        <button type="submit" disabled={busy}>Record case</button>
      </form>

      <h4>Surveillance cases</h4>
      {casesState.loading && <Loading label="Loading infection cases…" />}
      {casesState.error != null && <ErrorView error={casesState.error} />}
      {casesState.data && casesState.data.items.length === 0 && (
        <Empty label="No infection cases recorded." />
      )}
      <ul className="board-list">
        {casesState.data?.items.map((c) => (
          <li key={c.id} className="board-card">
            <div>
              <strong>{c.hai_type}</strong>
              <StateBadge state={c.status} />
            </div>
            <div className="hint">
              {refText(c.patient_ref)} · onset {c.onset_date}
              {c.unit ? ` · ${c.unit}` : ""} · source:{" "}
              {c.source_ref ? c.source_ref : "manual intake (POST /infection/cases)"}
            </div>
            {c.note && <div className="hint">Review note: {c.note}</div>}
            <div className="row-actions">
              {c.status === "reported" && (
                <button type="button" disabled={busy}
                  onClick={() =>
                    void perform("Case under review", () =>
                      transitionInfectionCase(client, c.id, { to_status: "under-review" }))}>
                  Start review
                </button>
              )}
              {c.status === "under-review" && (
                <button type="button" disabled={busy}
                  onClick={() =>
                    void perform("Case confirmed", () =>
                      transitionInfectionCase(client, c.id, { to_status: "confirmed" }))}>
                  Confirm case
                </button>
              )}
              {c.stewardship_task_id || referralTask[c.id] ? (
                <span className="hint">
                  Stewardship Task {c.stewardship_task_id ?? referralTask[c.id]} — referred
                  to {stewardshipDisplay}
                </span>
              ) : (
                <button type="button" disabled={busy}
                  onClick={() =>
                    void perform("Stewardship referral created", async () => {
                      const result = await stewardshipReferral(client, c.id);
                      setReferralTask((prev) => ({ ...prev, [c.id]: result.task.id }));
                    })}>
                  Refer to stewardship
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>

      <h4>SSI surveillance feed</h4>
      {feedState.error != null && <ErrorView error={feedState.error} />}
      {feed && (
        <>
          <p className="hint">
            Source: {feed.source} — {feed.note}
          </p>
          {feed.items.length === 0 && (
            <Empty label="No SSI cases in the surgery-outcomes feed." />
          )}
          <ul className="board-list">
            {feed.items.map((item, i) => (
              <li key={`${item.source_ref}-${i}`} className="board-card">
                <div>
                  <strong>ssi</strong>
                  <StateBadge state={item.status} />
                </div>
                <div className="hint">
                  {refText(item.patient_ref)} · onset {item.onset_date ?? "—"} · source:{" "}
                  {item.source_ref}
                </div>
              </li>
            ))}
          </ul>
        </>
      )}

      <h4>Rates per 1,000 device-days</h4>
      {ratesState.error != null && <ErrorView error={ratesState.error} />}
      <ul className="board-list">
        {ratesState.data?.items.map((r) => (
          <li key={r.type} className="board-card">
            <div>
              <strong>{r.display ?? r.type}</strong>
              <span className="badge">{r.confirmed_cases} confirmed</span>
            </div>
            <div className="hint">
              {r.rate_per_1000 != null
                ? `${r.rate_per_1000} per 1,000 ${r.denominator_type} (${r.device_days} device-days)`
                : `No rate — ${r.reason ?? "not device-day rated"}`}
            </div>
          </li>
        ))}
      </ul>

      <h4>Active isolation precautions</h4>
      {precautionsState.error != null && <ErrorView error={precautionsState.error} />}
      {precautionsState.data && precautionsState.data.items.length === 0 && (
        <Empty label="No active isolation precautions." />
      )}
      <ul className="board-list">
        {precautionsState.data?.items.map((p) => (
          <li key={p.id} className="board-card">
            <div>
              <strong>{p.precaution_type}</strong>
              <StateBadge state={p.status} />
            </div>
            <div className="hint">
              {refText(p.patient_ref)} · since {p.started_on}
              {p.unit ? ` · ${p.unit}` : ""} — {p.reason}
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
