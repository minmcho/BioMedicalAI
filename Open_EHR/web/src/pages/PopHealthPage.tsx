import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type {
  ExploreResult,
  RegistryListEntry,
  RegistrySummary,
  Role,
  StratumCell,
} from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

// Phase 3 — population health over the OMOP warehouse. Everything on this
// page is an aggregate computed by /pophealth or /warehouse on the
// server: there are no patient rows here because the API has none to
// give. Strata below the small-cell threshold arrive already suppressed
// ("<5") — the page renders exactly what the server sent.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function StrataList({ title, cells }: { title: string; cells: StratumCell[] }) {
  if (cells.length === 0) return null;
  return (
    <div>
      <h4>{title}</h4>
      <ul className="result-list">
        {cells.map((cell) => (
          <li key={cell.value} className="result-row">
            <span className="result-type">{cell.value}</span>
            <span className="result-meta">
              {cell.suppressed ? (
                <>
                  {"<5"} <span className="badge badge-warn">suppressed</span>
                </>
              ) : (
                cell.count
              )}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function RegistryCard({
  client,
  entry,
  refreshKey,
}: {
  client: ApiClient;
  entry: RegistryListEntry;
  refreshKey: number;
}) {
  const summary = useAsync<RegistrySummary>(
    () => client.registrySummary(entry.key),
    [client, entry.key, refreshKey],
  );
  return (
    <section className="card">
      <h3>{entry.name ?? entry.key}</h3>
      {entry.definition.description && <p className="hint">{entry.definition.description}</p>}
      {summary.loading && <Loading label="Loading registry summary…" />}
      {summary.error != null && <ErrorView error={summary.error} />}
      {summary.data && (
        <>
          <p>
            <strong>{summary.data.member_count}</strong> members
            {summary.data.lab && (
              <>
                {" · "}latest {summary.data.lab.code}{" "}
                {summary.data.lab.comparator} {summary.data.lab.threshold}
                {summary.data.lab.window_days
                  ? ` within ${summary.data.lab.window_days} days`
                  : ""}
                {": "}
                {summary.data.lab.pct_in_range == null
                  ? "no current results"
                  : `${summary.data.lab.pct_in_range}% in range`}{" "}
                ({summary.data.lab.members_with_current_result} with a current result)
              </>
            )}
          </p>
          <StrataList title="By gender" cells={summary.data.stratification.by_gender} />
          <StrataList
            title={`By age band (as of ${summary.data.stratification.as_of})`}
            cells={summary.data.stratification.by_age_band}
          />
          <p className="hint">
            Strata under {summary.data.small_cell_threshold} persons are suppressed by the
            server — small counts are never sent to the browser.
          </p>
        </>
      )}
    </section>
  );
}

export function PopHealthPage({ client, role }: Props) {
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const registries = useAsync(() => client.registries(), [client, tick]);
  const status = useAsync(() => client.warehouseStatus(), [client, tick]);
  const whStatus = status.data;

  const [conditionCode, setConditionCode] = useState("");
  const [labCode, setLabCode] = useState("");
  const [comparator, setComparator] = useState<"<" | "<=" | ">" | ">=" | "==">("<");
  const [threshold, setThreshold] = useState("");
  const [exploreResult, setExploreResult] = useState<ExploreResult | null>(null);
  const [exploreError, setExploreError] = useState<string | null>(null);

  async function onRunEtl() {
    setBusy(true);
    setNotice(null);
    setActionError(null);
    try {
      const stats = await client.runEtl();
      setNotice(
        `ETL run complete: ${stats.processed} events processed, ` +
          `${stats.no_target} no-target, ${stats.dead_letter} dead-lettered.`,
      );
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function onExplore(e: React.FormEvent) {
    e.preventDefault();
    setExploreResult(null);
    setExploreError(null);
    try {
      const result = await client.exploreCohort({
        ...(conditionCode.trim() ? { conditionCode: conditionCode.trim() } : {}),
        ...(labCode.trim()
          ? { labCode: labCode.trim(), comparator, threshold: Number(threshold) }
          : {}),
      });
      setExploreResult(result);
    } catch (err) {
      setExploreError(errorText(err));
    }
  }

  const canRunEtl = role === "admin";

  return (
    <div>
      <h2>Population health</h2>
      <p className="hint">
        Registries are Studio cohort configuration (kind <code>cohort</code>) computed over
        the OMOP analytics warehouse — aggregate counts only, with small-cell suppression.
      </p>
      {notice && (
        <p className="state state-empty" role="status">
          {notice}
        </p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">
          {actionError}
        </div>
      )}

      {registries.loading && <Loading label="Loading registries…" />}
      {registries.error != null && <ErrorView error={registries.error} />}
      {registries.data && registries.data.length === 0 && (
        <Empty label="No published cohort registries. Publish one in Studio configuration (kind: cohort)." />
      )}
      {registries.data?.map((entry) => (
        <RegistryCard key={entry.key} client={client} entry={entry} refreshKey={tick} />
      ))}

      <section className="card">
        <h3>Cohort explorer</h3>
        <p className="hint">
          Count persons matching all filters. The lab filter uses each person&apos;s latest
          result. Counts under the small-cell threshold return suppressed.
        </p>
        <form className="inline-form" onSubmit={onExplore}>
          <label className="field">
            Condition code (SNOMED)
            <input
              value={conditionCode}
              onChange={(e) => setConditionCode(e.target.value)}
              placeholder="e.g. 44054006"
            />
          </label>
          <label className="field">
            Lab code (LOINC)
            <input
              value={labCode}
              onChange={(e) => setLabCode(e.target.value)}
              placeholder="e.g. 4548-4"
            />
          </label>
          <label className="field">
            Comparator
            <select
              value={comparator}
              onChange={(e) => setComparator(e.target.value as typeof comparator)}
            >
              <option value="<">&lt;</option>
              <option value="<=">&lt;=</option>
              <option value=">">&gt;</option>
              <option value=">=">&gt;=</option>
              <option value="==">=</option>
            </select>
          </label>
          <label className="field">
            Threshold
            <input
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              inputMode="decimal"
              placeholder="e.g. 7.0"
            />
          </label>
          <button type="submit">Count cohort</button>
        </form>
        {exploreError && (
          <div className="state state-error" role="alert">
            {exploreError}
          </div>
        )}
        {exploreResult && (
          <p role="status">
            Matching persons:{" "}
            <strong>
              {exploreResult.suppressed ? "<5 (suppressed)" : exploreResult.count}
            </strong>
          </p>
        )}
      </section>

      <section className="card">
        <h3>Warehouse &amp; ETL status</h3>
        {status.loading && <Loading label="Loading warehouse status…" />}
        {status.error != null && <ErrorView error={status.error} />}
        {whStatus && (
          <>
            <p>
              Outbox: <strong>{whStatus.outbox.pending}</strong> pending,{" "}
              <strong>{whStatus.outbox.processed}</strong> processed,{" "}
              <strong>{whStatus.outbox.dead_letter}</strong> dead-lettered.
            </p>
            <p>
              Mapping coverage:{" "}
              <strong>
                {whStatus.mapping_coverage.coverage_pct == null
                  ? "—"
                  : `${whStatus.mapping_coverage.coverage_pct}%`}
              </strong>{" "}
              ({whStatus.mapping_coverage.mapped_rows} of{" "}
              {whStatus.mapping_coverage.total_rows} rows mapped,{" "}
              {whStatus.mapping_coverage.unmapped_rows} unmapped)
            </p>
            <table className="lab-table">
              <thead>
                <tr>
                  <th>OMOP table</th>
                  <th>Rows</th>
                  <th>Mapped</th>
                  <th>Unmapped</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(whStatus.warehouse).map(([table, rows]) => (
                  <tr key={table}>
                    <td>{table}</td>
                    <td>{rows}</td>
                    <td>{whStatus.mapping_coverage.tables[table]?.mapped ?? "—"}</td>
                    <td>{whStatus.mapping_coverage.tables[table]?.unmapped ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {whStatus.dead_letters.length > 0 && (
              <>
                <h4>Dead-lettered events</h4>
                <ul className="result-list">
                  {whStatus.dead_letters.map((d) => (
                    <li key={d.event_id} className="result-row">
                      <span className="result-type">
                        #{d.event_id} {d.resource} v{d.version}
                      </span>
                      <span className="result-meta">{d.error ?? "unknown error"}</span>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {canRunEtl ? (
              <button type="button" onClick={onRunEtl} disabled={busy}>
                {busy ? "Running ETL…" : "Run ETL now"}
              </button>
            ) : (
              <p className="hint">ETL runs are triggered by an administrator.</p>
            )}
          </>
        )}
      </section>
    </div>
  );
}
