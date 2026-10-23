import { useMemo, useState } from "react";
import { ApiError } from "../api/client";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Lab Depth D1 + D2 — Laboratory compendium & benches.
// Self-contained surface over backend/app/lab_compendium.py:
// - D1 directory browser: every published catalogue test with its
//   governed directory attributes (performing section/site, method,
//   specimen requirements, availability, TAT targets with declared
//   clocks) and age/sex-partitioned reference ranges. The data is
//   published Studio configuration (terminology/lab-catalog); this
//   view is read-only — editing/publishing stays in System Studio.
// - D2 benches: the bench registry (terminology/lab-benches) with
//   per-bench grant state, the bench workload view and the shift
//   handover panel. Workload/handover are gated server-side by the
//   caller's bench grant (lab.bench.<code> on the privileging
//   ledger); a refusal renders here as the server's named 403,
//   never as an empty list.

export interface CompendiumApi {
  request<T>(path: string, init?: RequestInit): Promise<T>;
}

export interface DirectorySpecimen {
  container?: string;
  specimenType?: string;
  minimumVolumeMl?: number;
  fastingRequired?: boolean;
}

export interface DirectoryAvailability {
  mode: string;
  batchSchedule?: { days: string[]; times: string[] };
  referenceLab?: string;
}

export interface TatTarget {
  priority: string;
  clock: string;
  targetMinutes: number;
}

export interface TestDirectory {
  performingSection?: string;
  performingSite?: string;
  method?: string;
  specimen?: DirectorySpecimen;
  availability?: DirectoryAvailability;
  tatTargets?: TatTarget[];
}

export interface RangePartition {
  label?: string;
  sex?: string;
  ageMinYears?: number | null;
  ageMaxYears?: number | null;
  low?: number;
  high?: number;
  criticalLow?: number;
  criticalHigh?: number;
}

export interface DirectoryTest {
  code: string;
  display?: string | null;
  active: boolean;
  bench?: string | null;
  panel: boolean;
  components: string[];
  unit?: string | null;
  qualitative: boolean;
  values?: string[] | null;
  reference_range?: {
    low?: number;
    high?: number;
    criticalLow?: number;
    criticalHigh?: number;
    unit?: string;
  } | null;
  reference_range_partitions?: RangePartition[] | null;
  directory?: TestDirectory | null;
}

export interface BenchGrant {
  grant_id: string;
  staff_id: string;
  granted_by: string;
  granted_at?: string | null;
  note?: string;
}

export interface Bench {
  code: string;
  name?: string | null;
  section?: string | null;
  location?: string | null;
  supervisor_staff_id?: string | null;
  hours?: { days: string[]; opensAt: string; closesAt: string } | null;
  grant_code: string;
  caller_has_grant: boolean;
  grants: BenchGrant[];
}

export interface WorkloadItem {
  order_id: string;
  status?: string | null;
  code?: string | null;
  patient_ref?: string | null;
  priority?: string | null;
  authored_on?: string | null;
}

export interface Workload {
  bench: string;
  counts_by_status: Record<string, number>;
  open_total: number;
  open_items: WorkloadItem[];
}

export interface HandoverNote {
  note_id: string;
  bench_code: string;
  note: string;
  author: string;
  status: string;
  acknowledged_by?: string | null;
  acknowledged_at?: string | null;
  created_at?: string | null;
}

export interface HandoverView {
  bench: string;
  open_items: WorkloadItem[];
  open_total: number;
  counts_by_status: Record<string, number>;
  notes: HandoverNote[];
  open_notes: number;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError)
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function availabilityText(av?: DirectoryAvailability | null): string {
  if (!av) return "Not published";
  if (av.mode === "24-7") return "24/7";
  if (av.mode === "batched") {
    const days = av.batchSchedule?.days?.join(", ") ?? "";
    const times = av.batchSchedule?.times?.join(", ") ?? "";
    return `Batched — ${days} at ${times}`;
  }
  if (av.mode === "send-out")
    return `Send-out — ${av.referenceLab ?? "reference laboratory"}`;
  return av.mode;
}

function rangeText(low?: number, high?: number, unit?: string | null): string {
  if (low === undefined && high === undefined) return "No range published";
  return `${low ?? "…"} – ${high ?? "…"}${unit ? ` ${unit}` : ""}`;
}

export function TestDirectoryDetail({ test }: { test: DirectoryTest }) {
  const dir = test.directory;
  const partitions = test.reference_range_partitions ?? [];
  return (
    <div className="compendium-detail">
      <h3>
        {test.display ?? test.code} <span className="code-chip">{test.code}</span>
      </h3>
      {!dir && (
        <Empty label="No directory attributes published for this test yet — publish them in System Studio (Terminology → lab-catalog)." />
      )}
      {dir && (
        <dl className="detail-grid">
          <dt>Performing section</dt>
          <dd>{dir.performingSection ?? "Not published"}</dd>
          <dt>Performing site</dt>
          <dd>{dir.performingSite ?? "Not published"}</dd>
          <dt>Method</dt>
          <dd>{dir.method ?? "Not published"}</dd>
          <dt>Availability</dt>
          <dd>{availabilityText(dir.availability)}</dd>
          <dt>Specimen type</dt>
          <dd>{dir.specimen?.specimenType ?? "Not specified"}</dd>
          <dt>Container</dt>
          <dd>{dir.specimen?.container ?? "Not specified"}</dd>
          <dt>Minimum volume</dt>
          <dd>
            {dir.specimen?.minimumVolumeMl !== undefined
              ? `${dir.specimen.minimumVolumeMl} mL`
              : "Not specified"}
          </dd>
          <dt>Fasting</dt>
          <dd>
            {dir.specimen?.fastingRequired === undefined
              ? "Not specified"
              : dir.specimen.fastingRequired
                ? "Fasting specimen required"
                : "No fasting requirement"}
          </dd>
        </dl>
      )}
      {dir?.tatTargets && dir.tatTargets.length > 0 && (
        <>
          <h4>Turnaround targets</h4>
          <table className="data-table">
            <thead>
              <tr>
                <th>Priority</th>
                <th>Clock</th>
                <th>Target (minutes)</th>
              </tr>
            </thead>
            <tbody>
              {dir.tatTargets.map((t) => (
                <tr key={`${t.priority}-${t.clock}`}>
                  <td>{t.priority}</td>
                  <td>{t.clock}</td>
                  <td>{t.targetMinutes}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h4>Reference range</h4>
      <p>
        Default:{" "}
        {rangeText(
          test.reference_range?.low,
          test.reference_range?.high,
          test.reference_range?.unit ?? test.unit,
        )}
        {test.reference_range?.criticalLow !== undefined ||
        test.reference_range?.criticalHigh !== undefined
          ? ` (critical ${test.reference_range?.criticalLow ?? "…"} – ${test.reference_range?.criticalHigh ?? "…"})`
          : ""}
      </p>
      {partitions.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>Partition</th>
              <th>Sex</th>
              <th>Age (years)</th>
              <th>Range</th>
            </tr>
          </thead>
          <tbody>
            {partitions.map((p, i) => (
              <tr key={p.label ?? i}>
                <td>{p.label ?? "Partition"}</td>
                <td>{p.sex ?? "any"}</td>
                <td>
                  {p.ageMinYears ?? 0} – {p.ageMaxYears ?? "∞"}
                </td>
                <td>{rangeText(p.low, p.high, test.unit)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {test.panel && test.components.length > 0 && (
        <p>Panel components: {test.components.join(", ")}</p>
      )}
    </div>
  );
}

export function LabCompendium({ api }: { api: CompendiumApi }) {
  const directoryState = useAsync(
    () => api.request<{ tests: DirectoryTest[]; total: number }>("/lab/compendium/directory"),
    [],
  );
  const benchesState = useAsync(
    () => api.request<{ benches: Bench[]; total: number }>("/lab/compendium/benches"),
    [],
  );
  const [filter, setFilter] = useState("");
  const [benchFilter, setBenchFilter] = useState("");
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [benchCode, setBenchCode] = useState("");
  const [workload, setWorkload] = useState<Workload | null>(null);
  const [handover, setHandover] = useState<HandoverView | null>(null);
  const [benchError, setBenchError] = useState<string | null>(null);
  const [benchBusy, setBenchBusy] = useState(false);
  const [noteText, setNoteText] = useState("");
  const [noteError, setNoteError] = useState<string | null>(null);

  const tests = useMemo(
    () => directoryState.data?.tests ?? [],
    [directoryState.data],
  );
  const benchOptions = useMemo(() => {
    const fromRegistry = (benchesState.data?.benches ?? []).map((b) => b.code);
    if (fromRegistry.length > 0) return fromRegistry;
    return [...new Set(tests.map((t) => t.bench).filter(Boolean))] as string[];
  }, [benchesState.data, tests]);
  const filtered = tests.filter(
    (t) =>
      (!benchFilter || t.bench === benchFilter) &&
      (!filter ||
        t.code.toLowerCase().includes(filter.toLowerCase()) ||
        (t.display ?? "").toLowerCase().includes(filter.toLowerCase())),
  );
  const selected =
    tests.find((t) => t.code === selectedCode) ?? filtered[0] ?? null;
  const activeBench =
    benchesState.data?.benches.find((b) => b.code === benchCode) ?? null;

  async function loadBench(code: string) {
    setBenchCode(code);
    setWorkload(null);
    setHandover(null);
    setBenchError(null);
    if (!code) return;
    setBenchBusy(true);
    try {
      const [w, h] = await Promise.all([
        api.request<Workload>(`/lab/compendium/benches/${code}/workload`),
        api.request<HandoverView>(`/lab/compendium/benches/${code}/handover`),
      ]);
      setWorkload(w);
      setHandover(h);
    } catch (err) {
      setBenchError(errorText(err));
    } finally {
      setBenchBusy(false);
    }
  }

  async function submitNote() {
    if (!noteText.trim()) {
      setNoteError("Write the handover note before submitting — an empty note hands over nothing.");
      return;
    }
    setNoteError(null);
    setBenchBusy(true);
    try {
      await api.request(`/lab/compendium/benches/${benchCode}/handover`, {
        method: "POST",
        body: JSON.stringify({ note: noteText.trim() }),
      });
      setNoteText("");
      await loadBench(benchCode);
    } catch (err) {
      setNoteError(errorText(err));
    } finally {
      setBenchBusy(false);
    }
  }

  async function acknowledge(noteId: string) {
    setBenchBusy(true);
    setNoteError(null);
    try {
      await api.request(
        `/lab/compendium/benches/${benchCode}/handover/${noteId}/acknowledge`,
        { method: "POST", body: JSON.stringify({}) },
      );
      await loadBench(benchCode);
    } catch (err) {
      setNoteError(errorText(err));
    } finally {
      setBenchBusy(false);
    }
  }

  return (
    <div className="lab-compendium">
      <section aria-label="Test directory">
        <h2>Test compendium &amp; directory</h2>
        <p className="section-note">
          Published catalogue attributes (System Studio → Terminology →
          lab-catalog). Specimen requirements shown here are enforced at
          order placement; partitioned ranges govern interpretation.
        </p>
        {directoryState.loading && <Loading label="Loading test directory…" />}
        {directoryState.error != null && (
          <ErrorView error={directoryState.error} />
        )}
        {directoryState.data && (
          <>
            <div className="filter-row">
              <label>
                Search{" "}
                <input
                  aria-label="Search tests"
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="Code or name"
                />
              </label>
              <label>
                Bench{" "}
                <select
                  aria-label="Filter by bench"
                  value={benchFilter}
                  onChange={(e) => setBenchFilter(e.target.value)}
                >
                  <option value="">All benches</option>
                  {benchOptions.map((code) => (
                    <option key={code} value={code}>
                      {code}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            {filtered.length === 0 && (
              <Empty label="No tests match this filter in the published catalogue." />
            )}
            {filtered.length > 0 && (
              <div className="compendium-layout">
                <ul className="test-list">
                  {filtered.map((t) => (
                    <li key={t.code}>
                      <button
                        type="button"
                        className={selected?.code === t.code ? "selected" : ""}
                        onClick={() => setSelectedCode(t.code)}
                      >
                        {t.display ?? t.code} ({t.code})
                      </button>
                    </li>
                  ))}
                </ul>
                {selected && <TestDirectoryDetail test={selected} />}
              </div>
            )}
          </>
        )}
      </section>

      <section aria-label="Benches">
        <h2>Benches &amp; shift handover</h2>
        {benchesState.loading && <Loading label="Loading benches…" />}
        {benchesState.error != null && <ErrorView error={benchesState.error} />}
        {benchesState.data && (
          <>
            <label>
              Bench{" "}
              <select
                aria-label="Select bench"
                value={benchCode}
                onChange={(e) => void loadBench(e.target.value)}
              >
                <option value="">Select a bench…</option>
                {benchesState.data.benches.map((b) => (
                  <option key={b.code} value={b.code}>
                    {b.name ?? b.code} ({b.code})
                  </option>
                ))}
              </select>
            </label>
            {activeBench && (
              <div className="bench-meta">
                <p>
                  Section: {activeBench.section ?? "—"} · Location:{" "}
                  {activeBench.location ?? "—"} · Supervisor (staff id):{" "}
                  {activeBench.supervisor_staff_id ?? "—"}
                </p>
                <p>
                  Hours:{" "}
                  {activeBench.hours
                    ? `${activeBench.hours.days.join(", ")} ${activeBench.hours.opensAt}–${activeBench.hours.closesAt}`
                    : "Not published"}
                </p>
                <p>
                  Your bench grant ({activeBench.grant_code}):{" "}
                  {activeBench.caller_has_grant
                    ? "held"
                    : "NOT held — workload and handover require it"}
                </p>
                {activeBench.grants.length > 0 && (
                  <ul>
                    {activeBench.grants.map((g) => (
                      <li key={g.grant_id}>
                        Grant {g.grant_id}: {g.staff_id} (granted by{" "}
                        {g.granted_by})
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            {benchBusy && <Loading label="Loading bench…" />}
            {benchError && (
              <p className="inline-error" role="alert">
                {benchError}
              </p>
            )}
            {workload && (
              <div className="bench-workload">
                <h3>Workload — {workload.bench}</h3>
                <p>Open items: {workload.open_total}</p>
                <ul>
                  {Object.entries(workload.counts_by_status).map(
                    ([status, count]) => (
                      <li key={status}>
                        {status}: {count}
                      </li>
                    ),
                  )}
                </ul>
              </div>
            )}
            {handover && (
              <div className="bench-handover">
                <h3>Shift handover — {handover.bench}</h3>
                <p>
                  Open work items: {handover.open_total} · Open notes:{" "}
                  {handover.open_notes}
                </p>
                <ul className="handover-items">
                  {handover.open_items.map((item) => (
                    <li key={item.order_id}>
                      {item.code ?? "order"} {item.order_id} —{" "}
                      {item.status ?? ""} ({item.patient_ref ?? ""})
                    </li>
                  ))}
                </ul>
                <ul className="handover-notes">
                  {handover.notes.map((n) => (
                    <li key={n.note_id}>
                      <span>
                        {n.note} — {n.author} ({n.status})
                      </span>
                      {n.status === "open" && (
                        <button
                          type="button"
                          disabled={benchBusy}
                          onClick={() => void acknowledge(n.note_id)}
                        >
                          Acknowledge
                        </button>
                      )}
                    </li>
                  ))}
                </ul>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void submitNote();
                  }}
                >
                  <label>
                    Handover note{" "}
                    <textarea
                      aria-label="Handover note"
                      value={noteText}
                      onChange={(e) => setNoteText(e.target.value)}
                      rows={3}
                    />
                  </label>
                  {noteError && (
                    <p className="inline-error" role="alert">
                      {noteError}
                    </p>
                  )}
                  <button type="submit" disabled={benchBusy || !benchCode}>
                    Add handover note
                  </button>
                </form>
              </div>
            )}
          </>
        )}
      </section>
    </div>
  );
}
