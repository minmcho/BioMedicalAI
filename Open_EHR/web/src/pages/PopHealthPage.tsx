import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type {
  CohortExport,
  ExploreResult,
  QualityMeasureEntry,
  QualityMeasureResult,
  RegistryListEntry,
  RegistrySummary,
  Role,
  StratumCell,
} from "../api/types";
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
                  {"<5"} <StatusBadge tone="warn">suppressed</StatusBadge>
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

function MeasureCard({ client, entry }: { client: ApiClient; entry: QualityMeasureEntry }) {
  const result = useAsync<QualityMeasureResult>(
    () => client.qualityMeasureResult(entry.key),
    [client, entry.key],
  );
  return (
    <section className="card">
      <h3>{entry.title ?? entry.name ?? entry.key}</h3>
      {entry.description && <p className="hint">{entry.description}</p>}
      <p className="hint">
        Studio measure config (kind <code>measure</code>), definition v
        {entry.definition_version} · small-cell threshold {entry.small_cell_threshold}
      </p>
      {result.loading && <Loading label="Computing measure…" />}
      {result.error != null && <ErrorView error={result.error} />}
      {result.data &&
        (result.data.suppressed ? (
          <p role="status">
            <StatusBadge tone="warn">small-cell suppressed</StatusBadge> Counts under{" "}
            {result.data.small_cell_threshold} are hidden (denominator{" "}
            {result.data.denominator}, numerator {result.data.numerator}) and no rate is
            shown — a rate would reveal the suppressed numerator.
          </p>
        ) : (
          <p role="status">
            <strong>{result.data.rate}%</strong> — {result.data.numerator} of{" "}
            {result.data.denominator} persons (as of {result.data.as_of})
          </p>
        ))}
    </section>
  );
}

function RegistryCard({
  client,
  entry,
  refreshKey,
  canSnapshot,
  canExport,
}: {
  client: ApiClient;
  entry: RegistryListEntry;
  refreshKey: number;
  canSnapshot: boolean;
  canExport: boolean;
}) {
  const summary = useAsync<RegistrySummary>(
    () => client.registrySummary(entry.key),
    [client, entry.key, refreshKey],
  );
  const [snapTick, setSnapTick] = useState(0);
  const [exported, setExported] = useState<CohortExport | null>(null);
  const [exportUrl, setExportUrl] = useState<string | null>(null);
  const [cardError, setCardError] = useState<string | null>(null);
  const [cardBusy, setCardBusy] = useState(false);
  const snapshots = useAsync(
    () => client.registrySnapshots(entry.key),
    [client, entry.key, snapTick, refreshKey],
  );

  async function onSnapshot() {
    setCardBusy(true);
    setCardError(null);
    try {
      await client.takeRegistrySnapshot(entry.key);
      setSnapTick((t) => t + 1);
    } catch (err) {
      setCardError(errorText(err));
    } finally {
      setCardBusy(false);
    }
  }

  async function onExport() {
    setCardBusy(true);
    setCardError(null);
    try {
      const result = await client.exportRegistry(entry.key);
      setExported(result);
      // Build the download URL eagerly (and defensively — some
      // environments have no URL.createObjectURL; the export itself
      // has still happened and is audited server-side).
      try {
        setExportUrl(
          URL.createObjectURL(
            new Blob([JSON.stringify(result, null, 2)], {
              type: "application/json",
            }),
          ),
        );
      } catch {
        setExportUrl(null);
      }
    } catch (err) {
      setCardError(errorText(err));
    } finally {
      setCardBusy(false);
    }
  }

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
      <h4>Cohort snapshots</h4>
      {snapshots.loading && <Loading label="Loading snapshots…" />}
      {snapshots.data && snapshots.data.snapshots.length === 0 && (
        <p className="hint">No snapshots yet — a snapshot pins the member count at a moment in time.</p>
      )}
      {snapshots.data && snapshots.data.snapshots.length > 0 && (
        <ul className="result-list">
          {snapshots.data.snapshots.map((s) => (
            <li key={s.id} className="result-row">
              <span className="result-type">{s.taken_at ?? "—"}</span>
              <span className="result-meta">
                {s.member_count} in cohort · definition v{s.definition_version} · by{" "}
                {s.taken_by}
              </span>
            </li>
          ))}
        </ul>
      )}
      {cardError && (
        <div className="state state-error" role="alert">
          {cardError}
        </div>
      )}
      <div className="inline-form">
        {canSnapshot && (
          <button type="button" onClick={onSnapshot} disabled={cardBusy}>
            Take snapshot
          </button>
        )}
        {canExport && (
          <button type="button" onClick={onExport} disabled={cardBusy}>
            Export aggregates
          </button>
        )}
      </div>
      {exported && (
        <p role="status" className="hint">
          {exported.notice} Exported by {exported.exported_by} · definition v
          {exported.definition_version} · {exported.summary.member_count} members.{" "}
          {exportUrl && (
            <a href={exportUrl} download={`${entry.key}-export.json`}>
              Download export JSON
            </a>
          )}
        </p>
      )}
    </section>
  );
}

export function PopHealthPage({ client, role }: Props) {
  const [tab, setTab] = useState("registries");
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const registries = useAsync(() => client.registries(), [client, tick]);
  const measures = useAsync(() => client.qualityMeasures(), [client, tick]);
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
    if (labCode.trim() && !threshold.trim()) {
      setExploreError("A threshold is required when a lab code is given.");
      return;
    }
    if (labCode.trim() && Number.isNaN(Number(threshold))) {
      setExploreError("The threshold must be a number when a lab code is given.");
      return;
    }
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

  const { permissions } = useRuntimeConfig();
  const canRunEtl = permissions["pophealth.run"]?.[role] === true;
  const canExport = permissions["pophealth.export"]?.[role] === true;

  const registriesPanel = (
    <>
      {registries.loading && <Loading label="Loading registries…" />}
      {registries.error != null && <ErrorView error={registries.error} />}
      {registries.data && registries.data.length === 0 && (
        <Empty label="No published cohort registries. Publish one in Studio configuration (kind: cohort)." />
      )}
      {registries.data?.map((entry) => (
        <RegistryCard
          key={entry.key}
          client={client}
          entry={entry}
          refreshKey={tick}
          canSnapshot={canRunEtl}
          canExport={canExport}
        />
      ))}
    </>
  );

  const measuresPanel = (
    <>
      <h3>Quality measures</h3>
      <p className="hint">
        Measures are Studio configuration (kind <code>measure</code>) evaluated over the
        warehouse by the same cohort evaluator as the registries — numerator inside
        denominator, with small-cell suppression.
      </p>
      {measures.loading && <Loading label="Loading measures…" />}
      {measures.error != null && <ErrorView error={measures.error} />}
      {measures.data && measures.data.measures.length === 0 && (
        <Empty label="No published quality measures. Publish one in Studio configuration (kind: measure)." />
      )}
      {measures.data?.measures.map((entry) => (
        <MeasureCard key={entry.key} client={client} entry={entry} />
      ))}
    </>
  );

  const explorerPanel = (
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
  );

  const warehousePanel = (
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
              <p className="hint">
                Use “Run ETL now” in the page header to trigger a run.
              </p>
            ) : (
              <p className="hint">ETL runs are triggered by an administrator.</p>
            )}
          </>
        )}
      </section>
  );

  return (
    <div>
      <ModuleHeader
        icon="chart"
        group="Insights"
        title="Population health"
        purpose="Registries are Studio cohort configuration (kind cohort) computed over the OMOP analytics warehouse — aggregate counts only, with small-cell suppression."
        primaryAction={
          canRunEtl
            ? {
                label: busy ? "Running ETL…" : "Run ETL now",
                onClick: () => void onRunEtl(),
                disabled: busy,
              }
            : undefined
        }
      />
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
      <Tabs
        ariaLabel="Population health"
        tabs={[
          { key: "registries", label: "Registries" },
          { key: "measures", label: "Quality measures" },
          { key: "explorer", label: "Cohort explorer" },
          { key: "warehouse", label: "Warehouse & ETL" },
        ]}
        activeKey={tab}
        onChange={setTab}
        panels={{
          registries: registriesPanel,
          measures: measuresPanel,
          explorer: explorerPanel,
          warehouse: warehousePanel,
        }}
      />
    </div>
  );
}
