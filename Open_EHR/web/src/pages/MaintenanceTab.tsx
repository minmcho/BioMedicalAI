import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 16 (WS-I / K2) — maintenance / HTM work orders, mounted by the
// coordinator as a tab inside the Devices page, over the SAME Phase 14
// device registry (no second registry). The lifecycle is governed:
// opened → assigned → in-progress → completed → closed; illegal moves
// are refused by the server (409) and shown inline. A downtime order
// takes its device out of service (the published downtime status) and
// completing it restores the device. PM due dates come from the API
// (each open preventive order carries its pm_due_at).

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface WorkOrder {
  work_order_id: string;
  device_id: string;
  type: "corrective" | "preventive";
  priority: "stat" | "urgent" | "routine";
  status: string;
  assignee: string | null;
  downtime: boolean;
  reason: string;
  opened_by: string;
  opened_at: string | null;
  completed_at: string | null;
  completion_note: string | null;
  pm_due_at: string | null;
  overdue: boolean;
}

interface MaintConfig {
  downtime_status: string;
  work_types: string[];
  priorities: string[];
  statuses: string[];
}

interface DeviceItem {
  device_key: string;
  name: string;
  status: string;
  maintenance_due: string | null;
}

const TRANSITIONS: Record<string, string[]> = {
  opened: ["assigned", "in-progress"],
  assigned: ["in-progress"],
  "in-progress": ["completed"],
  completed: ["closed"],
  closed: [],
};

function orderTone(order: WorkOrder): StatusTone {
  if (order.overdue) return "bad";
  if (order.status === "completed" || order.status === "closed") return "ok";
  if (order.priority === "stat") return "bad";
  if (order.priority === "urgent") return "warn";
  return "info";
}

const dateOnly = (iso: string | null) => (iso ? iso.slice(0, 10) : "—");

export function MaintenanceTab({ client }: Props) {
  const worklist = useAsync<{ items: WorkOrder[]; total: number; overdue: number; downtime: number }>(
    () => client.request("/maintenance/worklist"),
    [client],
  );
  const orders = useAsync<{ items: WorkOrder[]; total: number }>(
    () => client.request("/maintenance/work-orders"),
    [client],
  );
  const config = useAsync<MaintConfig>(() => client.request("/maintenance/config"), [client]);
  const devices = useAsync<{ items: DeviceItem[] } | null>(
    () =>
      typeof client.devices === "function"
        ? (client.devices() as unknown as Promise<{ items: DeviceItem[] }>).catch(() => null)
        : Promise.resolve(null),
    [client],
  );
  const [form, setForm] = useState({
    device_id: "",
    type: "corrective",
    priority: "routine",
    reason: "",
    downtime: false,
  });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const downtimeStatus = config.data?.downtime_status ?? "out-of-service";
  const downDevices = (devices.data?.items ?? []).filter((d) => d.status === downtimeStatus);
  const pmDue = (worklist.data?.items ?? [])
    .filter((o) => o.type === "preventive" && o.pm_due_at)
    .sort((a, b) => String(a.pm_due_at).localeCompare(String(b.pm_due_at)));

  function reloadAll() {
    worklist.reload();
    orders.reload();
    devices.reload();
  }

  async function createOrder() {
    setBusy(true);
    setError(null);
    try {
      await client.request("/maintenance/work-orders", {
        method: "POST",
        body: JSON.stringify(form),
      });
      setForm({ device_id: "", type: "corrective", priority: "routine", reason: "", downtime: false });
      reloadAll();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function generatePm() {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const out = await client.request<{ created_count: number; checked: number; due: number; not_due: number }>(
        "/maintenance/pm/generate",
        { method: "POST" },
      );
      setNotice(
        `PM generation checked ${out.checked} device(s): ${out.due} due, ${out.created_count} new preventive order(s) created, ${out.not_due} not due yet.`,
      );
      reloadAll();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const deviceItems = devices.data?.items ?? [];

  return (
    <div>
      <h3>Maintenance — HTM work orders</h3>
      <p className="hint">
        Work orders run against the device registry above. Lifecycle: opened → assigned →
        in-progress → completed → closed. A downtime order takes the device{" "}
        <strong>{downtimeStatus}</strong> until the order completes.
      </p>
      {worklist.data && (
        <p role="status">
          Open orders: <strong>{worklist.data.total}</strong> · overdue PM:{" "}
          <strong>{worklist.data.overdue}</strong> · devices in downtime:{" "}
          <strong>{worklist.data.downtime}</strong>
        </p>
      )}
      {downDevices.length > 0 && (
        <div className="card">
          <h4>Devices currently down</h4>
          <ul className="result-list">
            {downDevices.map((d) => (
              <li key={d.device_key} className="result-row">
                <span className="result-type">{d.name}</span>
                <span className="result-meta">{d.device_key}</span>
                <StatusBadge tone="bad">down — {d.status}</StatusBadge>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div className="card">
        <h4>Preventive maintenance due</h4>
        <p>
          <button type="button" disabled={busy} onClick={() => void generatePm()}>
            Generate PM orders for due devices
          </button>
        </p>
        {notice && <p role="status">{notice}</p>}
        {pmDue.length === 0 ? (
          <Empty label="No open preventive orders with a due date. Generate PM orders to create them from the governed per-type intervals." />
        ) : (
          <ul className="result-list">
            {pmDue.map((o) => (
              <li key={o.work_order_id} className="result-row">
                <span className="result-type">{o.device_id}</span>
                <span className="result-meta">next PM due {dateOnly(o.pm_due_at)}</span>
                <StatusBadge tone={o.overdue ? "bad" : "info"}>
                  {o.overdue ? "PM overdue" : "PM scheduled"}
                </StatusBadge>
              </li>
            ))}
          </ul>
        )}
      </div>

      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void createOrder();
        }}
      >
        <h4>Open a work order</h4>
        <label>
          Device
          {deviceItems.length > 0 ? (
            <select
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
            >
              <option value="">Select a device…</option>
              {deviceItems.map((d) => (
                <option key={d.device_key} value={d.device_key}>
                  {d.name} ({d.device_key}){d.status === downtimeStatus ? " — currently down" : ""}
                </option>
              ))}
            </select>
          ) : (
            <input
              value={form.device_id}
              onChange={(e) => setForm({ ...form, device_id: e.target.value })}
              placeholder="device key from the registry"
            />
          )}
        </label>
        <label>
          Type
          <select value={form.type} onChange={(e) => setForm({ ...form, type: e.target.value })}>
            {(config.data?.work_types ?? ["corrective", "preventive"]).map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
        </label>
        <label>
          Priority
          <select value={form.priority} onChange={(e) => setForm({ ...form, priority: e.target.value })}>
            {(config.data?.priorities ?? ["stat", "urgent", "routine"]).map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </label>
        <label>
          Reason
          <input
            value={form.reason}
            onChange={(e) => setForm({ ...form, reason: e.target.value })}
            required
          />
        </label>
        <label>
          <input
            type="checkbox"
            checked={form.downtime}
            onChange={(e) => setForm({ ...form, downtime: e.target.checked })}
          />{" "}
          Takes the device out of service (downtime)
        </label>
        <button type="submit" disabled={busy || !form.device_id || !form.reason}>
          Open work order
        </button>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </form>

      <h4>All work orders</h4>
      {orders.loading && <Loading label="Loading work orders…" />}
      {orders.error != null && <ErrorView error={orders.error} />}
      {orders.data && orders.data.items.length === 0 && (
        <Empty label="No work orders yet. Open one above, or generate PM orders for due devices." />
      )}
      {orders.data?.items.map((order) => (
        <OrderRow key={order.work_order_id} client={client} order={order} onChanged={reloadAll} />
      ))}
    </div>
  );
}

function OrderRow({
  client,
  order,
  onChanged,
}: {
  client: ApiClient;
  order: WorkOrder;
  onChanged: () => void;
}) {
  const [assignee, setAssignee] = useState("");
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const nextStates = TRANSITIONS[order.status] ?? [];

  async function post(path: string, body: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    try {
      await client.request(path, { method: "POST", body: JSON.stringify(body) });
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p>
        <strong>{order.work_order_id}</strong> · {order.device_id}{" "}
        <StatusBadge tone={orderTone(order)}>{order.status}</StatusBadge>{" "}
        <StatusBadge tone="neutral">
          {order.type} · {order.priority}
        </StatusBadge>
        {order.downtime && <StatusBadge tone="bad">downtime</StatusBadge>}
        {order.overdue && <StatusBadge tone="bad">overdue</StatusBadge>}
      </p>
      <p className="hint">
        {order.reason} · opened by {order.opened_by}
        {order.assignee ? ` · assigned to ${order.assignee}` : " · unassigned"}
        {order.pm_due_at ? ` · PM due ${dateOnly(order.pm_due_at)}` : ""}
        {order.completed_at ? ` · completed ${dateOnly(order.completed_at)}` : ""}
        {order.completion_note ? ` · completion note: ${order.completion_note}` : ""}
      </p>
      {nextStates.length > 0 && (
        <div>
          {(order.status === "opened" || order.status === "assigned") && (
            <>
              <label>
                Assignee
                <input value={assignee} onChange={(e) => setAssignee(e.target.value)} />
              </label>
              <button
                type="button"
                disabled={busy || !assignee}
                onClick={() =>
                  void post(`/maintenance/work-orders/${order.work_order_id}/assign`, { assignee })
                }
              >
                Assign
              </button>{" "}
            </>
          )}
          {nextStates.includes("completed") && (
            <label>
              Completion note
              <input value={note} onChange={(e) => setNote(e.target.value)} />
            </label>
          )}
          {nextStates.map((to) => (
            <button
              key={to}
              type="button"
              disabled={busy}
              onClick={() =>
                void post(`/maintenance/work-orders/${order.work_order_id}/transition`, {
                  to_status: to,
                  ...(to === "assigned" ? { assignee } : {}),
                  ...(to === "completed" ? { completion_note: note } : {}),
                })
              }
            >
              Move to {to}
            </button>
          ))}
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
