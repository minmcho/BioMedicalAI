import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import * as rfidApi from "../api/rfid";
import type { DeviceItem, Role } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { MaintenanceTab } from "./MaintenanceTab";

interface Props {
  client: ApiClient;
  role: Role;
  /** Phase 15 deep-link contract: section === "rfid" renders the
   *  RFID registry panel; anything else keeps the default view. */
  section?: string;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

const MANAGE_ROLES: Role[] = ["admin", "configurator", "clinician"];

/** Devices (Phase 14, Track H): the device registry with computed
 *  calibration posture and gate block reasons, the calibration
 *  worklist (HTM queue), and the honest feed status — every feed in
 *  this deployment is simulated or recorded, and the page says so
 *  with the server's own label. A calibration-overdue device is
 *  flagged here and its results are blocked or quarantined at
 *  ingestion per the published device config. */
export function DevicesPage({ client, role, section }: Props) {
  const devices = useAsync(() => client.devices(), [client]);
  const worklist = useAsync(() => client.calibrationWorklist(), [client]);
  const feeds = useAsync(() => client.feedsStatus(), [client]);
  const readings = useAsync(() => client.deviceReadings(), [client]);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadKey, setReloadKey] = useState(0);
  // UX pass (spec P-7): registry / calibration / readings are tabs.
  const [tab, setTab] = useState("registry");

  const canManage = MANAGE_ROLES.includes(role);

  async function recordCalibration(device: DeviceItem) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const updated = await client.recordCalibration(
        device.device_key,
        new Date().toISOString(),
      );
      setNotice(
        `Calibration recorded for ${device.device_key}; next due ${updated.calibration_due ?? "—"} (interval from the published device config).`,
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setActionError(`Calibration failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  if (section === "rfid") {
    return (
      <section>
        <ModuleHeader title="Devices — RFID" purpose="RFID tag registry, asset find and reader dead letters." />
        <RfidPanel client={client} role={role} />
        <BarcodeLookupPanel client={client} />
      </section>
    );
  }

  if (section === "maintenance") {
    return (
      <section>
        <ModuleHeader title="Devices — Maintenance" purpose="Device maintenance work for the registered fleet." />
        <MaintenanceTab client={client} />
      </section>
    );
  }

  // UX pass (spec P-1): the loading/error states keep the page title —
  // a page never renders without its one <h2>.
  if (devices.loading || feeds.loading) {
    return (
      <section>
        <ModuleHeader title="Devices" purpose="Device registry, calibration posture and feed status." />
        <Loading label="Loading devices…" />
      </section>
    );
  }
  if (devices.error || feeds.error) {
    return (
      <section>
        <ModuleHeader title="Devices" purpose="Device registry, calibration posture and feed status." />
        <ErrorView error={devices.error ?? feeds.error} />
      </section>
    );
  }

  const registry = devices.data;
  const feed = feeds.data;
  const work = worklist.data;
  const recentReadings = readings.data ?? [];

  const registryPanel = (
    <>
      <h3>Feed status</h3>
      {feed && (
        <div className="cards">
          <div className="card">
            <div className="card-value">{feed.devices.total}</div>
            <div className="card-label">Registered devices ({feed.devices.overdue} calibration-overdue, {feed.devices.blocked} gate-blocked)</div>
          </div>
          <div className="card">
            <div className="card-value">{feed.mllp_gate.enabled ? "on" : "off"}</div>
            <div className="card-label">
              HL7/MLLP device gate — unknown device: {feed.mllp_gate.on_unknown_device ?? "—"};
              calibration overdue: {feed.mllp_gate.on_calibration_overdue ?? "—"}
            </div>
          </div>
          <div className="card">
            <div className="card-value">{feed.mqtt.state}</div>
            <div className="card-label">
              MQTT adapter{feed.mqtt.configured ? "" : " — no broker configured in integration/mqtt, so the adapter is honestly off"}
            </div>
          </div>
          <div className="card">
            <div className="card-value">{feed.lake_entries}</div>
            <div className="card-label">Raw-zone (lake) entries; {feed.quarantine_held} quarantine holds, {feed.dead_letters} dead letters</div>
          </div>
        </div>
      )}

      <h3>Registry</h3>
      {!registry || registry.items.length === 0 ? (
        <Empty label="No devices registered." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th>Device</th><th>Type</th><th>Location</th><th>Serial</th>
              <th>Status</th><th>Calibration</th><th>Gate</th><th />
            </tr>
          </thead>
          <tbody>
            {registry.items.map((d) => (
              <tr key={d.device_key}>
                <td>{d.name}<br /><span className="muted">{d.device_key}</span></td>
                <td>{d.device_type}</td>
                <td>{d.location || "—"}</td>
                <td>{d.serial || "—"}</td>
                <td>{d.status}</td>
                <td>
                  {d.calibration.status}
                  {d.calibration.due ? <><br /><span className="muted">due {d.calibration.due.slice(0, 10)}</span></> : null}
                </td>
                <td>{d.blocked_reason ? <span className="error">{d.blocked_reason}</span> : "clear"}</td>
                <td>
                  {canManage && (
                    <button type="button" disabled={busy} onClick={() => recordCalibration(d)}>
                      Record calibration
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );

  const calibrationPanel = (
    <>
      <h3>Calibration worklist</h3>
      {!work || work.items.length === 0 ? (
        <Empty label="No devices overdue or inside the calibration worklist window." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Device</th><th>Posture</th><th>Due</th><th>Days remaining</th></tr>
          </thead>
          <tbody>
            {work.items.map((d) => (
              <tr key={d.device_key}>
                <td>{d.device_key}</td>
                <td>{d.calibration.status}</td>
                <td>{d.calibration.due?.slice(0, 10) ?? "—"}</td>
                <td className="num">{d.calibration.days_remaining ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  );

  const readingsPanel = (
    <>
      <h3>Recent readings</h3>
      {recentReadings.length === 0 ? (
        <Empty label="No device readings recorded yet." />
      ) : (
        <table className="data-table">
          <thead>
            <tr><th>Device</th><th>Metric</th><th>Value</th><th>Source</th><th>Excursion</th><th>Received</th></tr>
          </thead>
          <tbody>
            {recentReadings.slice(0, 20).map((r) => (
              <tr key={r.id}>
                <td>{r.device_key}</td>
                <td>{r.metric}</td>
                <td className="num">{r.value} {r.unit}</td>
                <td>{r.source} ({r.source_label})</td>
                <td>{r.excursion ? <StatusBadge tone="bad">EXCURSION</StatusBadge> : "—"}</td>
                <td>{r.received_at?.slice(0, 19).replace("T", " ") ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <BarcodeLookupPanel client={client} />
    </>
  );

  return (
    <section key={reloadKey}>
      <ModuleHeader icon="device" group="Data Platform" title="Devices" purpose="Device registry, calibration posture and feed status." />
      <p className="muted">
        Device feeds in this deployment are <strong>{feed?.feed_label ?? "simulated"}</strong> —
        simulated or recorded inputs, labelled as such in the data. No live device link is claimed.
      </p>
      {notice && <p className="notice">{notice}</p>}
      {actionError && <p className="error">{actionError}</p>}
      <Tabs
        tabs={[
          { key: "registry", label: "Registry" },
          { key: "calibration", label: "Calibration worklist" },
          { key: "readings", label: "Readings & barcode" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Devices views"
        panels={{
          registry: registryPanel,
          calibration: calibrationPanel,
          readings: readingsPanel,
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Phase 15: barcode universal lookup panel (default view + RFID view)

export function BarcodeLookupPanel({ client }: { client: ApiClient }) {
  const [value, setValue] = useState("");
  const [result, setResult] = useState<rfidApi.BarcodeLookupResult | null>(null);
  const [lookupError, setLookupError] = useState<string | null>(null);
  const [busyLookup, setBusyLookup] = useState(false);

  async function doLookup() {
    setBusyLookup(true); setLookupError(null); setResult(null);
    try {
      setResult(await rfidApi.barcodeLookup(client, value.trim()));
    } catch (err) {
      setLookupError(errorText(err));
    } finally { setBusyLookup(false); }
  }

  const entity = result?.entity;
  const family = entity?.family_chain ?? entity?.specimen;
  return (
    <>
      <h3>Barcode lookup</h3>
      <p className="hint">Scan or type any registered barcode value — wristband, specimen, aliquot, blood, medication or asset — to resolve what it is, its family chain and its event history.</p>
      <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void doLookup(); }}>
        <label className="field">Barcode value
          <input aria-label="Barcode value" value={value} onChange={(e) => setValue(e.target.value)} placeholder="OCSP1|spec-1" />
        </label>
        <button type="submit" disabled={busyLookup || !value.trim()}>Look up barcode</button>
      </form>
      {lookupError && <div className="state state-error" role="alert">{lookupError}</div>}
      {result && (
        <div className="card">
          <p><strong>{result.kind}</strong> — {result.value} <StatusBadge tone={result.status === "active" ? "ok" : "bad"}>{result.status}</StatusBadge></p>
          <p className="muted">Bound to {result.entity_ref} · issued by {result.issued_by}</p>
          {entity?.patient && <p>Patient: {entity.patient.name ?? "—"} (MRN {entity.patient.mrn ?? "—"})</p>}
          {entity?.device && <p>Device: {entity.device.name} — {entity.device.location}</p>}
          {entity?.blood_unit && <p>Blood unit: {entity.blood_unit.unit_id} · group {entity.blood_unit.blood_group} · {entity.blood_unit.status}</p>}
          {entity?.dispense && <p>Dispense: {entity.dispense.medication_display ?? entity.dispense.dispense_id} · {entity.dispense.status ?? "—"}</p>}
          {entity && entity.resolved === false && <p className="error">Unresolved: {entity.reason ?? result.entity_ref}</p>}
          {family && (
            <div>
              <p>Specimen chain: accession {family.accession ?? family.specimen_id}</p>
              {(family.parent_refs ?? []).length > 0 && <p className="muted">Parents: {family.parent_refs!.join(", ")}</p>}
              {(family.family_barcodes ?? []).length > 0 && (
                <ul>{family.family_barcodes!.map((f, i) => <li key={i}>{f.kind ?? "barcode"} {f.value ?? ""} → {f.entity_ref ?? ""}{f.parent_value ? ` (parent ${f.parent_value})` : ""}</li>)}</ul>
              )}
            </div>
          )}
          <h4>Event history</h4>
          {result.history.length === 0 ? <Empty label="No events recorded for this barcode." /> : (
            <ul>{result.history.map((h, i) => <li key={i}>{h.event} — {h.actor} · {h.at?.slice(0, 19).replace("T", " ") ?? ""}</li>)}</ul>
          )}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Phase 15 (H-2): RFID registry panel — simulated feed, honestly labelled

export function RfidPanel({ client, role }: { client: ApiClient; role: Role }) {
  const [tick, setTick] = useState(0);
  const config = useAsync(() => rfidApi.rfidConfig(client), [client, tick]);
  const tags = useAsync(() => rfidApi.listRfidTags(client), [client, tick]);
  const dead = useAsync(() => rfidApi.listRfidDeadLetters(client), [client, tick]);
  const [bindType, setBindType] = useState<Record<string, string>>({});
  const [bindId, setBindId] = useState<Record<string, string>>({});
  const [panelError, setPanelError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState(false);
  const [findEpc, setFindEpc] = useState("");
  const [found, setFound] = useState<rfidApi.RfidTagDetail | null>(null);
  const [findError, setFindError] = useState<string | null>(null);

  const canManage = MANAGE_ROLES.includes(role);
  const feedLabel = config.data?.feed_label ?? tags.data?.feed_label ?? "simulated";

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusyAction(true); setPanelError(null); setNotice(null);
    try { await fn(); setNotice(`${label} — done.`); setTick((t) => t + 1); }
    catch (err) { setPanelError(`${label} failed: ${errorText(err)}`); }
    finally { setBusyAction(false); }
  }

  async function doFind() {
    setFindError(null); setFound(null);
    try { setFound(await rfidApi.getRfidTag(client, findEpc.trim())); }
    catch (err) { setFindError(errorText(err)); }
  }

  if (tags.loading) return <Loading label="Loading RFID tags…" />;
  if (tags.error) return <ErrorView error={tags.error} />;
  const lastSighting = found?.sightings?.[0];
  return (
    <>
      <p className="muted">
        RFID reader feed is <strong>{feedLabel}</strong> — simulated reader events posted
        through the API stand in for reader middleware. No live reader link is claimed
        (source_label: {feedLabel}).
      </p>
      {notice && <p className="notice">{notice}</p>}
      {panelError && <p className="error">{panelError}</p>}

      <h3>Tag registry</h3>
      {!tags.data || tags.data.items.length === 0 ? <Empty label="No RFID tags registered." /> : (
        <table className="data-table">
          <thead><tr><th>EPC</th><th>Type</th><th>Bound entity</th><th>Status</th><th>Last seen</th><th>Bind / actions</th></tr></thead>
          <tbody>{tags.data.items.map((t) => (
            <tr key={t.epc}>
              <td>{t.epc}</td><td>{t.tag_type}</td>
              <td>{t.entity_ref ?? "—"}</td>
              <td><StatusBadge tone={t.status === "bound" ? "ok" : t.status === "retired" ? "bad" : "neutral"}>{t.status}</StatusBadge></td>
              <td>{t.last_seen_zone ?? "—"}{t.last_seen_at ? <><br /><span className="muted">{t.last_seen_at.slice(0, 19).replace("T", " ")}</span></> : null}</td>
              <td>{canManage && t.status !== "retired" ? (
                <span className="appt-actions">
                  <input aria-label={`Entity type for tag ${t.epc}`} placeholder="Entity type (e.g. Device)" value={bindType[t.epc] ?? ""} onChange={(e) => setBindType((m) => ({ ...m, [t.epc]: e.target.value }))} />
                  <input aria-label={`Entity id for tag ${t.epc}`} placeholder="Entity id" value={bindId[t.epc] ?? ""} onChange={(e) => setBindId((m) => ({ ...m, [t.epc]: e.target.value }))} />
                  <button type="button" disabled={busyAction || !(bindType[t.epc] ?? "").trim() || !(bindId[t.epc] ?? "").trim()} onClick={() => void act(`Bind tag ${t.epc}`, () => rfidApi.bindRfidTag(client, t.epc, (bindType[t.epc] ?? "").trim(), (bindId[t.epc] ?? "").trim()))}>Bind</button>
                  {t.status === "bound" && <button type="button" disabled={busyAction} onClick={() => void act(`Unbind tag ${t.epc}`, () => rfidApi.unbindRfidTag(client, t.epc))}>Unbind</button>}
                  <button type="button" disabled={busyAction} onClick={() => void act(`Retire tag ${t.epc}`, () => rfidApi.retireRfidTag(client, t.epc))}>Retire</button>
                </span>
              ) : <span className="hint">{t.status === "retired" ? "Retired" : "Read-only"}</span>}</td>
            </tr>))}
          </tbody>
        </table>
      )}

      <h3>Asset find</h3>
      <form className="inline-form" onSubmit={(e) => { e.preventDefault(); void doFind(); }}>
        <label className="field">Tag EPC
          <input aria-label="Tag EPC to find" value={findEpc} onChange={(e) => setFindEpc(e.target.value)} placeholder="EPC-ASSET-001" />
        </label>
        <button type="submit" disabled={!findEpc.trim()}>Find tag</button>
      </form>
      {findError && <div className="state state-error" role="alert">{findError}</div>}
      {found && (
        <div className="card">
          <p><strong>{found.epc}</strong> — {found.tag_type} · <StatusBadge tone={found.status === "bound" ? "ok" : "neutral"}>{found.status}</StatusBadge></p>
          <p>Last seen zone: {found.last_seen_zone ?? "never seen"} · bound to {found.entity_ref ?? "—"}</p>
          {lastSighting ? (
            <p>Last sighting: zone {lastSighting.zone} via {lastSighting.reader_id} at {lastSighting.seen_at?.slice(0, 19).replace("T", " ")} (source: {lastSighting.source_label})</p>
          ) : <p className="muted">No sightings recorded for this tag.</p>}
          {found.sightings.length > 0 && (
            <ul>{found.sightings.map((s) => <li key={s.id}>{s.zone} — {s.reader_id} · {s.seen_at?.slice(0, 19).replace("T", " ")} ({s.source_label})</li>)}</ul>
          )}
        </div>
      )}

      <h3>Reader dead letters</h3>
      <p className="hint">Events from unknown/inactive readers or unregistered EPCs are dead-lettered with their disposition — never silently dropped. Feed: {feedLabel}.</p>
      {!dead.data || dead.data.items.length === 0 ? <Empty label="No dead-lettered reader events." /> : (
        <table className="data-table">
          <thead><tr><th>Reader</th><th>EPC</th><th>Disposition</th><th>Reason</th></tr></thead>
          <tbody>{dead.data.items.map((d) => (
            <tr key={d.id}><td>{d.reader_id}</td><td>{d.epc}</td><td><StatusBadge tone="bad">{d.disposition}</StatusBadge></td><td>{d.reason}</td></tr>))}
          </tbody>
        </table>
      )}
    </>
  );
}
