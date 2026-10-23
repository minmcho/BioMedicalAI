import { useEffect, useState } from "react";
import { ApiError, type ApiClient, type SafetyChecklistStatus } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import type {
  FormDefinition,
  QuestionnaireAnswerItem,
  Role,
  SurgeryCaseEntry,
  SurgeryCaseRecord,
} from "../api/types";
import {
  FALLBACK_OUTCOME_CONFIG,
  FALLBACK_PACU_CONFIG,
  FALLBACK_STERILE_CONFIG,
  FALLBACK_TRAY_TYPES,
  FALLBACK_URGENCIES,
  INTRAOP_FORMS,
  assignTrayToCase,
  blocksFromPayload,
  conceptsFromPayload,
  createPreferenceCard,
  createSurgeryRequest,
  createTray,
  declineSurgeryRequest,
  approveSurgeryRequest,
  deletePreferenceCard,
  generatePickList,
  getSsiFeed,
  getSurgeryUtilisation,
  getSurgeryWaitlist,
  listImplantLots,
  listPacuScores,
  listPreferenceCards,
  listSurgeryRequests,
  listTrays,
  outcomeConfigFromPayload,
  pacuConfigFromPayload,
  recordOutcome,
  recordPacuScore,
  runSterilisationCycle,
  scheduleSurgeryRequest,
  sterileConfigFromPayload,
  submitIntraopForm,
  transitionTray,
  updatePreferenceCard,
  type CasePick,
  type ImplantLot,
  type IntraopFormKey,
  type OrBlock,
  type OutcomeConfig,
  type PacuConfig,
  type PacuScore,
  type PreferenceCard,
  type PreferenceCardItem,
  type SterileConfig,
  type SurgeryOutcome,
  type SurgeryRequestItem,
  type SurgeryUtilisation,
  type TrayInstance,
} from "../api/surgeryDepth";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { FormRenderer } from "../components/FormRenderer";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
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

/** Derive the offered next-states from a published surgery-flow
 *  payload; null when unusable (caller keeps the built-in map). */
export function nextStatesFromPayload(
  payload: unknown,
): Record<string, string[]> | null {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const transitions = (payload as { transitions?: unknown }).transitions;
  if (!Array.isArray(transitions)) return null;
  const derived: Record<string, string[]> = {};
  for (const t of transitions) {
    if (!t || typeof t !== "object") continue;
    const { from, to } = t as { from?: unknown; to?: unknown };
    if (typeof from !== "string" || typeof to !== "string") continue;
    (derived[from] ??= []).push(to);
  }
  return Object.keys(derived).length > 0 ? derived : null;
}

type RecordFormKey = "preop-checklist" | "anaesthesia-record" | "anaesthesia-vitals";

const RECORD_FORMS: Array<{ key: RecordFormKey; label: string }> = [
  { key: "preop-checklist", label: "Pre-op checklist" },
  { key: "anaesthesia-record", label: "Anaesthesia record" },
  { key: "anaesthesia-vitals", label: "Anaesthesia vitals" },
];

/** Phase 6 perioperative record for one case: the server-timestamped
 *  case timeline, the attached pre-op checklist + anaesthesia record
 *  (published Studio forms, rendered by the shared FormRenderer), and
 *  the anaesthesia vitals series. */
function CaseRecordPanel({
  client,
  caseId,
  canWork,
}: {
  client: ApiClient;
  caseId: string;
  canWork: boolean;
}) {
  const [record, setRecord] = useState<SurgeryCaseRecord | null>(null);
  const [recordError, setRecordError] = useState<string | null>(null);
  const [openForm, setOpenForm] = useState<RecordFormKey | null>(null);
  const [formDef, setFormDef] = useState<FormDefinition | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [reloadTick, setReloadTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    client
      .surgeryCaseRecord(caseId)
      .then((rec) => {
        if (!cancelled) setRecord(rec);
      })
      .catch((err: unknown) => {
        if (!cancelled) setRecordError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client, caseId, reloadTick]);

  useEffect(() => {
    let cancelled = false;
    setFormDef(null);
    if (!openForm || typeof client.effectiveConfig !== "function") return;
    client
      .effectiveConfig("form", openForm)
      .then((cfg) => {
        if (!cancelled) setFormDef(cfg.payload as unknown as FormDefinition);
      })
      .catch(() => {
        if (!cancelled) setFormDef(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, openForm]);

  async function submitForm(answers: QuestionnaireAnswerItem[]) {
    if (!openForm) return;
    if (openForm === "preop-checklist") {
      await client.submitPreopChecklist(caseId, answers);
    } else if (openForm === "anaesthesia-record") {
      await client.submitAnaesthesia(caseId, answers);
    } else {
      await client.submitAnaesthesiaVitals(caseId, answers);
    }
    setNotice(`${RECORD_FORMS.find((f) => f.key === openForm)?.label} recorded.`);
    setOpenForm(null);
    setReloadTick((t) => t + 1);
  }

  const timeline = record?.case.timeline ?? [];
  const vitals = record?.anaesthesia_vitals;
  return (
    <div className="case-record">
      {recordError && (
        <div className="state state-error" role="alert">{recordError}</div>
      )}
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {timeline.length > 0 && (
        <ol className="case-timeline" aria-label={`Timeline for ${caseId}`}>
          {timeline.map((event, i) => (
            <li key={i}>
              <strong>{event.event}</strong>{" "}
              <span className="hint">
                {event.at ? new Date(event.at).toLocaleString() : ""}
                {event.by ? ` · ${event.by}` : ""}
              </span>
            </li>
          ))}
        </ol>
      )}
      {record?.preop_checklist && (
        <p className="hint">
          Pre-op checklist completed
          {record.preop_checklist.meta.completedAt
            ? ` ${new Date(String(record.preop_checklist.meta.completedAt)).toLocaleString()}`
            : ""}
          .
        </p>
      )}
      {record?.anaesthesia && (
        <p className="hint">
          Anaesthesia record on file
          {record.anaesthesia.meta.summary?.agents
            ? ` — agents: ${String(record.anaesthesia.meta.summary.agents)}`
            : ""}
          .
        </p>
      )}
      {vitals && vitals.entries.length > 0 && (
        <div>
          <strong>Anaesthesia vitals ({vitals.entries.length})</strong>
          <ul>
            {vitals.entries.map((entry) => (
              <li key={entry.id} className="hint">
                {entry.authored ? new Date(entry.authored).toLocaleTimeString() : ""}{" "}
                {Object.entries(entry.answers)
                  .map(([k, v]) => `${k}: ${String(v.value)}`)
                  .join(" · ")}
              </li>
            ))}
          </ul>
        </div>
      )}
      {canWork && (
        <div className="row-actions">
          {RECORD_FORMS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setOpenForm(openForm === f.key ? null : f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
      )}
      {canWork && openForm && (
        formDef ? (
          <FormRenderer
            form={formDef}
            submitLabel={`Submit ${RECORD_FORMS.find((f) => f.key === openForm)?.label}`}
            onSubmit={submitForm}
          />
        ) : (
          <p className="hint">Loading the published form…</p>
        )
      )}
    </div>
  );
}

type SafetySection = "sign-in" | "time-out" | "sign-out";

const SAFETY_SECTIONS: Array<{ key: SafetySection; label: string }> = [
  { key: "sign-in", label: "Sign In" },
  { key: "time-out", label: "Time Out" },
  { key: "sign-out", label: "Sign Out" },
];

const SAFETY_PREFIX: Record<SafetySection, string> = {
  "sign-in": "signin-",
  "time-out": "timeout-",
  "sign-out": "signout-",
};

/** The published form's slice for one checklist section: the section
 *  group when the form is grouped (the seeded shape), else the flat
 *  items carrying the section's code prefix. */
export function safetySectionForm(
  form: FormDefinition,
  section: SafetySection,
): FormDefinition | null {
  const group = (form.item ?? []).find((i) => i.linkId === section);
  if (group && Array.isArray(group.item) && group.item.length > 0) {
    return { resourceType: "Questionnaire", title: group.text ?? section, item: group.item };
  }
  const items = (form.item ?? []).filter((i) =>
    i.linkId.startsWith(SAFETY_PREFIX[section]));
  return items.length > 0
    ? { resourceType: "Questionnaire", title: form.title, item: items }
    : null;
}

/** Depth pass (Workstream 2): the WHO surgical safety checklist panel
 *  for one case — the three sections with Done/Pending labelled
 *  status, and the governed form for the next incomplete section
 *  (rendered by the shared FormRenderer, like the other case forms).
 *  When the published surgery workflow enables the gate, the server
 *  refuses in-op without Time Out and done without Sign Out (409) —
 *  that refusal surfaces in the page's action-error area. */
function SafetyChecklistPanel({
  client,
  caseId,
  canWork,
  initial,
  onRecorded,
}: {
  client: ApiClient;
  caseId: string;
  canWork: boolean;
  initial?: SafetyChecklistStatus;
  onRecorded: () => void;
}) {
  const [status, setStatus] = useState<SafetyChecklistStatus>(
    initial ?? { "sign-in": false, "time-out": false, "sign-out": false },
  );
  useEffect(() => {
    if (initial) setStatus(initial);
  }, [initial]);
  const nextSection = SAFETY_SECTIONS.find((s) => !status[s.key])?.key ?? null;
  const [openSection, setOpenSection] = useState<SafetySection | null>(null);
  const [formDef, setFormDef] = useState<FormDefinition | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setFormDef(null);
    if (!openSection || typeof client.effectiveConfig !== "function") return;
    client
      .effectiveConfig("form", "surgical-safety-checklist")
      .then((cfg) => {
        if (!cancelled) {
          setFormDef(safetySectionForm(
            cfg.payload as unknown as FormDefinition, openSection));
        }
      })
      .catch(() => {
        if (!cancelled) setFormDef(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, openSection]);

  async function submitSection(answers: QuestionnaireAnswerItem[]) {
    if (!openSection) return;
    setFormError(null);
    try {
      const out = await client.submitSafetyChecklist(caseId, openSection, answers);
      setStatus(out.safety_checklist);
      setNotice(`${SAFETY_SECTIONS.find((s) => s.key === openSection)?.label} recorded.`);
      setOpenSection(null);
      onRecorded();
    } catch (err) {
      setFormError(errorText(err));
    }
  }

  const activeSection = openSection ?? nextSection;
  return (
    <div className="case-record">
      <strong>WHO surgical safety checklist</strong>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {formError && <div className="state state-error" role="alert">{formError}</div>}
      <ul>
        {SAFETY_SECTIONS.map((s) => (
          <li key={s.key}>
            {s.label}:{" "}
            <span style={{ color: status[s.key] ? "#0b6e4f" : "#b45309" }}>
              {status[s.key] ? "Done" : "Pending"}
            </span>
          </li>
        ))}
      </ul>
      {canWork && nextSection && (
        <div className="row-actions">
          <button
            type="button"
            onClick={() =>
              setOpenSection(openSection === nextSection ? null : nextSection)}
          >
            {`Record ${SAFETY_SECTIONS.find((s) => s.key === nextSection)?.label}`}
          </button>
        </div>
      )}
      {canWork && activeSection && openSection === activeSection && (
        formDef ? (
          <FormRenderer
            form={formDef}
            submitLabel={`Submit ${SAFETY_SECTIONS.find((s) => s.key === activeSection)?.label}`}
            onSubmit={submitSection}
          />
        ) : (
          <p className="hint">Loading the published form…</p>
        )
      )}
    </div>
  );
}

/** Published-config payload for one Studio object, or null when the
 *  fetch is unavailable/fails (callers keep their built-in fallback). */
function useEffectivePayload(
  client: ApiClient,
  kind: string,
  key: string,
): Record<string, unknown> | null {
  const [payload, setPayload] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    let cancelled = false;
    if (typeof client.effectiveConfig !== "function") return;
    client
      .effectiveConfig(kind as never, key)
      .then((cfg) => {
        if (!cancelled) setPayload(cfg.payload as Record<string, unknown>);
      })
      .catch(() => {
        if (!cancelled) setPayload(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, kind, key]);
  return payload;
}

function urgencyTone(urgency: string): StatusTone {
  if (urgency === "emergent") return "bad";
  if (urgency === "urgent") return "warn";
  return "info";
}

function requestStatusTone(status: string): StatusTone {
  if (status === "approved") return "info";
  if (status === "scheduled") return "ok";
  if (status === "declined") return "bad";
  return "warn";
}

function trayStatusTone(status: string): StatusTone {
  if (status === "sterile") return "ok";
  if (status === "issued") return "info";
  if (status === "sterilising") return "warn";
  return "neutral";
}

/** Phase 15 (E): PACU scoring + case outcome for one case. The five
 *  Aldrete criteria come from the published terminology/pacu-scoring
 *  config; the total and discharge verdict are computed SERVER-side —
 *  this panel only shows what the server returns. The pacu → done
 *  gate itself is enforced on the board transition (its 409 surfaces
 *  in the page's action-error area). */
function PacuOutcomePanel({
  client,
  caseId,
  canWork,
}: {
  client: ApiClient;
  caseId: string;
  canWork: boolean;
}) {
  const pacuPayload = useEffectivePayload(client, "terminology", "pacu-scoring");
  const pacuConfig: PacuConfig =
    pacuConfigFromPayload(pacuPayload) ?? FALLBACK_PACU_CONFIG;
  const outcomePayload = useEffectivePayload(
    client, "terminology", "surgery-complications");
  const outcomeConfig: OutcomeConfig =
    outcomeConfigFromPayload(outcomePayload) ?? FALLBACK_OUTCOME_CONFIG;

  const [values, setValues] = useState<Record<string, string>>({});
  const [latest, setLatest] = useState<PacuScore | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [outStatus, setOutStatus] = useState(outcomeConfig.statuses[0] ?? "recovered");
  const [outComplications, setOutComplications] = useState<string[]>([]);
  const [outSsi, setOutSsi] = useState(false);
  const [outOnset, setOutOnset] = useState("");
  const [outOrganism, setOutOrganism] = useState("");
  const [savedOutcome, setSavedOutcome] = useState<SurgeryOutcome | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    listPacuScores(client, caseId)
      .then((res) => {
        if (!cancelled) setLatest(res.latest);
      })
      .catch(() => {
        /* no scores yet / depth unavailable — the form still works */
      });
    return () => {
      cancelled = true;
    };
  }, [client, caseId]);

  async function submitPacu() {
    setError(null);
    setNotice(null);
    const scores: Record<string, number> = {};
    for (const c of pacuConfig.criteria) {
      const raw = values[c.code];
      if (raw === undefined || raw === "") {
        setError(`Score every criterion — missing: ${c.display ?? c.code}`);
        return;
      }
      scores[c.code] = Number(raw);
    }
    setBusy(true);
    try {
      const out = await recordPacuScore(client, caseId, scores);
      setLatest(out);
      setNotice(
        `PACU score recorded: total ${out.total} of ${pacuConfig.criteria.length * 2}.`,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function submitOutcome() {
    setError(null);
    setNotice(null);
    setBusy(true);
    try {
      const out = await recordOutcome(client, caseId, {
        status: outStatus,
        complications: outComplications,
        ssi: outSsi,
        ssiOnsetDate: outSsi ? outOnset : undefined,
        organism: outSsi ? outOrganism || undefined : undefined,
      });
      setSavedOutcome(out);
      setNotice(`Outcome recorded for case ${caseId}: ${out.status}.`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="case-record">
      <strong>PACU score (modified Aldrete)</strong>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      {latest && (
        <p className="hint">
          Latest PACU total: {latest.total} (threshold {latest.threshold}) —{" "}
          {latest.passes
            ? "Passes — discharge ready"
            : "Does not pass — not discharge ready"}
          {latest.must_pass_failures.length > 0 &&
            ` · must-pass failures: ${latest.must_pass_failures
              .map((f) => `${f.code} scored ${f.scored} (minimum ${f.minimum})`)
              .join(", ")}`}
        </p>
      )}
      {canWork && (
        <div>
          {pacuConfig.criteria.map((c) => (
            <label key={c.code}>
              {c.display ?? c.code} (0–{c.max}){" "}
              <input
                type="number"
                min={0}
                max={c.max}
                aria-label={`PACU ${c.display ?? c.code}`}
                value={values[c.code] ?? ""}
                onChange={(e) =>
                  setValues((prev) => ({ ...prev, [c.code]: e.target.value }))}
              />
            </label>
          ))}
          <div className="row-actions">
            <button type="button" disabled={busy} onClick={() => void submitPacu()}>
              Record PACU score
            </button>
          </div>
        </div>
      )}

      <strong>Case outcome</strong>
      {savedOutcome && (
        <p className="hint">
          Recorded outcome: {savedOutcome.status}
          {savedOutcome.ssi
            ? ` · SSI onset ${savedOutcome.ssi_onset_date ?? ""}${
                savedOutcome.organism ? ` · organism ${savedOutcome.organism}` : ""}`
            : " · no SSI"}
          {savedOutcome.complications.length > 0 &&
            ` · complications: ${savedOutcome.complications.join(", ")}`}
        </p>
      )}
      {canWork && (
        <div>
          <label>
            Outcome status{" "}
            <select
              aria-label="Outcome status"
              value={outStatus}
              onChange={(e) => setOutStatus(e.target.value)}
            >
              {outcomeConfig.statuses.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <fieldset>
            <legend>Complications</legend>
            {outcomeConfig.complications.map((c) => (
              <label key={c.code}>
                <input
                  type="checkbox"
                  aria-label={`Complication ${c.display ?? c.code}`}
                  checked={outComplications.includes(c.code)}
                  onChange={(e) =>
                    setOutComplications((prev) =>
                      e.target.checked
                        ? [...prev, c.code]
                        : prev.filter((x) => x !== c.code))}
                />{" "}
                {c.display ?? c.code}
              </label>
            ))}
          </fieldset>
          <label>
            <input
              type="checkbox"
              aria-label="Surgical-site infection (SSI)"
              checked={outSsi}
              onChange={(e) => setOutSsi(e.target.checked)}
            />{" "}
            Surgical-site infection (SSI)
          </label>
          {outSsi && (
            <>
              <label>
                SSI onset date{" "}
                <input
                  type="date"
                  aria-label="SSI onset date"
                  value={outOnset}
                  onChange={(e) => setOutOnset(e.target.value)}
                />
              </label>
              <label>
                Organism{" "}
                <input
                  aria-label="SSI organism"
                  value={outOrganism}
                  onChange={(e) => setOutOrganism(e.target.value)}
                />
              </label>
            </>
          )}
          <div className="row-actions">
            <button type="button" disabled={busy} onClick={() => void submitOutcome()}>
              Record outcome
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

/** Phase 15 (E): the three governed intra-operative forms for one
 *  case, following the safety-checklist panel pattern — the published
 *  form rendered by the shared FormRenderer, submitted through the
 *  surgery-depth endpoint. */
function IntraopPanel({
  client,
  caseId,
  canWork,
}: {
  client: ApiClient;
  caseId: string;
  canWork: boolean;
}) {
  const [openForm, setOpenForm] = useState<IntraopFormKey | null>(null);
  const [formDef, setFormDef] = useState<FormDefinition | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [recorded, setRecorded] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;
    setFormDef(null);
    if (!openForm || typeof client.effectiveConfig !== "function") return;
    client
      .effectiveConfig("form", openForm)
      .then((cfg) => {
        if (!cancelled) setFormDef(cfg.payload as unknown as FormDefinition);
      })
      .catch(() => {
        if (!cancelled) setFormDef(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, openForm]);

  async function submitForm(answers: QuestionnaireAnswerItem[]) {
    if (!openForm) return;
    setError(null);
    try {
      await submitIntraopForm(client, caseId, openForm, answers);
      const label = INTRAOP_FORMS.find((f) => f.key === openForm)?.label;
      setRecorded((prev) => [...prev, openForm]);
      setNotice(`${label} recorded.`);
      setOpenForm(null);
    } catch (err) {
      setError(errorText(err));
    }
  }

  return (
    <div className="case-record">
      <strong>Intra-operative record</strong>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      <ul>
        {INTRAOP_FORMS.map((f) => (
          <li key={f.key}>
            {f.label}: {recorded.includes(f.key) ? "Recorded" : "Not recorded this session"}
          </li>
        ))}
      </ul>
      {canWork && (
        <div className="row-actions">
          {INTRAOP_FORMS.map((f) => (
            <button
              key={f.key}
              type="button"
              onClick={() => setOpenForm(openForm === f.key ? null : f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
      )}
      {canWork && openForm && (
        formDef ? (
          <FormRenderer
            form={formDef}
            submitLabel={`Submit ${INTRAOP_FORMS.find((f) => f.key === openForm)?.label}`}
            onSubmit={submitForm}
          />
        ) : (
          <p className="hint">Loading the published form…</p>
        )
      )}
    </div>
  );
}

/** Phase 15 (E): case requests + the surgical waitlist (approved,
 *  unscheduled — ordered server-side by urgency rank then wait). */
function RequestsPanel({
  client,
  canWork,
  rooms,
}: {
  client: ApiClient;
  canWork: boolean;
  rooms: Array<{ code: string; display?: string }>;
}) {
  const urgencyPayload = useEffectivePayload(client, "terminology", "surgery-urgency");
  const urgencies = conceptsFromPayload(urgencyPayload) ?? FALLBACK_URGENCIES;
  const [requests, setRequests] = useState<SurgeryRequestItem[]>([]);
  const [waitlist, setWaitlist] = useState<SurgeryRequestItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [declineReason, setDeclineReason] = useState("");
  const [sched, setSched] = useState({ room: "OR-1", start: "", end: "" });
  const [schedResult, setSchedResult] = useState<string | null>(null);
  const [createForm, setCreateForm] = useState({
    patientId: "", procedureCode: "", display: "", surgeon: "", urgency: "elective",
  });

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    Promise.all([listSurgeryRequests(client), getSurgeryWaitlist(client)])
      .then(([reqs, wait]) => {
        if (cancelled) return;
        setRequests(reqs.items);
        setWaitlist(wait.items);
        setSelectedId((prev) => prev ?? reqs.items[0]?.request_id ?? null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client, tick]);

  const selected = requests.find((r) => r.request_id === selectedId) ?? null;

  async function act(label: string, fn: () => Promise<unknown>) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      setTick((t) => t + 1);
    } catch (err) {
      setError(`${label} failed: ${errorText(err)}`);
    }
  }

  function renderRequestItem(r: SurgeryRequestItem) {
    return (
      <li key={r.request_id}>
        <button type="button" onClick={() => setSelectedId(r.request_id)}>
          {r.procedure_display ?? r.procedure_code} — {r.patient_ref}
        </button>{" "}
        <StatusBadge tone={urgencyTone(r.urgency)}>{r.urgency}</StatusBadge>{" "}
        <StatusBadge tone={requestStatusTone(r.status)}>{r.status}</StatusBadge>
      </li>
    );
  }

  return (
    <div>
      <h3>Requests &amp; waitlist</h3>
      <p className="hint">
        Case requests are decided here; approved requests form the surgical
        waitlist (ordered by published urgency rank, then longest wait) and
        are scheduled into an operating room — the created case reports
        whether it falls inside the surgeon&apos;s published OR block.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      <div className="master-detail">
        <div>
          <h4>All requests ({requests.length})</h4>
          {requests.length === 0
            ? <p className="hint">No case requests.</p>
            : <ul>{requests.map(renderRequestItem)}</ul>}
          <h4>Waitlist — approved, unscheduled ({waitlist.length})</h4>
          {waitlist.length === 0
            ? <p className="hint">Waitlist is empty.</p>
            : <ul>{waitlist.map(renderRequestItem)}</ul>}
        </div>
        <div>
          <h4>Request detail</h4>
          {!selected && <p className="hint">Select a request to see its detail.</p>}
          {selected && (
            <div>
              <p>
                <strong>{selected.procedure_display ?? selected.procedure_code}</strong>{" "}
                <StatusBadge tone={urgencyTone(selected.urgency)}>
                  {selected.urgency}
                </StatusBadge>{" "}
                <StatusBadge tone={requestStatusTone(selected.status)}>
                  {selected.status}
                </StatusBadge>
              </p>
              <p className="hint">
                Patient {selected.patient_ref} · Procedure {selected.procedure_code}
                {" "}· Surgeon {selected.surgeon || "—"} · Waiting{" "}
                {selected.wait_hours ?? 0} h
                {selected.case_id ? ` · Case ${selected.case_id}` : ""}
                {selected.decline_reason
                  ? ` · Decline reason: ${selected.decline_reason}`
                  : ""}
              </p>
              {canWork && selected.status === "requested" && (
                <div className="row-actions">
                  <button
                    type="button"
                    onClick={() =>
                      void act("Request approved", () =>
                        approveSurgeryRequest(client, selected.request_id))}
                  >
                    Approve request
                  </button>
                  <label>
                    Decline reason{" "}
                    <input
                      aria-label="Decline reason"
                      value={declineReason}
                      onChange={(e) => setDeclineReason(e.target.value)}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={!declineReason.trim()}
                    onClick={() =>
                      void act("Request declined", () =>
                        declineSurgeryRequest(
                          client, selected.request_id, declineReason.trim()))}
                  >
                    Decline request
                  </button>
                </div>
              )}
              {canWork && selected.status === "approved" && (
                <div className="row-actions">
                  <label>
                    Schedule room{" "}
                    <select
                      aria-label="Schedule room"
                      value={sched.room}
                      onChange={(e) => setSched({ ...sched, room: e.target.value })}
                    >
                      {(rooms.length > 0 ? rooms : [{ code: "OR-1" }, { code: "OR-2" }])
                        .map((r) => (
                          <option key={r.code} value={r.code}>{r.code}</option>
                        ))}
                    </select>
                  </label>
                  <label>
                    Schedule start{" "}
                    <input
                      type="datetime-local"
                      aria-label="Schedule start"
                      value={sched.start}
                      onChange={(e) => setSched({ ...sched, start: e.target.value })}
                    />
                  </label>
                  <label>
                    Schedule end{" "}
                    <input
                      type="datetime-local"
                      aria-label="Schedule end"
                      value={sched.end}
                      onChange={(e) => setSched({ ...sched, end: e.target.value })}
                    />
                  </label>
                  <button
                    type="button"
                    disabled={!sched.start || !sched.end}
                    onClick={() =>
                      void act("Request scheduled", async () => {
                        const out = await scheduleSurgeryRequest(
                          client, selected.request_id, {
                            room: sched.room,
                            start: new Date(sched.start).toISOString(),
                            end: new Date(sched.end).toISOString(),
                          });
                        setSchedResult(
                          out.in_block
                            ? `Scheduled in block${out.block_code ? ` ${out.block_code}` : ""} (case ${out.request.case_id ?? ""}).`
                            : "Scheduled outside a published block — reported, not enforced.",
                        );
                      })}
                  >
                    Schedule into block
                  </button>
                  {schedResult && <p className="hint">{schedResult}</p>}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!createForm.patientId.trim() || !createForm.procedureCode.trim()
              || !createForm.surgeon.trim()) return;
            void act("Case request created", async () => {
              await createSurgeryRequest(client, {
                patientId: createForm.patientId.trim(),
                procedureCode: createForm.procedureCode.trim(),
                procedureDisplay: createForm.display.trim() || undefined,
                surgeon: createForm.surgeon.trim(),
                urgency: createForm.urgency,
              });
              setCreateForm({
                patientId: "", procedureCode: "", display: "",
                surgeon: "", urgency: createForm.urgency,
              });
            });
          }}
        >
          <h4>New case request</h4>
          <label>
            Request patient id{" "}
            <input
              aria-label="Request patient id"
              value={createForm.patientId}
              onChange={(e) => setCreateForm({ ...createForm, patientId: e.target.value })}
            />
          </label>
          <label>
            Request procedure code{" "}
            <input
              aria-label="Request procedure code"
              value={createForm.procedureCode}
              onChange={(e) => setCreateForm({ ...createForm, procedureCode: e.target.value })}
            />
          </label>
          <label>
            Request description{" "}
            <input
              aria-label="Request description"
              value={createForm.display}
              onChange={(e) => setCreateForm({ ...createForm, display: e.target.value })}
            />
          </label>
          <label>
            Request surgeon{" "}
            <input
              aria-label="Request surgeon"
              value={createForm.surgeon}
              onChange={(e) => setCreateForm({ ...createForm, surgeon: e.target.value })}
            />
          </label>
          <label>
            Request urgency{" "}
            <select
              aria-label="Request urgency"
              value={createForm.urgency}
              onChange={(e) => setCreateForm({ ...createForm, urgency: e.target.value })}
            >
              {urgencies.filter((u) => u.active !== false).map((u) => (
                <option key={u.code} value={u.code}>{u.display ?? u.code}</option>
              ))}
            </select>
          </label>
          <button type="submit">Create request</button>
        </form>
      )}
    </div>
  );
}

/** Phase 15 (E): OR block templates (published terminology/or-blocks)
 *  + one day's utilisation, computed server-side from the real
 *  appointments — figures shown as text plus a bar. */
function BlocksPanel({ client, date }: { client: ApiClient; date: string }) {
  const blocksPayload = useEffectivePayload(client, "terminology", "or-blocks");
  const blocks: OrBlock[] = blocksFromPayload(blocksPayload) ?? [];
  const [util, setUtil] = useState<SurgeryUtilisation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    getSurgeryUtilisation(client, date)
      .then((res) => {
        if (!cancelled) setUtil(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client, date]);

  return (
    <div>
      <h3>Blocks &amp; utilisation</h3>
      <p className="hint">
        Block templates are published Studio configuration
        (terminology/or-blocks). Utilisation for {date} is computed by the
        server from the day&apos;s real appointments.
      </p>
      {error && <div className="state state-error" role="alert">{error}</div>}
      <h4>Block templates</h4>
      {blocks.length === 0
        ? <p className="hint">No published block templates loaded.</p>
        : (
          <ul>
            {blocks.map((b, i) => (
              <li key={b.code ?? i}>
                <strong>{b.code ?? "block"}</strong> — {b.surgeon ?? "—"} ·{" "}
                {b.service ?? "—"} · {b.room ?? "—"} · {b.weekday ?? "—"}{" "}
                {b.start ?? ""}–{b.end ?? ""}
              </li>
            ))}
          </ul>
        )}
      <h4>Utilisation — {date}{util ? ` (${util.weekday})` : ""}</h4>
      {!util && !error && <Loading label="Loading utilisation…" />}
      {util && util.rooms.length === 0 && <Empty label="No rooms configured." />}
      {util && util.rooms.map((room) => {
        const pct = room.utilisation_pct ?? 0;
        return (
          <div key={room.room}>
            <p>
              <strong>{room.room}</strong>
              {room.display ? ` — ${room.display}` : ""}: Utilisation{" "}
              {room.utilisation_pct === null ? "—" : `${room.utilisation_pct}%`} ·
              Scheduled {room.scheduled_minutes} min · Block {room.block_minutes}{" "}
              min · In-block {room.in_block_minutes} min · Non-block{" "}
              {room.non_block_minutes} min · Cases {room.cases} · Turnover gaps{" "}
              {room.turnover.total_gap_minutes} min ({room.turnover.count})
            </p>
            <div
              className="util-bar"
              role="img"
              aria-label={`${room.room} utilisation ${room.utilisation_pct ?? 0}%`}
              style={{
                background: "#e5e7eb", height: "0.75rem", maxWidth: "24rem",
              }}
            >
              <div
                style={{
                  background: "#0b6e4f", height: "100%",
                  width: `${Math.min(100, Math.max(0, pct))}%`,
                }}
              />
            </div>
            {room.blocks.length > 0 && (
              <ul>
                {room.blocks.map((b) => (
                  <li key={b.block_code ?? b.start}>
                    Block {b.block_code ?? "—"} ({b.surgeon ?? "—"},{" "}
                    {b.service ?? "—"}): {b.scheduled_minutes} of{" "}
                    {b.block_minutes} min scheduled — utilisation{" "}
                    {b.utilisation_pct === null ? "—" : `${b.utilisation_pct}%`}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}

function ItemRowsEditor({
  items,
  onChange,
  prefix,
}: {
  items: PreferenceCardItem[];
  onChange: (items: PreferenceCardItem[]) => void;
  prefix: string;
}) {
  return (
    <div>
      {items.map((item, i) => (
        <div key={i} className="row-actions">
          <label>
            Kind{" "}
            <select
              aria-label={`${prefix} item ${i + 1} kind`}
              value={item.kind}
              onChange={(e) =>
                onChange(items.map((it, j) => j === i
                  ? { ...it, kind: e.target.value as PreferenceCardItem["kind"] }
                  : it))}
            >
              <option value="supply">supply</option>
              <option value="implant">implant</option>
              <option value="instrument-tray">instrument-tray</option>
            </select>
          </label>
          <label>
            Code{" "}
            <input
              aria-label={`${prefix} item ${i + 1} code`}
              value={item.code}
              onChange={(e) =>
                onChange(items.map((it, j) => j === i ? { ...it, code: e.target.value } : it))}
            />
          </label>
          <label>
            Qty{" "}
            <input
              type="number"
              min={1}
              aria-label={`${prefix} item ${i + 1} qty`}
              value={item.qty}
              onChange={(e) =>
                onChange(items.map((it, j) => j === i
                  ? { ...it, qty: Number(e.target.value) || 1 }
                  : it))}
            />
          </label>
          <button
            type="button"
            onClick={() => onChange(items.filter((_, j) => j !== i))}
          >
            Remove item {i + 1}
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => onChange([...items, { kind: "supply", code: "", qty: 1 }])}
      >
        Add item
      </button>
    </div>
  );
}

/** Phase 15 (E): surgeon preference cards (CRUD) + case pick lists.
 *  Implant items allocate FEFO from the implant lot book; an oversell
 *  refusal (409) is surfaced verbatim. */
function PreferenceCardsPanel({
  client,
  canWork,
}: {
  client: ApiClient;
  canWork: boolean;
}) {
  const [cards, setCards] = useState<PreferenceCard[]>([]);
  const [lots, setLots] = useState<ImplantLot[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [editItems, setEditItems] = useState<PreferenceCardItem[]>([]);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [createForm, setCreateForm] = useState({ surgeon: "", procedureCode: "" });
  const [createItems, setCreateItems] = useState<PreferenceCardItem[]>([
    { kind: "supply", code: "", qty: 1 },
  ]);
  const [pickCaseId, setPickCaseId] = useState("");
  const [pick, setPick] = useState<CasePick | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    Promise.all([listPreferenceCards(client), listImplantLots(client)])
      .then(([cardRes, lotRes]) => {
        if (cancelled) return;
        setCards(cardRes.items);
        setLots(lotRes.items);
        const sel = cardRes.items.find((c) => c.card_id === selectedId)
          ?? cardRes.items[0] ?? null;
        setSelectedId(sel?.card_id ?? null);
        setEditItems(sel ? sel.items.map((i) => ({ ...i })) : []);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [client, tick]);

  const selected = cards.find((c) => c.card_id === selectedId) ?? null;

  async function act(label: string, fn: () => Promise<unknown>) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      setTick((t) => t + 1);
    } catch (err) {
      setError(`${label} failed: ${errorText(err)}`);
    }
  }

  async function generatePick() {
    setPickError(null);
    setPick(null);
    try {
      const out = await generatePickList(client, pickCaseId.trim());
      setPick(out);
    } catch (err) {
      // Surface the server's refusal verbatim (e.g. the oversell 409):
      // ApiError's message IS the server's detail string.
      setPickError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div>
      <h3>Preference cards</h3>
      <p className="hint">
        A surgeon&apos;s card for one procedure drives the case pick list.
        Implant items draw first-expiry-first-out from the implant lots
        below; a pick that cannot be covered in full is refused (oversell)
        before any lot moves.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      <div className="master-detail">
        <div>
          <h4>Cards ({cards.length})</h4>
          {cards.length === 0
            ? <p className="hint">No preference cards.</p>
            : (
              <ul>
                {cards.map((c) => (
                  <li key={c.card_id}>
                    <button
                      type="button"
                      onClick={() => {
                        setSelectedId(c.card_id);
                        setEditItems(c.items.map((i) => ({ ...i })));
                      }}
                    >
                      {c.surgeon} — {c.procedure_code}
                    </button>
                  </li>
                ))}
              </ul>
            )}
          <h4>Implant lots</h4>
          {lots.length === 0
            ? <p className="hint">No implant lots on hand.</p>
            : (
              <ul>
                {lots.map((l) => (
                  <li key={`${l.item_code}-${l.lot_number}`}>
                    {l.item_code} · Lot {l.lot_number} · expires {l.expiry_date}
                    {" "}· qty {l.quantity}
                    {l.expired ? " · EXPIRED" : ""}
                  </li>
                ))}
              </ul>
            )}
        </div>
        <div>
          <h4>Card detail</h4>
          {!selected && <p className="hint">Select a card to see its items.</p>}
          {selected && (
            <div>
              <p>
                <strong>{selected.surgeon}</strong> · procedure{" "}
                {selected.procedure_code} · {selected.items.length} items
              </p>
              <ul>
                {selected.items.map((item, i) => (
                  <li key={i}>
                    {item.kind}: {item.display ?? item.code} × {item.qty}
                  </li>
                ))}
              </ul>
              {canWork && (
                <>
                  <ItemRowsEditor
                    items={editItems}
                    onChange={setEditItems}
                    prefix="Card"
                  />
                  <div className="row-actions">
                    <button
                      type="button"
                      onClick={() =>
                        void act("Preference card updated", () =>
                          updatePreferenceCard(client, selected.card_id, editItems))}
                    >
                      Save card items
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        void act("Preference card deleted", () =>
                          deletePreferenceCard(client, selected.card_id))}
                    >
                      Delete card
                    </button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      </div>

      <h4>Generate pick list</h4>
      <div className="row-actions">
        <label>
          Pick list case id{" "}
          <input
            aria-label="Pick list case id"
            value={pickCaseId}
            onChange={(e) => setPickCaseId(e.target.value)}
            placeholder="case-1"
          />
        </label>
        <button
          type="button"
          disabled={!canWork || !pickCaseId.trim()}
          onClick={() => void generatePick()}
        >
          Generate pick list
        </button>
      </div>
      {pickError && (
        <div className="state state-error" role="alert">{pickError}</div>
      )}
      {pick && (
        <div>
          <p className="hint">
            Pick list {pick.pick_id} for case {pick.case_id} — surgeon{" "}
            {pick.surgeon}, procedure {pick.procedure_code} (card {pick.card_id}).
          </p>
          <ul>
            {pick.items.map((item, i) => (
              <li key={i}>
                {item.kind}: {item.display ?? item.code} × {item.qty}
                {item.allocations.length > 0 && (
                  <ul>
                    {item.allocations.map((a, j) => (
                      <li key={j}>
                        Lot {a.lot_number} · expires {a.expiry_date} · qty {a.qty}
                        {a.serial ? ` · serial ${a.serial}` : ""}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!createForm.surgeon.trim() || !createForm.procedureCode.trim()) return;
            void act("Preference card created", async () => {
              await createPreferenceCard(client, {
                surgeon: createForm.surgeon.trim(),
                procedureCode: createForm.procedureCode.trim(),
                items: createItems,
              });
              setCreateForm({ surgeon: "", procedureCode: "" });
              setCreateItems([{ kind: "supply", code: "", qty: 1 }]);
            });
          }}
        >
          <h4>New preference card</h4>
          <label>
            Card surgeon{" "}
            <input
              aria-label="Card surgeon"
              value={createForm.surgeon}
              onChange={(e) => setCreateForm({ ...createForm, surgeon: e.target.value })}
            />
          </label>
          <label>
            Card procedure code{" "}
            <input
              aria-label="Card procedure code"
              value={createForm.procedureCode}
              onChange={(e) =>
                setCreateForm({ ...createForm, procedureCode: e.target.value })}
            />
          </label>
          <ItemRowsEditor items={createItems} onChange={setCreateItems} prefix="New card" />
          <button type="submit">Create card</button>
        </form>
      )}
    </div>
  );
}

/** Phase 15 (E): sterile processing — tray lifecycle, sterilisation
 *  cycles (the ONLY route to sterile), and case assignment (409
 *  unless sterile + in-date). */
function SterilePanel({
  client,
  canWork,
}: {
  client: ApiClient;
  canWork: boolean;
}) {
  const trayTypesPayload = useEffectivePayload(client, "terminology", "instrument-trays");
  const trayTypes = conceptsFromPayload(trayTypesPayload) ?? FALLBACK_TRAY_TYPES;
  const sterilePayload = useEffectivePayload(client, "terminology", "sterile-processing");
  const sterileConfig: SterileConfig =
    sterileConfigFromPayload(sterilePayload) ?? FALLBACK_STERILE_CONFIG;

  const [trays, setTrays] = useState<TrayInstance[]>([]);
  const [states, setStates] = useState<string[]>(sterileConfig.states);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [cycleForm, setCycleForm] = useState({
    cycleType: sterileConfig.cycleTypes[0]?.code ?? "steam-134",
    temperature: "134",
    duration: "18",
    indicator: "pass" as "pass" | "fail",
  });
  const [assignCaseId, setAssignCaseId] = useState("");
  const [createForm, setCreateForm] = useState({
    trayId: "", trayType: trayTypes[0]?.code ?? "major-orthopaedic",
  });

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    listTrays(client)
      .then((res) => {
        if (cancelled) return;
        setTrays(res.items);
        if (res.states.length > 0) setStates(res.states);
        setSelectedId((prev) => prev ?? res.items[0]?.tray_id ?? null);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client, tick]);

  const selected = trays.find((t) => t.tray_id === selectedId) ?? null;
  const nextState = selected
    ? states[(states.indexOf(selected.status) + 1) % states.length]
    : null;

  async function act(label: string, fn: () => Promise<unknown>) {
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      setTick((t) => t + 1);
    } catch (err) {
      setError(`${label} failed: ${errorText(err)}`);
    }
  }

  return (
    <div>
      <h3>Sterile processing</h3>
      <p className="hint">
        Trays walk the published lifecycle ({states.join(" → ")}). A tray
        becomes sterile ONLY through a recorded sterilisation cycle, and
        can be issued to a case only while sterile and within its{" "}
        {sterileConfig.shelfLifeDays}-day shelf life.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {error && <div className="state state-error" role="alert">{error}</div>}
      <div className="master-detail">
        <div>
          <h4>Trays ({trays.length})</h4>
          {trays.length === 0
            ? <p className="hint">No trays registered.</p>
            : (
              <ul>
                {trays.map((t) => (
                  <li key={t.tray_id}>
                    <button type="button" onClick={() => setSelectedId(t.tray_id)}>
                      {t.tray_id} — {t.tray_type}
                    </button>{" "}
                    <StatusBadge tone={trayStatusTone(t.status)}>{t.status}</StatusBadge>
                    {t.expires_at
                      ? <span className="hint"> · sterile until {new Date(t.expires_at).toLocaleDateString()}</span>
                      : null}
                  </li>
                ))}
              </ul>
            )}
        </div>
        <div>
          <h4>Tray detail</h4>
          {!selected && <p className="hint">Select a tray.</p>}
          {selected && (
            <div>
              <p>
                <strong>{selected.tray_id}</strong> — {selected.tray_type}{" "}
                <StatusBadge tone={trayStatusTone(selected.status)}>
                  {selected.status}
                </StatusBadge>
              </p>
              <p className="hint">
                {selected.sterilised_at
                  ? `Sterilised ${new Date(selected.sterilised_at).toLocaleString()} · `
                  : ""}
                {selected.expires_at
                  ? `Expires ${new Date(selected.expires_at).toLocaleString()} · `
                  : ""}
                {selected.assigned_case_id
                  ? `Issued to case ${selected.assigned_case_id}`
                  : "Not issued"}
              </p>
              {canWork && selected.status === "sterilising" && (
                <div>
                  <h5>Record sterilisation cycle</h5>
                  <label>
                    Cycle type{" "}
                    <select
                      aria-label="Cycle type"
                      value={cycleForm.cycleType}
                      onChange={(e) =>
                        setCycleForm({ ...cycleForm, cycleType: e.target.value })}
                    >
                      {sterileConfig.cycleTypes.map((c) => (
                        <option key={c.code} value={c.code}>
                          {c.display ?? c.code}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Temperature °C{" "}
                    <input
                      type="number"
                      aria-label="Cycle temperature"
                      value={cycleForm.temperature}
                      onChange={(e) =>
                        setCycleForm({ ...cycleForm, temperature: e.target.value })}
                    />
                  </label>
                  <label>
                    Duration minutes{" "}
                    <input
                      type="number"
                      aria-label="Cycle duration"
                      value={cycleForm.duration}
                      onChange={(e) =>
                        setCycleForm({ ...cycleForm, duration: e.target.value })}
                    />
                  </label>
                  <label>
                    Indicator result{" "}
                    <select
                      aria-label="Indicator result"
                      value={cycleForm.indicator}
                      onChange={(e) =>
                        setCycleForm({
                          ...cycleForm,
                          indicator: e.target.value as "pass" | "fail",
                        })}
                    >
                      <option value="pass">pass</option>
                      <option value="fail">fail</option>
                    </select>
                  </label>
                  <div className="row-actions">
                    <button
                      type="button"
                      onClick={() =>
                        void act("Sterilisation cycle recorded", () =>
                          runSterilisationCycle(client, selected.tray_id, {
                            cycleType: cycleForm.cycleType,
                            temperatureC: Number(cycleForm.temperature),
                            durationMinutes: Number(cycleForm.duration),
                            indicatorResult: cycleForm.indicator,
                          }))}
                    >
                      Record cycle
                    </button>
                  </div>
                </div>
              )}
              {canWork && selected.status === "sterile" && (
                <div className="row-actions">
                  <label>
                    Assign to case id{" "}
                    <input
                      aria-label="Assign tray to case id"
                      value={assignCaseId}
                      onChange={(e) => setAssignCaseId(e.target.value)}
                      placeholder="case-1"
                    />
                  </label>
                  <button
                    type="button"
                    disabled={!assignCaseId.trim()}
                    onClick={() =>
                      void act("Tray assigned to case", () =>
                        assignTrayToCase(client, assignCaseId.trim(), selected.tray_id))}
                  >
                    Assign tray to case
                  </button>
                </div>
              )}
              {canWork && nextState
                && selected.status !== "sterilising"
                && selected.status !== "sterile" && (
                <div className="row-actions">
                  <button
                    type="button"
                    onClick={() =>
                      void act(`Tray → ${nextState}`, () =>
                        transitionTray(client, selected.tray_id, nextState))}
                  >
                    {`Advance to ${nextState}`}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            void act("Tray registered", async () => {
              await createTray(client, {
                trayType: createForm.trayType,
                trayId: createForm.trayId.trim() || undefined,
              });
              setCreateForm({ trayId: "", trayType: createForm.trayType });
            });
          }}
        >
          <h4>Register tray</h4>
          <label>
            Tray id (optional){" "}
            <input
              aria-label="New tray id"
              value={createForm.trayId}
              onChange={(e) => setCreateForm({ ...createForm, trayId: e.target.value })}
            />
          </label>
          <label>
            Tray type{" "}
            <select
              aria-label="New tray type"
              value={createForm.trayType}
              onChange={(e) => setCreateForm({ ...createForm, trayType: e.target.value })}
            >
              {trayTypes.filter((t) => t.active !== false).map((t) => (
                <option key={t.code} value={t.code}>{t.display ?? t.code}</option>
              ))}
            </select>
          </label>
          <button type="submit">Register tray</button>
        </form>
      )}
    </div>
  );
}

/** Phase 15 (E): outcomes + the SSI surveillance feed for infection
 *  prevention (per-case outcome entry lives in the board's case
 *  detail, next to the PACU score). */
function OutcomesPanel({ client }: { client: ApiClient }) {
  const [feed, setFeed] = useState<{
    feed: string; purpose: string; items: SurgeryOutcome[]; total: number;
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    if (typeof client.request !== "function") return;
    getSsiFeed(client)
      .then((res) => {
        if (!cancelled) setFeed(res);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(errorText(err));
      });
    return () => {
      cancelled = true;
    };
  }, [client]);

  return (
    <div>
      <h3>Outcomes — SSI surveillance</h3>
      <p className="hint">
        Record a case&apos;s outcome (with any surgical-site infection) from
        the board&apos;s case detail — “PACU &amp; outcome”. This tab is the
        infection-prevention surveillance feed of SSI cases.
      </p>
      {error && <div className="state state-error" role="alert">{error}</div>}
      {!feed && !error && <Loading label="Loading SSI feed…" />}
      {feed && (
        <>
          <p>
            <strong>{feed.feed}</strong> — {feed.purpose} ({feed.total} cases)
          </p>
          {feed.items.length === 0
            ? <Empty label="No surgical-site infections recorded." />
            : (
              <ul>
                {feed.items.map((o) => (
                  <li key={o.outcome_id}>
                    Case {o.case_id} · Patient {o.patient_ref ?? "—"} ·{" "}
                    {o.procedure_display ?? o.procedure_code ?? "procedure"} ·{" "}
                    Status {o.status} · SSI onset {o.ssi_onset_date ?? "—"}
                    {o.organism ? ` · Organism ${o.organism}` : ""}
                  </li>
                ))}
              </ul>
            )}
        </>
      )}
    </div>
  );
}

export function SurgeryBoardPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["surgery.work"]?.[role] === true;
  const [nextStates, setNextStates] =
    useState<Record<string, string[]>>(NEXT_STATES);
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        if (typeof client.effectiveConfig !== "function") return;
        const cfg = await client.effectiveConfig("workflow", "surgery-flow");
        const built = nextStatesFromPayload(cfg?.payload);
        if (!cancelled && built) setNextStates(built);
      } catch {
        // keep the built-in map
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [client]);
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [date, setDate] = useState(todayIso());
  const [moves, setMoves] = useState<Record<string, { start: string; end: string }>>({});
  const [form, setForm] = useState({
    patientId: "", code: "", display: "", room: "OR-1", start: "", end: "",
  });
  const [openRecord, setOpenRecord] = useState<string | null>(null);
  const [openSafety, setOpenSafety] = useState<string | null>(null);
  const [openPacu, setOpenPacu] = useState<string | null>(null);
  const [openIntraop, setOpenIntraop] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState("board");

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
    const caseNextStates = nextStates[state] ?? [];
    const move = moves[caseId] ?? { start: "", end: "" };
    return (
      <li key={caseId} className="board-card">
        <div>
          <strong>{codeLabel(entry.case.code)}</strong>{" "}
          <StatusBadge
            tone={state === "done" ? "ok" : state === "pacu" ? "warn" : state === "in-op" || state === "pre-op" ? "info" : "neutral"}
          >
            {state}
          </StatusBadge>
        </div>
        <div className="hint">
          {entry.start ? new Date(entry.start).toLocaleTimeString() : "—"} –{" "}
          {entry.end ? new Date(entry.end).toLocaleTimeString() : "—"} · Patient{" "}
          {refLabel(entry.case.subject)}
        </div>
        {entry.timeline && entry.timeline.length > 0 && (
          <div className="hint">
            Timeline:{" "}
            {entry.timeline
              .map((e) => `${e.event}${e.at ? ` ${new Date(e.at).toLocaleTimeString()}` : ""}`)
              .join(" → ")}
          </div>
        )}
        <div className="row-actions">
          <button
            type="button"
            onClick={() => setOpenRecord(openRecord === caseId ? null : caseId)}
          >
            {openRecord === caseId ? "Hide record" : "Case record"}
          </button>
          <button
            type="button"
            onClick={() => setOpenSafety(openSafety === caseId ? null : caseId)}
          >
            {openSafety === caseId ? "Hide safety checklist" : "Safety checklist"}
          </button>
          <button
            type="button"
            onClick={() => setOpenPacu(openPacu === caseId ? null : caseId)}
          >
            {openPacu === caseId ? "Hide PACU & outcome" : "PACU & outcome"}
          </button>
          <button
            type="button"
            onClick={() => setOpenIntraop(openIntraop === caseId ? null : caseId)}
          >
            {openIntraop === caseId ? "Hide intra-op forms" : "Intra-op forms"}
          </button>
        </div>
        {openRecord === caseId && (
          <CaseRecordPanel client={client} caseId={caseId} canWork={canWork} />
        )}
        {openPacu === caseId && (
          <PacuOutcomePanel client={client} caseId={caseId} canWork={canWork} />
        )}
        {openIntraop === caseId && (
          <IntraopPanel client={client} caseId={caseId} canWork={canWork} />
        )}
        {openSafety === caseId && (
          <SafetyChecklistPanel
            client={client}
            caseId={caseId}
            canWork={canWork}
            initial={(entry as SurgeryCaseEntry & {
              safety_checklist?: SafetyChecklistStatus;
            }).safety_checklist}
            onRecorded={() => setTick((t) => t + 1)}
          />
        )}
        {canWork && caseNextStates.length > 0 && (
          <div className="row-actions">
            {caseNextStates.map((target) => (
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
      <ModuleHeader
        icon="scalpel"
        group="Clinical"
        title="Surgery board"
        purpose="The operating-room day: schedule, requests, blocks, preference cards, sterile processing and outcomes."
      />
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

      <Tabs
        ariaLabel="Surgery department"
        tabs={[
          { key: "board", label: "Board" },
          { key: "requests", label: "Requests & waitlist" },
          { key: "blocks", label: "Blocks & utilisation" },
          { key: "cards", label: "Preference cards" },
          { key: "sterile", label: "Sterile processing" },
          { key: "outcomes", label: "Outcomes" },
        ]}
        activeKey={activeTab}
        onChange={setActiveTab}
        panels={{
          // The board panel is the existing board UI below (rendered
          // when the Board tab is active) — unchanged behaviour.
          board: <></>,
          requests: (
            <RequestsPanel client={client} canWork={canWork} rooms={rooms} />
          ),
          blocks: <BlocksPanel client={client} date={date} />,
          cards: <PreferenceCardsPanel client={client} canWork={canWork} />,
          sterile: <SterilePanel client={client} canWork={canWork} />,
          outcomes: <OutcomesPanel client={client} />,
        }}
      />

      {activeTab === "board" && (
        <>
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
        </>
      )}
    </section>
  );
}
