import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import type { FormDefinition, Role } from "../api/types";
import {
  addendum,
  assignProtocol,
  closeCritical,
  createImagingReport,
  createStudy,
  doseHistory,
  flagCritical,
  getInstances,
  getModalities,
  getOps,
  getProtocols,
  getPublishedForm,
  getSeries,
  getStudyDetail,
  getModalityWorklist,
  getWorklist,
  ingestDicom,
  listCritical,
  readBackCritical,
  recordDose,
  scanCritical,
  structuredReport,
  submitScreening,
  tatStats,
  transitionWorklist,
  verifyImagingReport,
  type DicomIngestResult,
  type MwlEntry,
  type RadCritical,
  type RadDoseHistory,
  type RadWorklistItem,
} from "../api/radiology";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { FormRenderer } from "../components/FormRenderer";
import { RichTextEditor } from "../components/RichTextEditor";
import { RichTextView } from "../components/RichTextView";
import { htmlToText } from "../components/richtext";
import { ImagingAssistPanel } from "./ImagingAssistPanel";
import { ListManager } from "../components/ListManager";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

// Phase 15 integration pass: the shared ListManager now sits beside
// the modality filter (below). The study detail remains an inline
// two-pane (worklist table + detail panel) — functionally equivalent
// to the shared MasterDetail, which the Patients/Workforce pages use.

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const WORKLIST_STATES = ["ordered", "scheduled", "arrived", "in-progress", "completed", "reported"];
const PRIORITIES = ["STAT", "URGENT", "ROUTINE"];
const NEXT_STATE: Record<string, { to: string; label: string }> = {
  ordered: { to: "scheduled", label: "Schedule" },
  scheduled: { to: "arrived", label: "Mark arrived" },
  arrived: { to: "in-progress", label: "Start exam" },
  "in-progress": { to: "completed", label: "Complete exam" },
  completed: { to: "reported", label: "Mark reported" },
};
const SCREENING_FORMS = [
  { key: "radiology-screening-contrast", label: "Contrast safety screening" },
  { key: "radiology-screening-pregnancy", label: "Pregnancy screening" },
  { key: "radiology-screening-mr-safety", label: "MR safety screening" },
];
const REPORT_TEMPLATES = ["radiology-report-ct-chest", "radiology-report-xr-chest"];

function stateTone(state: string): StatusTone {
  if (state === "reported" || state === "completed") return "ok";
  if (state === "in-progress" || state === "arrived") return "warn";
  if (state === "scheduled") return "info";
  return "neutral";
}
function priorityTone(priority: string): StatusTone {
  if (priority === "STAT") return "bad";
  if (priority === "URGENT") return "warn";
  return "neutral";
}
function reportTone(status: string | undefined): StatusTone {
  if (status === "final") return "ok";
  if (status === "preliminary") return "warn";
  return "neutral";
}

interface SeriesView {
  uid: string;
  modality: string;
  instances: Array<{ sopUid: string; number: unknown }>;
}

function dicomValue(obj: Record<string, { Value?: unknown[] }>, tag: string): unknown {
  return obj[tag]?.Value?.[0];
}

export function RadiologyPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["radiology.work"]?.[role] === true;
  const canVerify = permissions["radiology.verify"]?.[role] === true;
  const [tab, setTab] = useState("worklist");
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [orderId, setOrderId] = useState("");
  const [createModality, setCreateModality] = useState("CT");
  const [stateFilter, setStateFilter] = useState("");
  const [priorityFilter, setPriorityFilter] = useState("");
  const [modalityFilter, setModalityFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conclusion, setConclusion] = useState("");
  const [protocolCode, setProtocolCode] = useState("");
  const [screeningKey, setScreeningKey] = useState<string | null>(null);
  const [screeningForm, setScreeningForm] = useState<FormDefinition | null>(null);
  const [templateKey, setTemplateKey] = useState(REPORT_TEMPLATES[0]);
  const [templateForm, setTemplateForm] = useState<FormDefinition | null>(null);
  const [addendumText, setAddendumText] = useState<Record<string, string>>({});

  const worklistState = useAsync(
    () => getWorklist(client, { state: stateFilter || undefined, priority: priorityFilter || undefined, modality: modalityFilter || undefined }),
    [client, tick, stateFilter, priorityFilter, modalityFilter],
  );
  const tatState = useAsync(() => tatStats(client), [client, tick]);
  const criticalState = useAsync(() => listCritical(client), [client, tick]);
  const modalitiesState = useAsync(() => getModalities(client), [client]);
  const protocolsState = useAsync(() => getProtocols(client).catch(() => []), [client]);
  const opsState = useAsync(() => getOps(client).catch(() => null), [client]);

  const detailState = useAsync(async () => {
    if (!selectedId) return null;
    const detail = await getStudyDetail(client, selectedId);
    let series: SeriesView[] = [];
    if (detail.study_uid) {
      try {
        const rawSeries = await getSeries(client, detail.study_uid);
        series = await Promise.all(rawSeries.map(async (s) => {
          const uid = String(dicomValue(s, "0020000E") ?? "");
          let instances: SeriesView["instances"] = [];
          try {
            const rawInstances = await getInstances(client, detail.study_uid as string, uid);
            instances = rawInstances.map((i) => ({
              sopUid: String(dicomValue(i, "00080018") ?? ""),
              number: dicomValue(i, "00200013"),
            }));
          } catch { /* series without archived instances renders empty */ }
          return { uid, modality: String(dicomValue(s, "00080060") ?? ""), instances };
        }));
      } catch { /* no archived DICOM for this study yet */ }
    }
    return { detail, series };
  }, [client, selectedId, tick]);

  const items = worklistState.data?.items ?? [];
  const modalities = (modalitiesState.data?.modalities ?? []).filter((m) => m.active !== false);
  const protocols = (protocolsState.data ?? []).filter((p) => p.active !== false);
  const tat = tatState.data;
  const criticalItems = criticalState.data?.items ?? [];
  const openCritical = criticalItems.filter((c) => c.state !== "closed").length;
  const selected = items.find((i) => i.study_id === selectedId) ?? null;
  const detail = detailState.data?.detail ?? null;
  const series = detailState.data?.series ?? [];

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

  async function openForm(kind: "screening" | "template", key: string) {
    if (kind === "screening") { setScreeningKey(key); setScreeningForm(null); }
    else { setTemplateForm(null); }
    try {
      const { form } = await getPublishedForm(client, key);
      if (kind === "screening") setScreeningForm(form);
      else setTemplateForm(form);
    } catch (err) {
      setActionError(`Loading form ${key} failed: ${errorText(err)}`);
    }
  }

  const worklistPanel = (
    <div>
      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!orderId.trim()) return;
            void perform("Study created", async () => {
              const item = await createStudy(client, { serviceRequestId: orderId.trim(), modality: createModality });
              setSelectedId(item.study.id as string);
              setOrderId("");
            });
          }}
        >
          <h3>New study from order</h3>
          <label>
            Service request id{" "}
            <input aria-label="Service request id" value={orderId} onChange={(e) => setOrderId(e.target.value)} placeholder="sr-img-1" />
          </label>
          <label>
            Modality{" "}
            <select aria-label="Modality" value={createModality} onChange={(e) => setCreateModality(e.target.value)}>
              {modalities.map((m) => (
                <option key={m.code} value={m.code}>{m.code} — {m.display ?? m.code}</option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy}>Create study</button>
        </form>
      )}

      <h3>RIS worklist</h3>
      <div className="inline-form">
        <label>
          State{" "}
          <select aria-label="Worklist state filter" value={stateFilter} onChange={(e) => setStateFilter(e.target.value)}>
            <option value="">All states</option>
            {WORKLIST_STATES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </label>
        <label>
          Priority{" "}
          <select aria-label="Worklist priority filter" value={priorityFilter} onChange={(e) => setPriorityFilter(e.target.value)}>
            <option value="">All priorities</option>
            {PRIORITIES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <label>
          Modality{" "}
          <select aria-label="Worklist modality filter" value={modalityFilter} onChange={(e) => setModalityFilter(e.target.value)}>
            <option value="">All modalities</option>
            {modalities.map((m) => <option key={m.code} value={m.code}>{m.code}</option>)}
          </select>
        </label>
        <ListManager client={client} kind="terminology" listKey="imaging-modalities" label="Imaging modalities" />
      </div>
      {worklistState.loading && <Loading label="Loading worklist…" />}
      {worklistState.error != null && <ErrorView error={worklistState.error} />}
      {!worklistState.loading && !worklistState.error && items.length === 0 && (
        <Empty label="No imaging studies on the worklist." />
      )}
      {items.length > 0 && (
        <table className="table">
          <thead>
            <tr><th>Patient</th><th>Modality</th><th>Description</th><th>Priority</th><th>State</th><th>Report</th><th /></tr>
          </thead>
          <tbody>
            {items.map((item) => (
              <tr key={item.study_id}>
                <td>{item.patient_ref}</td>
                <td>{item.modality || "—"}</td>
                <td>{item.description ?? "—"}</td>
                <td><StatusBadge tone={priorityTone(item.priority)}>{item.priority}</StatusBadge></td>
                <td><StatusBadge tone={stateTone(item.state)}>{item.state}</StatusBadge></td>
                <td>{item.report ? <StatusBadge tone={reportTone(item.report.status)}>{item.report.status}</StatusBadge> : "no report"}</td>
                <td>
                  <button type="button" onClick={() => { setSelectedId(item.study_id); setScreeningKey(null); setTemplateForm(null); }}>
                    Details
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedId && detailState.loading && <Loading label="Loading study…" />}
      {selectedId && detail && selected && (
        <div className="board-card">
          <h3>Study detail</h3>
          <p className="hint">
            {detail.study.description ?? "Imaging study"} · {detail.study.modality?.[0]?.display ?? selected.modality} · Patient{" "}
            {refLabel(detail.study.subject)} · UID {detail.study_uid} · Ordered{" "}
            {selected.ordered_at ? new Date(selected.ordered_at).toLocaleString() : "—"}
            {selected.tat_target_minutes != null ? ` · TAT target ${selected.tat_target_minutes} min` : ""}
            {selected.station_ae ? ` · Station ${selected.station_ae}` : ""}
          </p>
          <p>
            State <StatusBadge tone={stateTone(selected.state)}>{selected.state}</StatusBadge>{" "}
            Priority <StatusBadge tone={priorityTone(selected.priority)}>{selected.priority}</StatusBadge>{" "}
            Protocol: {selected.protocol_code ?? "not assigned"}
          </p>
          {canWork && NEXT_STATE[selected.state] && (
            <button type="button" disabled={busy} onClick={() =>
              void perform(`Study ${NEXT_STATE[selected.state].label}`, () =>
                transitionWorklist(client, selected.study_id, NEXT_STATE[selected.state].to))}>
              {NEXT_STATE[selected.state].label}
            </button>
          )}

          <h4>Viewer / images</h4>
          {series.length === 0 && (
            <p className="hint">No archived DICOM series for this study yet — metadata only.</p>
          )}
          {series.map((s) => (
            <div key={s.uid}>
              <p>Series {s.uid} ({s.modality}) — {s.instances.length} instance(s)</p>
              <ul>
                {s.instances.map((inst) => (
                  <li key={inst.sopUid}>Instance {String(inst.number ?? "—")} · SOP {inst.sopUid}</li>
                ))}
              </ul>
            </div>
          ))}
          {detail.viewer_url ? (
            <p>
              <a href={detail.viewer_url} target="_blank" rel="noreferrer">Open in external viewer</a>{" "}
              <span className="hint">(external viewer — leaves OpenCare)</span>
            </p>
          ) : (
            <p className="hint">External viewer not configured.</p>
          )}

          <ImagingAssistPanel
            client={client}
            studyId={selected.study_id}
            studyLabel={detail.study.description ?? "Imaging study"}
          />

          {canWork && (
            <>
              <h4>Protocol</h4>
              <div className="inline-form">
                <label>
                  Protocol{" "}
                  <select aria-label="Protocol" value={protocolCode} onChange={(e) => setProtocolCode(e.target.value)}>
                    <option value="">Select a protocol…</option>
                    {protocols.filter((p) => !p.modality || p.modality === selected.modality).map((p) => (
                      <option key={p.code} value={p.code}>{p.code} — {p.display ?? p.code}</option>
                    ))}
                  </select>
                </label>
                <button type="button" disabled={busy || !protocolCode} onClick={() =>
                  void perform("Protocol assigned", () => assignProtocol(client, selected.study_id, protocolCode))}>
                  Assign protocol
                </button>
              </div>

              <h4>Pre-exam screening</h4>
              <p className="hint">
                Completed: {Object.keys(selected.screening ?? {}).length > 0
                  ? Object.keys(selected.screening).join(", ")
                  : "none yet"}
              </p>
              {SCREENING_FORMS.map((f) => (
                <button key={f.key} type="button" disabled={busy} onClick={() => void openForm("screening", f.key)}>
                  {f.label}
                </button>
              ))}
              {screeningKey && (screeningForm ? (
                <FormRenderer
                  form={screeningForm}
                  submitLabel="Submit screening"
                  onSubmit={async (answers) => {
                    await perform("Screening submitted", async () => {
                      await submitScreening(client, selected.study_id, screeningKey, answers);
                      setScreeningKey(null);
                    });
                  }}
                />
              ) : <Loading label="Loading screening form…" />)}
            </>
          )}

          <h4>Reports</h4>
          {detail.reports.length === 0 && <Empty label="No report drafted for this study yet." />}
          {detail.reports.map((report) => {
            const rid = report.id as string;
            return (
              <div key={rid} className="board-card">
                <div>
                  <strong>Report {rid}</strong>{" "}
                  <StatusBadge tone={reportTone(report.status)}>{report.status}</StatusBadge>
                </div>
                {report.conclusion ? (
                  <RichTextView html={report.conclusion as string} />
                ) : (
                  <p>—</p>
                )}
                {canVerify && report.status === "preliminary" && (
                  <button type="button" disabled={busy} onClick={() =>
                    void perform("Report verified", () => verifyImagingReport(client, rid))}>
                    Verify report
                  </button>
                )}
                {canWork && report.status === "final" && (
                  <>
                    <button type="button" disabled={busy} onClick={() =>
                      void perform("Critical result flagged", () => flagCritical(client, rid))}>
                      Flag as critical result
                    </button>
                    <div className="inline-form">
                      <label>
                        Addendum conclusion{" "}
                        <RichTextEditor
                          ariaLabel={`Addendum conclusion for report ${rid}`}
                          value={addendumText[rid] ?? ""}
                          onChange={(html) => setAddendumText((m) => ({ ...m, [rid]: html }))}
                          minRows={2}
                        />
                      </label>
                      <button type="button" disabled={busy || !htmlToText(addendumText[rid] ?? "").trim()} onClick={() =>
                        void perform("Addendum added", () => addendum(client, rid, addendumText[rid] ?? ""))}>
                        Add addendum
                      </button>
                    </div>
                  </>
                )}
              </div>
            );
          })}

          {canWork && (
            <>
              <h4>Structured report</h4>
              <div className="inline-form">
                <label>
                  Report template{" "}
                  <select aria-label="Report template" value={templateKey} onChange={(e) => setTemplateKey(e.target.value)}>
                    {REPORT_TEMPLATES.map((k) => <option key={k} value={k}>{k}</option>)}
                  </select>
                </label>
                <button type="button" disabled={busy} onClick={() => void openForm("template", templateKey)}>
                  Load report template
                </button>
              </div>
              {templateForm && (
                <FormRenderer
                  form={templateForm}
                  submitLabel="Save structured report"
                  onSubmit={async (answers) => {
                    await perform("Structured report saved", async () => {
                      await structuredReport(client, selected.study_id, templateKey, answers);
                      setTemplateForm(null);
                    });
                  }}
                />
              )}
              {!detail.reports.some((r) => r.status === "preliminary") && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    if (!htmlToText(conclusion).trim()) return;
                    void perform("Report drafted", async () => {
                      await createImagingReport(client, selected.study_id, conclusion);
                      setConclusion("");
                    });
                  }}
                >
                  <label>
                    Report conclusion{" "}
                    <RichTextEditor
                      ariaLabel="Report conclusion"
                      value={conclusion}
                      onChange={setConclusion}
                      minRows={3}
                    />
                  </label>
                  <button type="submit" disabled={busy}>Save report draft</button>
                </form>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );

  const criticalPanel = (
    <div>
      <h3>Critical results</h3>
      {canWork && (
        <button type="button" disabled={busy} onClick={() =>
          void perform("Overdue critical scan", () => scanCritical(client))}>
          Scan for overdue criticals
        </button>
      )}
      {criticalState.loading && <Loading label="Loading critical results…" />}
      {criticalState.error != null && <ErrorView error={criticalState.error} />}
      {!criticalState.loading && criticalItems.length === 0 && (
        <Empty label="No critical results flagged." />
      )}
      {criticalItems.map((item) => (
        <CriticalRow key={item.report_id} item={item} canWork={canWork} busy={busy} perform={perform} client={client} />
      ))}
    </div>
  );

  const dosePanel = (
    <DosePanel
      client={client}
      items={items}
      doseQuantities={opsState.data?.doseQuantities ?? {}}
      canWork={canWork}
      busy={busy}
      perform={perform}
    />
  );

  const dicomPanel = (
    <DicomIngestPanel client={client} canWork={canWork} busy={busy} perform={perform} />
  );

  const mwlPanel = <MwlPanel client={client} />;

  return (
    <section>
      <ModuleHeader icon="scan" group="Clinical" title="Radiology" purpose="Imaging orders, studies and reports" />
      <p className="hint">
        The radiology department: RIS worklist, protocoling and pre-exam
        screening, archived DICOM (served via DICOMweb; pixels open in the
        configured <strong>external</strong> viewer), structured reporting,
        the critical-result closed loop, and radiation dose tracking.
        Synthetic demo data only.
      </p>
      {tat && (
        <div className="dashboard-grid" aria-label="Radiology summary">
          <div className="board-card"><strong>{worklistState.data?.total ?? items.length}</strong><br />Studies on worklist</div>
          <div className="board-card"><strong>{tat.total}</strong><br />Reported (TAT base)</div>
          <div className="board-card"><strong>{tat.breaches}</strong><br />TAT breaches</div>
          <div className="board-card"><strong>{tat.unreported_stat}</strong><br />STAT not yet reported</div>
          <div className="board-card"><strong>{openCritical}</strong><br />Open critical results</div>
        </div>
      )}
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}
      <Tabs
        ariaLabel="Radiology sections"
        tabs={[
          { key: "worklist", label: "Worklist" },
          { key: "mwl", label: "Modality worklist" },
          { key: "dicom", label: "DICOM ingest" },
          { key: "critical", label: "Critical results" },
          { key: "dose", label: "Dose" },
        ]}
        activeKey={tab}
        onChange={setTab}
        panels={{ worklist: worklistPanel, mwl: mwlPanel, dicom: dicomPanel, critical: criticalPanel, dose: dosePanel }}
      />
    </section>
  );
}

function CriticalRow({ item, canWork, busy, perform, client }: {
  item: RadCritical;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  client: ApiClient;
}) {
  const [reader, setReader] = useState("");
  const [text, setText] = useState("");
  return (
    <div className="board-card">
      <div>
        <strong>Report {item.report_id}</strong>{" "}
        <StatusBadge tone={item.state === "closed" ? "ok" : item.overdue ? "bad" : "warn"}>{item.state}</StatusBadge>{" "}
        {item.overdue && <StatusBadge tone="bad">Overdue{item.minutes_overdue != null ? ` by ${item.minutes_overdue} min` : ""}</StatusBadge>}
      </div>
      <p className="hint">
        Patient {item.patient_ref} · Study {item.study_id} · Ordering clinician {item.ordering_clinician || "—"} ·
        Flagged by {item.flagged_by}{item.flagged_at ? ` at ${new Date(item.flagged_at).toLocaleString()}` : ""} · SLA {item.sla_minutes} min
      </p>
      {item.readback_by && (
        <p>Read back by {item.readback_by}: “{item.readback_text}”</p>
      )}
      {canWork && item.state === "notified" && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!reader.trim() || !text.trim()) return;
            void perform("Read-back recorded", () => readBackCritical(client, item.report_id, reader.trim(), text.trim()));
          }}
        >
          <label>
            Reader{" "}
            <input aria-label={`Reader for report ${item.report_id}`} value={reader} onChange={(e) => setReader(e.target.value)} />
          </label>
          <label>
            Read-back text{" "}
            <input aria-label={`Read-back text for report ${item.report_id}`} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <button type="submit" disabled={busy}>Submit read-back</button>
        </form>
      )}
      {canWork && item.state === "read-back" && (
        <button type="button" disabled={busy} onClick={() =>
          void perform("Critical result closed", () => closeCritical(client, item.report_id))}>
          Close critical result
        </button>
      )}
    </div>
  );
}

function DosePanel({ client, items, doseQuantities, canWork, busy, perform }: {
  client: ApiClient;
  items: RadWorklistItem[];
  doseQuantities: Record<string, Array<{ code: string; unit?: string }>>;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [studyId, setStudyId] = useState("");
  const [quantity, setQuantity] = useState("");
  const [value, setValue] = useState("");
  const [patientId, setPatientId] = useState("");
  const [history, setHistory] = useState<RadDoseHistory | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);

  const study = items.find((i) => i.study_id === (studyId || items[0]?.study_id));
  const quantities = study ? (doseQuantities[study.modality] ?? []) : [];

  return (
    <div>
      <h3>Radiation dose</h3>
      {canWork && (
        <div className="inline-form">
          <label>
            Dose study{" "}
            <select aria-label="Dose study" value={study?.study_id ?? ""} onChange={(e) => setStudyId(e.target.value)}>
              {items.map((i) => (
                <option key={i.study_id} value={i.study_id}>{i.study_id} — {i.description ?? i.modality}</option>
              ))}
            </select>
          </label>
          <label>
            Dose quantity{" "}
            <select aria-label="Dose quantity" value={quantity} onChange={(e) => setQuantity(e.target.value)}>
              <option value="">Select a quantity…</option>
              {quantities.map((q) => (
                <option key={q.code} value={q.code}>{q.code}{q.unit ? ` (${q.unit})` : ""}</option>
              ))}
            </select>
          </label>
          <label>
            Dose value{" "}
            <input aria-label="Dose value" type="number" step="any" value={value} onChange={(e) => setValue(e.target.value)} />
          </label>
          <button type="button" disabled={busy || !study || !quantity || !value} onClick={() =>
            void perform("Dose recorded", () => recordDose(client, study!.study_id, quantity, Number(value)))}>
            Record dose
          </button>
        </div>
      )}
      <div className="inline-form">
        <label>
          Patient id for dose history{" "}
          <input aria-label="Patient id for dose history" value={patientId} onChange={(e) => setPatientId(e.target.value)} placeholder="pat-1" />
        </label>
        <button type="button" disabled={!patientId.trim()} onClick={async () => {
          setHistoryError(null);
          try {
            setHistory(await doseHistory(client, patientId.trim()));
          } catch (err) {
            setHistoryError(errorText(err));
          }
        }}>
          Look up dose history
        </button>
      </div>
      {historyError && <div className="state state-error" role="alert">{historyError}</div>}
      {history && (
        <div>
          <h4>Cumulative dose — {history.patient_ref}</h4>
          {history.items.length === 0 && <Empty label="No dose records for this patient." />}
          {history.items.length > 0 && (
            <table className="table">
              <thead><tr><th>Study</th><th>Modality</th><th>Quantity</th><th>Value</th><th>Recorded by</th></tr></thead>
              <tbody>
                {history.items.map((r) => (
                  <tr key={r.id}>
                    <td>{r.study_id}</td><td>{r.modality}</td><td>{r.quantity}</td>
                    <td>{r.value} {r.unit}</td><td>{r.recorded_by}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {history.totals.map((t) => (
            <p key={`${t.quantity}-${t.unit}`}>
              Total {t.quantity}: <strong>{t.total} {t.unit}</strong> across {t.count} record(s)
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// DICOM ingest panel (Phase 17, WS-RESID — the Phase 15 named UI gap;
// the backend POST /radiology/dicom/ingest already existed)
// ---------------------------------------------------------------------------

/** Read a file's exact bytes and base64-encode them (chunked, so a
 *  large object never blows the call stack). FileReader is used
 *  rather than Blob.arrayBuffer so the panel works in every browser
 *  and in the jsdom test environment alike. The ingest contract is
 *  base64-in-JSON, not multipart — see api/radiology.ts. */
async function fileToBase64(file: File): Promise<string> {
  const buffer = await new Promise<ArrayBuffer>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.onerror = () =>
      reject(reader.error ?? new Error("Could not read the selected file"));
    reader.readAsArrayBuffer(file);
  });
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    binary += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(binary);
}

function DicomIngestPanel({ client, canWork, busy, perform }: {
  client: ApiClient;
  canWork: boolean;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const [file, setFile] = useState<File | null>(null);
  const [result, setResult] = useState<DicomIngestResult | null>(null);

  return (
    <div>
      <h3>DICOM ingest</h3>
      <p className="hint">
        Archive one DICOM object from a modality or PACS export. Ingest is
        for <strong>ordered studies</strong>: the object's Study Instance UID
        must match an existing imaging study and its Patient ID must match
        that study's patient — anything else is refused with the reason.
        The exact received bytes are archived content-addressed by SHA-256;
        re-ingesting the same instance with identical bytes is idempotent,
        and with different bytes is refused.
      </p>
      {!canWork && (
        <p className="hint">
          Ingesting DICOM requires the radiology.work capability, so the
          ingest action is disabled for your role. Archived studies stay
          readable from the Worklist tab's study detail.
        </p>
      )}
      <div className="inline-form">
        <label>
          DICOM file{" "}
          <input
            aria-label="DICOM file"
            type="file"
            accept=".dcm,application/dicom"
            disabled={!canWork}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setResult(null);
            }}
          />
        </label>
        <button
          type="button"
          disabled={busy || !canWork || !file}
          onClick={() => {
            if (!file) return;
            void perform("DICOM ingested", async () => {
              setResult(null);
              const base64 = await fileToBase64(file);
              setResult(await ingestDicom(client, base64));
            });
          }}
        >
          Ingest DICOM file
        </button>
      </div>
      {file && (
        <p className="hint">
          Selected: {file.name} ({file.size} bytes) — ready to ingest.
        </p>
      )}
      {result && (
        <div className="board-card">
          <p>
            <StatusBadge tone="ok">
              {result.already_archived ? "Already archived" : "Archived"}
            </StatusBadge>{" "}
            {result.modality} instance {result.instance_number ?? "—"} of
            series {result.series_number ?? "—"} — {result.size_bytes} bytes
          </p>
          <table className="table">
            <tbody>
              <tr><th>Study UID</th><td>{result.study_uid}</td></tr>
              <tr><th>Series UID</th><td>{result.series_uid}</td></tr>
              <tr><th>SOP Instance UID</th><td>{result.sop_uid}</td></tr>
              <tr><th>Study</th><td>{result.study_id}</td></tr>
              <tr><th>Patient ID</th><td>{result.patient_id}</td></tr>
              <tr><th>Content hash (SHA-256)</th><td>{result.content_hash}</td></tr>
              <tr><th>Archive reference</th><td>{result.cold_ref}</td></tr>
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Modality Worklist panel (Phase 17, WS-RESID — the Phase 15 named UI
// gap; the backend GET /radiology/mwl already existed)
// ---------------------------------------------------------------------------

function dicomDa(raw: unknown): string {
  const s = typeof raw === "string" ? raw : "";
  return /^\d{8}$/.test(s) ? `${s.slice(0, 4)}-${s.slice(4, 6)}-${s.slice(6, 8)}` : s || "—";
}

function dicomTm(raw: unknown): string {
  const s = typeof raw === "string" ? raw : "";
  return /^\d{6}$/.test(s) ? `${s.slice(0, 2)}:${s.slice(2, 4)}:${s.slice(4, 6)}` : s || "—";
}

function patientNameOf(entry: MwlEntry): string {
  const pn = dicomValue(entry, "00100010");
  if (pn && typeof pn === "object" && "Alphabetic" in pn) {
    return String((pn as { Alphabetic?: unknown }).Alphabetic ?? "").replace(/\^/g, " ") || "—";
  }
  return pn != null ? String(pn) : "—";
}

function MwlPanel({ client }: { client: ApiClient }) {
  const [tick, setTick] = useState(0);
  const mwlState = useAsync(() => getModalityWorklist(client), [client, tick]);
  const worklistState = useAsync(
    () => getWorklist(client).catch(() => ({ items: [] as RadWorklistItem[], total: 0 })),
    [client, tick],
  );
  const entries = mwlState.data ?? [];
  // MWL entries exist only for scheduled/arrived studies, but the
  // DICOM JSON carries no workflow state — the status shown is the
  // study's live RIS worklist state, joined on the accession
  // (study id), never inferred from the MWL entry itself.
  const stateByStudy = new Map(
    (worklistState.data?.items ?? []).map((i) => [i.study_id, i.state]),
  );

  return (
    <div>
      <h3>Modality worklist</h3>
      <p className="hint">
        The DICOM Modality Worklist (PS3.18 DICOM JSON) the modalities poll:
        one entry per study in the scheduled or arrived states, with the
        Scheduled Procedure Step's station AE title and scheduled
        date/time. Status is the study's live RIS worklist state.
      </p>
      <button type="button" onClick={() => setTick((t) => t + 1)}>
        Refresh modality worklist
      </button>
      {mwlState.loading && <Loading label="Loading modality worklist…" />}
      {mwlState.error != null && <ErrorView error={mwlState.error} />}
      {!mwlState.loading && !mwlState.error && entries.length === 0 && (
        <Empty label="No studies on the modality worklist (nothing scheduled or arrived)." />
      )}
      {entries.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Patient</th><th>Patient ID</th><th>Accession</th>
              <th>Study UID</th><th>Modality</th><th>Station AE</th>
              <th>Scheduled</th><th>Status</th>
            </tr>
          </thead>
          <tbody>
            {entries.map((entry, idx) => {
              const step = (dicomValue(entry, "00400100") ?? {}) as MwlEntry;
              const studyId = String(dicomValue(entry, "00080050") ?? "");
              const state = stateByStudy.get(studyId);
              return (
                <tr key={`${studyId}-${idx}`}>
                  <td>{patientNameOf(entry)}</td>
                  <td>{String(dicomValue(entry, "00100020") ?? "—")}</td>
                  <td>{studyId || "—"}</td>
                  <td>{String(dicomValue(entry, "0020000D") ?? "—")}</td>
                  <td>{String(dicomValue(entry, "00080060") ?? "—")}</td>
                  <td>{String(dicomValue(step, "00400001") ?? "—")}</td>
                  <td>
                    {dicomDa(dicomValue(step, "00400002"))}{" "}
                    {dicomTm(dicomValue(step, "00400003"))}
                  </td>
                  <td>
                    {state ? (
                      <StatusBadge tone={stateTone(state)}>{state}</StatusBadge>
                    ) : (
                      <StatusBadge tone="neutral">not on RIS worklist</StatusBadge>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}
