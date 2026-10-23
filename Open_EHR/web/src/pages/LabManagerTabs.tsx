import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { saveDownloadedFile } from "../api/download";
import {
  addTestToSpecimen,
  composePanelDraft,
  composeTestDraft,
  deriveSpecimen,
  getManagerCatalog,
  getSpecimenChain,
  listCustodyBreaks,
  listSpecimenBarcodes,
  printSpecimenLabel,
  scanSpecimen,
  type ManagerCatalog,
  type SpecimenNode,
} from "../api/labDepth";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

// Phase 15 (scope B) — Laboratory completion tabs. The Test Manager
// ("Test Catalogue") and Panel Manager ("Panels") live INSIDE the
// Laboratory module, in the management cluster alongside QC & EQA.
// Both compose onto the GOVERNED catalogue DRAFT only
// (terminology/lab-catalog); publishing happens in System Studio and
// composition is authorised for the laboratory-director job role (or
// admin/configurator) — a 403 renders the read-only state below.
// "Specimens" is the aliquot / chain-of-custody view over
// backend/app/lab_specimens.py.

const DRAFT_ONLY_NOTICE =
  "Draft-only: saving composes the change onto the governed catalogue " +
  "draft (terminology/lab-catalog). Nothing is published from here — " +
  "the draft is reviewed and published in System Studio.";

function errorText(err: unknown): string {
  if (err instanceof ApiError)
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

function isForbidden(err: unknown): boolean {
  return err instanceof ApiError && err.status === 403;
}

/** The 403 read-only state for the manager tabs: composition is a
 *  laboratory-director (or admin/configurator) act on the server. */
function ComposerForbidden({ detail }: { detail: string }) {
  return (
    <div className="state state-error" role="alert">
      <strong>Read-only — laboratory director approval required.</strong>{" "}
      Catalogue composition requires the laboratory director job role
      (&lsquo;lab-director&rsquo;) or the admin/configurator role; your
      account holds neither, so this tab is read-only for you. The
      laboratory director reviews and publishes catalogue drafts in
      System Studio. Server detail: {detail}
    </div>
  );
}

function aliquotText(rules?: { count: number; volumeMl: number; container: string }): string {
  if (!rules) return "—";
  return `${rules.count} × ${rules.volumeMl} mL (${rules.container})`;
}

// ---------------------------------------------------------------------------
// Test Catalogue (Test Manager)
// ---------------------------------------------------------------------------

export function TestCatalogueTab({ client }: { client: ApiClient }) {
  const [tick, setTick] = useState(0);
  const state = useAsync<ManagerCatalog>(() => getManagerCatalog(client), [client, tick]);
  const [showForm, setShowForm] = useState(false);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    code: "", display: "", bench: "", unit: "",
    specimen_type: "", container: "standard tube",
    aliquot_count: "1", aliquot_volume: "5",
  });

  async function submit() {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const outcome = await composeTestDraft(client, {
        code: form.code.trim(),
        display: form.display.trim(),
        ...(form.bench.trim() ? { bench: form.bench.trim() } : {}),
        ...(form.unit.trim() ? { unit: form.unit.trim() } : {}),
        ...(form.specimen_type.trim()
          ? { specimen_type: form.specimen_type.trim() }
          : {}),
        ...(form.container.trim() ? { container: form.container.trim() } : {}),
        aliquot_rules: {
          count: Number(form.aliquot_count),
          volumeMl: Number(form.aliquot_volume),
          container: form.container.trim() || "standard tube",
        },
      });
      setNotice(
        `Draft change saved onto catalogue draft v${outcome.draft.version} — ` +
          "not published; publishing stays in System Studio.",
      );
      setShowForm(false);
      setTick((t) => t + 1);
    } catch (err) {
      if (isForbidden(err)) setForbidden(errorText(err));
      else setActionError(`Propose change failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const data = state.data;
  return (
    <>
      <h3>Test Catalogue</h3>
      <p className="hint">
        Test Manager over the published lab catalogue
        {data?.catalog_version ? ` (terminology/lab-catalog v${data.catalog_version})` : ""}:
        specimen/container requirements and aliquot rules per test.
      </p>
      <p className="hint">{DRAFT_ONLY_NOTICE}</p>
      {data?.draft && (
        <p className="hint" role="status">
          An unpublished catalogue draft exists (v{data.draft.version},
          updated by {data.draft.updated_by}) — it takes effect only when
          published in System Studio.
        </p>
      )}
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      {forbidden ? (
        <ComposerForbidden detail={forbidden} />
      ) : !showForm ? (
        <p>
          <button type="button" onClick={() => setShowForm(true)}>
            Propose change
          </button>
        </p>
      ) : (
        <form
          className="config-editor"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="field">
            Test code
            <input
              value={form.code}
              onChange={(e) => setForm((f) => ({ ...f, code: e.target.value }))}
            />
          </label>
          <label className="field">
            Display name
            <input
              value={form.display}
              onChange={(e) => setForm((f) => ({ ...f, display: e.target.value }))}
            />
          </label>
          <label className="field">
            Bench
            <input
              value={form.bench}
              onChange={(e) => setForm((f) => ({ ...f, bench: e.target.value }))}
            />
          </label>
          <label className="field">
            Unit
            <input
              value={form.unit}
              onChange={(e) => setForm((f) => ({ ...f, unit: e.target.value }))}
            />
          </label>
          <label className="field">
            Specimen type
            <input
              value={form.specimen_type}
              onChange={(e) => setForm((f) => ({ ...f, specimen_type: e.target.value }))}
            />
          </label>
          <label className="field">
            Container
            <input
              value={form.container}
              onChange={(e) => setForm((f) => ({ ...f, container: e.target.value }))}
            />
          </label>
          <label className="field">
            Aliquot count
            <input
              inputMode="numeric"
              value={form.aliquot_count}
              onChange={(e) => setForm((f) => ({ ...f, aliquot_count: e.target.value }))}
            />
          </label>
          <label className="field">
            Aliquot volume (mL)
            <input
              inputMode="decimal"
              value={form.aliquot_volume}
              onChange={(e) => setForm((f) => ({ ...f, aliquot_volume: e.target.value }))}
            />
          </label>
          <button
            type="submit"
            disabled={busy || !form.code.trim() || !form.display.trim()}
          >
            Save draft change
          </button>
        </form>
      )}

      {state.loading ? (
        <Loading label="Loading test catalogue…" />
      ) : state.error ? (
        <ErrorView error={state.error} />
      ) : !data || data.tests.length === 0 ? (
        <Empty label="No tests in the published catalogue." />
      ) : (
        <table className="lab-table">
          <thead>
            <tr>
              <th>Code</th><th>Test</th><th>Bench</th>
              <th>Specimen / container</th><th>Aliquot rules</th><th>Reflex</th>
            </tr>
          </thead>
          <tbody>
            {data.tests.map((t) => (
              <tr key={t.code}>
                <td>{t.code}</td>
                <td>
                  {t.display ?? t.code}
                  {t.active === false && (
                    <> <StatusBadge tone="neutral">inactive</StatusBadge></>
                  )}
                </td>
                <td>{t.bench ?? "—"}</td>
                <td>
                  {t.specimen_requirements?.specimen_type ?? "—"} /{" "}
                  {t.specimen_requirements?.container ?? "—"}
                </td>
                <td>
                  {aliquotText(t.aliquot_rules)}
                  {t.aliquot_rules_source === "default" && (
                    <> <StatusBadge tone="neutral">default rules</StatusBadge></>
                  )}
                </td>
                <td>
                  {(t.reflex_rules ?? []).length > 0
                    ? (t.reflex_rules ?? [])
                        .map((r) => `${r.triggerCode ?? ""} → ${r.addCode ?? ""}`)
                        .join("; ")
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Panels (Panel Manager)
// ---------------------------------------------------------------------------

export function PanelsTab({ client }: { client: ApiClient }) {
  const [tick, setTick] = useState(0);
  const state = useAsync<ManagerCatalog>(() => getManagerCatalog(client), [client, tick]);
  const [showForm, setShowForm] = useState(false);
  const [forbidden, setForbidden] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState({
    code: "", display: "", components: "", bench: "",
    specimen_type: "", container: "standard tube",
    aliquot_count: "1", aliquot_volume: "5",
  });

  async function submit() {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const outcome = await composePanelDraft(client, {
        code: form.code.trim(),
        display: form.display.trim(),
        components: form.components
          .split(",")
          .map((c) => c.trim())
          .filter(Boolean),
        ...(form.bench.trim() ? { bench: form.bench.trim() } : {}),
        ...(form.specimen_type.trim()
          ? { specimen_type: form.specimen_type.trim() }
          : {}),
        aliquot_rules: {
          count: Number(form.aliquot_count),
          volumeMl: Number(form.aliquot_volume),
          container: form.container.trim() || "standard tube",
        },
      });
      setNotice(
        `Panel draft change saved onto catalogue draft v${outcome.draft.version} — ` +
          "not published; publishing stays in System Studio.",
      );
      setShowForm(false);
      setTick((t) => t + 1);
    } catch (err) {
      if (isForbidden(err)) setForbidden(errorText(err));
      else setActionError(`Propose panel change failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  const data = state.data;
  return (
    <>
      <h3>Panels</h3>
      <p className="hint">
        Panel Manager: panel definitions on the published catalogue —
        component tests, reflex rules, specimen requirements and the
        required aliquots per panel.
      </p>
      <p className="hint">{DRAFT_ONLY_NOTICE}</p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      {forbidden ? (
        <ComposerForbidden detail={forbidden} />
      ) : !showForm ? (
        <p>
          <button type="button" onClick={() => setShowForm(true)}>
            Propose panel change
          </button>
        </p>
      ) : (
        <form
          className="config-editor"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <label className="field">
            Panel code
            <input
              value={form.code}
              onChange={(e) => setForm((f) => ({ ...f, code: e.target.value }))}
            />
          </label>
          <label className="field">
            Display name
            <input
              value={form.display}
              onChange={(e) => setForm((f) => ({ ...f, display: e.target.value }))}
            />
          </label>
          <label className="field">
            Component test codes (comma-separated)
            <input
              value={form.components}
              onChange={(e) => setForm((f) => ({ ...f, components: e.target.value }))}
            />
          </label>
          <label className="field">
            Bench
            <input
              value={form.bench}
              onChange={(e) => setForm((f) => ({ ...f, bench: e.target.value }))}
            />
          </label>
          <label className="field">
            Specimen type
            <input
              value={form.specimen_type}
              onChange={(e) => setForm((f) => ({ ...f, specimen_type: e.target.value }))}
            />
          </label>
          <label className="field">
            Container
            <input
              value={form.container}
              onChange={(e) => setForm((f) => ({ ...f, container: e.target.value }))}
            />
          </label>
          <label className="field">
            Required aliquots
            <input
              inputMode="numeric"
              value={form.aliquot_count}
              onChange={(e) => setForm((f) => ({ ...f, aliquot_count: e.target.value }))}
            />
          </label>
          <label className="field">
            Aliquot volume (mL)
            <input
              inputMode="decimal"
              value={form.aliquot_volume}
              onChange={(e) => setForm((f) => ({ ...f, aliquot_volume: e.target.value }))}
            />
          </label>
          <button
            type="submit"
            disabled={
              busy || !form.code.trim() || !form.display.trim() || !form.components.trim()
            }
          >
            Save panel draft
          </button>
        </form>
      )}

      {state.loading ? (
        <Loading label="Loading panels…" />
      ) : state.error ? (
        <ErrorView error={state.error} />
      ) : !data || data.panels.length === 0 ? (
        <Empty label="No panels in the published catalogue." />
      ) : (
        <ul className="result-list">
          {data.panels.map((p) => (
            <li key={p.code} className="result-row">
              <span className="result-type">panel</span>
              <span>
                {p.display ?? p.code} ({p.code})
                {p.bench ? ` — ${p.bench}` : ""}
                <br />
                <span className="result-meta">
                  Components:{" "}
                  {p.components
                    .map((c) => c.display ?? c.code)
                    .join(", ") || p.component_codes.join(", ")}
                </span>
                <br />
                <span className="result-meta">
                  Reflex:{" "}
                  {(p.reflex_rules ?? []).length > 0
                    ? (p.reflex_rules ?? [])
                        .map(
                          (r) =>
                            `${r.triggerCode ?? ""}${r.when ? ` (${r.when})` : ""} → ${r.addCode ?? ""}`,
                        )
                        .join("; ")
                    : "none"}
                </span>
                <br />
                <span className="result-meta">
                  Specimen: {p.specimen_requirements?.specimen_type ?? "—"} /{" "}
                  {p.specimen_requirements?.container ?? "—"} · Required
                  aliquots: {aliquotText(p.aliquot_rules)}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Specimens — aliquots + family chain of custody
// ---------------------------------------------------------------------------

// The published specimen-lifecycle action vocabulary's built-in
// fallback (terminology/specimen-lifecycle, backend default). The
// server validates every scan against the published config.
const SCAN_ACTIONS = [
  "collection", "transport", "receipt", "bench-transfer",
  "storage", "send-out-dispatch", "disposal",
];

function SpecimenTree({ node, depth = 0 }: { node: SpecimenNode; depth?: number }) {
  return (
    <li>
      <span>
        {node.specimen_key} — {node.derivation_type}
        {node.sequence ? ` #${node.sequence}` : ""}{" "}
        <StatusBadge tone="neutral">{node.status}</StatusBadge>{" "}
        <span className="result-meta">
          barcode {node.barcode ?? "—"} · {node.remaining_volume_ml} mL
          remaining of {node.volume_ml} mL · {node.container}
        </span>
      </span>
      {(node.children ?? []).length > 0 && (
        <ul>
          {(node.children ?? []).map((child) => (
            <SpecimenTree key={child.specimen_key} node={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export function SpecimensTab({ client }: { client: ApiClient }) {
  const [tick, setTick] = useState(0);
  // Guarded like the LabPage's other sections: a client without the
  // generic request surface (older test doubles) renders the empty
  // state instead of throwing inside the effect.
  const hasRequest = typeof client.request === "function";
  const barcodesState = useAsync(
    () =>
      hasRequest
        ? listSpecimenBarcodes(client)
        : Promise.resolve({ items: [], total: 0 }),
    [client, tick],
  );
  const breaksState = useAsync(
    () =>
      hasRequest
        ? listCustodyBreaks(client)
        : Promise.resolve({ items: [], total: 0, families: [], family_count: 0 }),
    [client, tick],
  );
  const [selected, setSelected] = useState<string>("");
  const chainState = useAsync(
    () =>
      selected && hasRequest
        ? getSpecimenChain(client, selected)
        : Promise.resolve(null),
    [client, selected, tick],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deriveForm, setDeriveForm] = useState({
    type: "aliquot", count: "1", volume_ml: "2", container: "",
  });
  const [addonCode, setAddonCode] = useState("");
  const [scanForm, setScanForm] = useState({ action: "receipt", location: "" });

  const items = barcodesState.data?.items ?? [];
  useEffect(() => {
    if (!selected && items.length > 0) setSelected(items[0].specimen_key);
  }, [items, selected]);

  const chain = chainState.data;
  const member =
    chain?.members.find((m) => m.specimen_key === selected) ??
    chain?.members.find((m) => m.specimen_key === chain.requested_member) ??
    chain?.tree ??
    null;

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

  const breaks = breaksState.data;
  return (
    <>
      <h3>Specimen aliquots &amp; chain of custody</h3>
      <p className="hint">
        Every specimen and aliquot carries its own barcode. Selecting any
        family member shows the whole parent → child lineage, each
        member&rsquo;s custody timeline, and any custody breaks.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      <h4>Custody-break worklist</h4>
      {breaksState.loading ? (
        <Loading label="Loading custody breaks…" />
      ) : breaksState.error ? (
        <ErrorView error={breaksState.error} />
      ) : !breaks || breaks.total === 0 ? (
        <Empty label="No custody breaks recorded." />
      ) : (
        <ul className="result-list">
          {breaks.items.map((b, i) => (
            <li key={`${b.specimen_key}-${b.action}-${i}`} className="result-row">
              <StatusBadge tone="bad">Custody break</StatusBadge>
              <span>
                {b.specimen_key} — {b.action}
                <br />
                <span className="result-meta">
                  {b.reason ?? "Missing predecessor event"}
                  {b.location ? ` · at ${b.location}` : ""}
                  {b.actor ? ` · by ${b.actor}` : ""}
                </span>
              </span>
            </li>
          ))}
        </ul>
      )}

      {barcodesState.loading ? (
        <Loading label="Loading specimens…" />
      ) : barcodesState.error ? (
        <ErrorView error={barcodesState.error} />
      ) : items.length === 0 ? (
        <Empty label="No specimens or aliquots registered yet." />
      ) : (
        <div style={{ display: "flex", gap: "1rem", alignItems: "flex-start" }}>
          <div style={{ minWidth: "16rem" }}>
            <h4>Specimens &amp; aliquots</h4>
            <ul className="result-list">
              {items.map((item) => (
                <li key={item.value} className="result-row">
                  <span>
                    {item.specimen_key}
                    <br />
                    <span className="result-meta">
                      {item.kind} · {item.value}
                    </span>
                  </span>
                  <button
                    type="button"
                    aria-label={`View specimen ${item.specimen_key}`}
                    onClick={() => setSelected(item.specimen_key)}
                  >
                    View chain
                  </button>
                </li>
              ))}
            </ul>
          </div>
          <div style={{ flex: 1 }}>
            <h4>Family chain</h4>
            {chainState.loading ? (
              <Loading label="Loading specimen chain…" />
            ) : chainState.error ? (
              <ErrorView error={chainState.error} />
            ) : !chain || !member ? (
              <Empty label="Select a specimen to see its family chain." />
            ) : (
              <>
                <p className="hint">
                  Family root {chain.root_key} · {chain.total} member(s)
                  {chain.breaks.length > 0
                    ? ` · ${chain.breaks.length} custody break(s) in this family`
                    : " · no custody breaks in this family"}
                </p>
                <ul>
                  <SpecimenTree node={chain.tree} />
                </ul>

                <h4>Custody timeline — {member.specimen_key}</h4>
                {member.custody.length === 0 ? (
                  <Empty label="No custody events for this member yet." />
                ) : (
                  <ul className="result-list">
                    {member.custody.map((event) => (
                      <li key={event.id} className="result-row">
                        <span>
                          {event.action}
                          {event.is_break && (
                            <>
                              {" "}
                              <StatusBadge tone="bad">Custody break</StatusBadge>
                            </>
                          )}
                          <br />
                          <span className="result-meta">
                            {event.actor}
                            {event.location ? ` · ${event.location}` : ""}
                            {event.at ? ` · ${new Date(event.at).toLocaleString()}` : ""}
                            {event.break_reason ? ` · ${event.break_reason}` : ""}
                          </span>
                        </span>
                      </li>
                    ))}
                  </ul>
                )}
                <p className="hint">Barcode: {member.barcode ?? "—"}</p>

                <form
                  className="config-editor"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void perform(`Derive from ${member.specimen_key}`, () =>
                      deriveSpecimen(client, member.specimen_key, {
                        type: deriveForm.type,
                        count: Number(deriveForm.count),
                        volume_ml: Number(deriveForm.volume_ml),
                        ...(deriveForm.container.trim()
                          ? { container: deriveForm.container.trim() }
                          : {}),
                      }),
                    );
                  }}
                >
                  <label className="field">
                    Derivation type
                    <select
                      value={deriveForm.type}
                      onChange={(e) => setDeriveForm((f) => ({ ...f, type: e.target.value }))}
                    >
                      <option value="aliquot">Aliquot</option>
                      <option value="split">Split</option>
                      <option value="subculture">Subculture</option>
                      <option value="isolate">Isolate</option>
                    </select>
                  </label>
                  <label className="field">
                    Number of aliquots
                    <input
                      inputMode="numeric"
                      value={deriveForm.count}
                      onChange={(e) => setDeriveForm((f) => ({ ...f, count: e.target.value }))}
                    />
                  </label>
                  <label className="field">
                    Volume per aliquot (mL)
                    <input
                      inputMode="decimal"
                      value={deriveForm.volume_ml}
                      onChange={(e) => setDeriveForm((f) => ({ ...f, volume_ml: e.target.value }))}
                    />
                  </label>
                  <label className="field">
                    Container
                    <input
                      value={deriveForm.container}
                      onChange={(e) => setDeriveForm((f) => ({ ...f, container: e.target.value }))}
                      placeholder={member.container}
                    />
                  </label>
                  <button
                    type="submit"
                    disabled={busy || deriveForm.volume_ml === "" || deriveForm.count === ""}
                  >
                    Derive aliquot
                  </button>
                </form>

                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void perform(`Add test to ${member.specimen_key}`, () =>
                      addTestToSpecimen(client, member.specimen_key, addonCode.trim()),
                    );
                  }}
                >
                  <label className="field">
                    Add-on test code
                    <input value={addonCode} onChange={(e) => setAddonCode(e.target.value)} />
                  </label>
                  <button type="submit" disabled={busy || !addonCode.trim()}>
                    Add test
                  </button>
                </form>

                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void perform(`Scan ${member.specimen_key}`, () =>
                      scanSpecimen(client, {
                        identifier: member.specimen_key,
                        action: scanForm.action,
                        ...(scanForm.location.trim()
                          ? { location: scanForm.location.trim() }
                          : {}),
                      }),
                    );
                  }}
                >
                  <label className="field">
                    Custody action
                    <select
                      value={scanForm.action}
                      onChange={(e) => setScanForm((f) => ({ ...f, action: e.target.value }))}
                    >
                      {SCAN_ACTIONS.map((a) => (
                        <option key={a} value={a}>{a}</option>
                      ))}
                    </select>
                  </label>
                  <label className="field">
                    Scan location
                    <input
                      value={scanForm.location}
                      onChange={(e) => setScanForm((f) => ({ ...f, location: e.target.value }))}
                    />
                  </label>
                  <button type="submit" disabled={busy}>
                    Record scan event
                  </button>
                </form>

                <p>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() =>
                      void perform(`Print label for ${member.specimen_key}`, async () => {
                        const file = await printSpecimenLabel(
                          client,
                          member.derivation_type === "aliquot" ? "aliquot" : "specimen",
                          member.entity_ref,
                        );
                        try {
                          saveDownloadedFile(file);
                        } catch {
                          // Some environments have no URL.createObjectURL;
                          // the label was still generated and audit-logged
                          // server-side.
                        }
                        return file;
                      })
                    }
                  >
                    Print label
                  </button>
                </p>
              </>
            )}
          </div>
        </div>
      )}
    </>
  );
}
