import { useState } from "react";
import {
  ApiError,
  type ApiClient,
  type InvContainer,
  type InvCount,
  type InvItem,
  type InvStockRow,
  type InvTaxonomy,
} from "../api/client";
import { getOrgTree, type OrgTree } from "../api/org";
import type { Role } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge } from "../components/StatusBadge";
import { Stepper } from "../components/Stepper";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

// Inventory & Materials (WS-C web): item master, lot-level stock by
// placement location, the append-only movement ledger, receiving /
// issue / transfer / adjust steppers, requisitions, cycle counts,
// case draw and the low-stock / expiry / implant-trace worklists —
// all over the /inventory contract (WS-A). Reads ride inventory.read
// (staff incl. viewer); writes follow inventory.manage (item master,
// PAR, counts) and inventory.move (movements, requisitions, case
// draw), which the backend grants to admin / configurator /
// clinician — the viewer is read-only here and the server enforces
// the same gates again. Locations come from GET /org-hierarchy
// (facility / department / unit nodes flattened); categories, reason
// codes and the expiry window come from GET /inventory/taxonomy —
// codes are never free text.

const WRITE_ROLES: Role[] = ["admin", "configurator", "clinician"];

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

function money(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "—";
  return `$${(cents / 100).toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  })}`;
}

/** Dollars input -> integer cents; empty -> null; junk -> NaN. */
function dollarsToCents(text: string): number | null {
  const t = text.trim();
  if (!t) return null;
  const n = Number(t);
  if (!Number.isFinite(n)) return NaN;
  return Math.round(n * 100);
}

function centsToDollars(cents: number | null | undefined): string {
  if (cents === null || cents === undefined) return "";
  return (cents / 100).toFixed(2);
}

export const LOT_STAGE_TRANSITIONS: Record<string, string[]> = {
  received: ["quarantine", "released"],
  quarantine: ["released", "recalled", "disposed"],
  released: ["in_use", "quarantine", "recalled", "disposed"],
  in_use: ["quarantine", "recalled", "disposed"],
  recalled: [],
  disposed: [],
};

export function stageReasonOptions(
  taxonomy: InvTaxonomy | null,
  toStage: string,
): Array<{ code: string; label: string }> {
  if (!taxonomy) return [];
  if (toStage === "quarantine") return taxonomy.quarantineReasonCodes ?? [];
  if (toStage === "recalled") return taxonomy.recallReasonCodes ?? [];
  if (toStage === "disposed") return taxonomy.disposalReasonCodes ?? [];
  return [];
}

export function resolveClassPolicy(
  taxonomy: InvTaxonomy | null,
  item: InvItem | null | undefined,
): Record<string, unknown> {
  if (!taxonomy || !item) return {};
  const cat = taxonomy.categories.find((c) => c.code === item.category);
  if (!cat) return {};
  const out: Record<string, unknown> = { ...(cat.classPolicy ?? {}) };
  const sub = cat.subcategories.find((s) => s.code === item.subcategory);
  if (sub?.classPolicy) Object.assign(out, sub.classPolicy);
  return out;
}

export function ladderCodes(item: InvItem | null | undefined): string[] {
  if (!item) return [];
  const codes = (item.uom_ladder ?? []).map((l) => l.code).filter(Boolean);
  return Array.from(new Set(codes));
}

export function itemBaseUom(item: InvItem | null | undefined): string {
  if (!item) return "";
  return item.base_uom || item.uom || "";
}

export function ladderSummary(item: InvItem): string {
  const codes = ladderCodes(item);
  if (codes.length > 0) return codes.join(" → ");
  return itemBaseUom(item);
}

function UnitLevelSelect({
  label,
  value,
  onChange,
  item,
}: {
  label: string;
  value: string;
  onChange: (code: string) => void;
  item: InvItem | null;
}) {
  const codes = ladderCodes(item);
  const disabled = !item || codes.length === 0;
  return (
    <label className="field">
      {label}
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">
          Base units{item ? ` (${itemBaseUom(item)})` : ""}
        </option>
        {codes.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      {disabled && (
        <span className="field-error">
          {!item
            ? "Choose an item first — its unit ladder sets the levels."
            : "This item has no unit ladder — quantities are entered in base units."}
        </span>
      )}
    </label>
  );
}

export interface StockLocation {
  code: string;
  label: string;
}

/** Flatten the org tree to stock locations: every facility
 *  (location), department and unit node is a place stock can sit,
 *  labelled with its ancestor path. Regions are not stock
 *  locations. Deduped by code (first path wins). */
export function flattenStockLocations(tree: OrgTree | null): StockLocation[] {
  if (!tree) return [];
  const out: StockLocation[] = [];
  const seen = new Set<string>();
  const push = (code: string, label: string) => {
    if (!code || seen.has(code)) return;
    seen.add(code);
    out.push({ code, label });
  };
  for (const region of tree.regions) {
    for (const loc of region.locations) {
      push(loc.code, loc.display ?? loc.code);
      for (const dept of loc.departments) {
        push(dept.code, `${loc.display ?? loc.code} — ${dept.display ?? dept.code}`);
        for (const unit of dept.units) {
          push(
            unit.code,
            `${loc.display ?? loc.code} — ${dept.display ?? dept.code} — ${unit.display ?? unit.code}`,
          );
        }
      }
    }
  }
  return out;
}

function LocationSelect({
  label,
  value,
  onChange,
  locations,
  locationsReady,
  allowAll = false,
}: {
  label: string;
  value: string;
  onChange: (code: string) => void;
  locations: StockLocation[];
  locationsReady: boolean;
  allowAll?: boolean;
}) {
  const disabled = !locationsReady || locations.length === 0;
  return (
    <label className="field">
      {label}
      <select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{allowAll ? "All locations" : "Select a location"}</option>
        {locations.map((l) => (
          <option key={l.code} value={l.code}>
            {l.label} ({l.code})
          </option>
        ))}
      </select>
      {disabled && (
        <span className="field-error">
          {!locationsReady
            ? "Loading locations from the organisation hierarchy…"
            : "No locations in the published organisation hierarchy."}
        </span>
      )}
    </label>
  );
}

function ItemSelect({
  label,
  value,
  onChange,
  items,
}: {
  label: string;
  value: string;
  onChange: (sku: string) => void;
  items: InvItem[];
}) {
  return (
    <label className="field">
      {label}
      <select
        aria-label={label}
        value={value}
        disabled={items.length === 0}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Select an item</option>
        {items.map((i) => (
          <option key={i.sku} value={i.sku}>
            {i.sku} — {i.name}
          </option>
        ))}
      </select>
      {items.length === 0 && (
        <span className="field-error">No items in the item master yet.</span>
      )}
    </label>
  );
}

interface Ctx {
  client: ApiClient;
  taxonomy: InvTaxonomy | null;
  items: InvItem[];
  locations: StockLocation[];
  locationsReady: boolean;
  canManage: boolean;
  canMove: boolean;
  tick: number;
  bump: () => void;
}

function nameFor(ctx: Ctx, sku: string): string {
  return ctx.items.find((i) => i.sku === sku)?.name ?? "";
}

function categoryLabel(ctx: Ctx, code: string): string {
  return ctx.taxonomy?.categories.find((c) => c.code === code)?.label ?? code;
}

export function InventoryPage({ client, role }: Props) {
  const canManage = WRITE_ROLES.includes(role);
  const canMove = WRITE_ROLES.includes(role);
  const [tab, setTab] = useState("items");
  const [tick, setTick] = useState(0);
  const bump = () => setTick((t) => t + 1);

  const taxonomyState = useAsync(() => client.inventoryTaxonomy(), [client]);
  const overviewState = useAsync(() => client.inventoryOverview(), [client, tick]);
  const orgState = useAsync(() => getOrgTree(client), [client]);
  const catalogueState = useAsync(() => client.inventoryItems({}), [client, tick]);

  const ctx: Ctx = {
    client,
    taxonomy: taxonomyState.data,
    items: catalogueState.data ?? [],
    locations: flattenStockLocations(orgState.data),
    locationsReady: !orgState.loading,
    canManage,
    canMove,
    tick,
    bump,
  };

  const overview = overviewState.data;

  return (
    <section>
      <ModuleHeader
        icon="boxes"
        group="Safety & Support"
        title="Inventory & Materials"
        purpose="Stock, supplies, implants and materials across locations"
        primaryAction={{
          label: "Receive stock",
          onClick: () => setTab("receiving"),
          disabled: !canMove,
          disabledReason: canMove
            ? undefined
            : "Receiving needs the inventory.move capability — your role is read-only.",
        }}
      />
      {!canMove && (
        <p className="hint">
          You are viewing inventory read-only: your role does not hold
          inventory.manage / inventory.move, so forms and actions are
          hidden or disabled. Stock, movements and worklists remain
          fully visible.
        </p>
      )}

      {overviewState.loading ? (
        <Loading label="Loading inventory overview…" />
      ) : overviewState.error ? (
        <ErrorView error={overviewState.error} />
      ) : overview ? (
        <div className="cards" aria-label="Inventory overview">
          <div className="card">
            <div className="card-value">{overview.active_items}</div>
            <div className="card-label">Active items</div>
          </div>
          <div className="card">
            <div className="card-value">{overview.stock_locations}</div>
            <div className="card-label">Stock locations</div>
          </div>
          <div className="card">
            <div className="card-value">{overview.on_hand_units}</div>
            <div className="card-label">Units on hand</div>
          </div>
          <div className="card">
            <div className="card-value">{overview.low_stock_lines}</div>
            <div className="card-label">
              Low-stock lines{" "}
              {overview.low_stock_lines > 0 && (
                <StatusBadge tone="warn">needs reorder</StatusBadge>
              )}
            </div>
          </div>
          <div className="card">
            <div className="card-value">{overview.expiring_lines}</div>
            <div className="card-label">
              Expiring lines{" "}
              {overview.expiring_lines > 0 && (
                <StatusBadge tone="warn">expiring soon</StatusBadge>
              )}
            </div>
          </div>
          <div className="card">
            <div className="card-value">{overview.expired_lines}</div>
            <div className="card-label">
              Expired lines{" "}
              {overview.expired_lines > 0 && (
                <StatusBadge tone="bad">expired</StatusBadge>
              )}
            </div>
          </div>
          <div className="card">
            <div className="card-value">{overview.movements_today}</div>
            <div className="card-label">Movements today</div>
          </div>
          <div className="card">
            <div className="card-value">{money(overview.stock_value_cents)}</div>
            <div className="card-label">
              Stock value{overview.uncosted_lots > 0
                ? ` (${overview.uncosted_lots} uncosted lot${overview.uncosted_lots === 1 ? "" : "s"} excluded)`
                : ""}
            </div>
          </div>
        </div>
      ) : null}

      {taxonomyState.error ? (
        <ErrorView error={taxonomyState.error} />
      ) : null}

      <Tabs
        tabs={[
          { key: "items", label: "Items" },
          { key: "stock", label: "Stock" },
          { key: "movements", label: "Movements" },
          { key: "receiving", label: "Receiving & Issue" },
          { key: "counts", label: "Counts" },
          { key: "worklists", label: "Worklists" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Inventory sections"
        panels={{
          items: <ItemsPanel ctx={ctx} />,
          stock: <StockPanel ctx={ctx} />,
          movements: <MovementsPanel ctx={ctx} />,
          receiving: <ReceivingPanel ctx={ctx} />,
          counts: <CountsPanel ctx={ctx} />,
          worklists: <WorklistsPanel ctx={ctx} />,
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Items tab: filtered item-master table + create/edit form.
// ---------------------------------------------------------------------------

function ItemsPanel({ ctx }: { ctx: Ctx }) {
  const { client, taxonomy, canManage } = ctx;
  const [category, setCategory] = useState("");
  const [subcategory, setSubcategory] = useState("");
  const [activeOnly, setActiveOnly] = useState(false);
  const [q, setQ] = useState("");
  const [editingSku, setEditingSku] = useState<string | null>(null);
  const [form, setForm] = useState({
    sku: "",
    name: "",
    category: "",
    subcategory: "",
    uom: "",
    structure: "simple",
    base_uom: "",
    kit_mode: "track-whole",
    hazard_class: "",
    sds_ref: "",
    lot_tracked: false,
    serial_tracked: false,
    expiry_tracked: false,
    costDollars: "",
    formulary_code: "",
    asset_key: "",
    active: true,
  });
  const [ladder, setLadder] = useState<
    Array<{ code: string; containsNext: string; barcode: string }>
  >([]);
  const [components, setComponents] = useState<
    Array<{ sku: string; qty: string }>
  >([]);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  const list = useAsync(
    () =>
      client.inventoryItems({
        category: category || undefined,
        subcategory: subcategory || undefined,
        active: activeOnly ? true : undefined,
        q: q.trim() || undefined,
      }),
    [client, ctx.tick, category, subcategory, activeOnly, q],
  );

  const filterSubcategories =
    taxonomy?.categories.find((c) => c.code === category)?.subcategories ?? [];
  const formSubcategories =
    taxonomy?.categories.find((c) => c.code === form.category)?.subcategories ?? [];

  function startEdit(item: InvItem) {
    setEditingSku(item.sku);
    setForm({
      sku: item.sku,
      name: item.name,
      category: item.category,
      subcategory: item.subcategory,
      uom: item.uom,
      structure: item.structure ?? "simple",
      base_uom: item.base_uom ?? item.uom ?? "",
      kit_mode: item.kit_mode ?? "track-whole",
      hazard_class: item.hazard_class ?? "",
      sds_ref: item.sds_ref ?? "",
      lot_tracked: item.lot_tracked,
      serial_tracked: item.serial_tracked,
      expiry_tracked: item.expiry_tracked,
      costDollars: centsToDollars(item.unit_cost_cents),
      formulary_code: item.formulary_code ?? "",
      asset_key: item.asset_key ?? "",
      active: item.active,
    });
    setLadder(
      (item.uom_ladder ?? []).map((l) => ({
        code: l.code,
        containsNext: String(l.containsNext ?? 1),
        barcode: l.barcode ?? "",
      })),
    );
    setComponents(
      (item.kit_components ?? []).map((c) => ({
        sku: c.sku,
        qty: String(c.qty),
      })),
    );
    setErrors({});
    setFormError(null);
  }

  function resetForm() {
    setEditingSku(null);
    setForm({
      sku: "", name: "", category: "", subcategory: "", uom: "",
      structure: "simple", base_uom: "", kit_mode: "track-whole",
      hazard_class: "", sds_ref: "",
      lot_tracked: false, serial_tracked: false, expiry_tracked: false,
      costDollars: "", formulary_code: "", asset_key: "", active: true,
    });
    setLadder([]);
    setComponents([]);
    setErrors({});
  }

  async function submit() {
    const errs: Record<string, string> = {};
    if (!form.sku.trim()) errs.sku = "SKU is required.";
    if (!form.name.trim()) errs.name = "Name is required.";
    if (!form.category) errs.category = "Choose a category from the taxonomy.";
    if (!form.subcategory) errs.subcategory = "Choose a subcategory from the taxonomy.";
    if (!form.uom.trim()) errs.uom = "Unit of measure is required (e.g. each, box).";
    const cents = dollarsToCents(form.costDollars);
    if (Number.isNaN(cents)) errs.cost = "Unit cost must be a dollar amount, e.g. 12.50.";
    const baseUom = (form.base_uom.trim() || form.uom.trim());
    // Ladder validation (mirrors the server rules).
    let parsedLadder: Array<{ code: string; containsNext: number; barcode?: string }> | undefined;
    if (ladder.length > 0) {
      const seen = new Set<string>();
      const ladderErrs: string[] = [];
      ladder.forEach((lvl, idx) => {
        const code = lvl.code.trim();
        if (!code) ladderErrs.push(`Level ${idx + 1}: code is required.`);
        if (code && seen.has(code)) ladderErrs.push(`Duplicate ladder level code “${code}”.`);
        seen.add(code);
        const contains = Number(lvl.containsNext);
        const isLast = idx === ladder.length - 1;
        if (!Number.isInteger(contains)) {
          ladderErrs.push(`Level ${code || idx + 1}: contains-next must be a whole number.`);
        } else if (isLast && contains !== 1) {
          ladderErrs.push("The last ladder level must have contains-next = 1.");
        } else if (!isLast && contains < 2) {
          ladderErrs.push(`Level ${code || idx + 1}: contains-next must be at least 2 (only the last level is 1).`);
        }
      });
      const lastCode = ladder[ladder.length - 1]?.code.trim();
      if (lastCode && baseUom && lastCode !== baseUom) {
        ladderErrs.push(`The last ladder level code (“${lastCode}”) must equal the base unit of measure (“${baseUom}”).`);
      }
      if (ladderErrs.length > 0) errs.ladder = ladderErrs.join(" ");
      else {
        parsedLadder = ladder.map((l) => ({
          code: l.code.trim(),
          containsNext: Number(l.containsNext),
          ...(l.barcode.trim() ? { barcode: l.barcode.trim() } : {}),
        }));
      }
    }
    // Kit validation (mirrors the server rules).
    let parsedComponents: Array<{ sku: string; qty: number }> | undefined;
    if (form.structure === "kit") {
      const kitErrs: string[] = [];
      if (!form.kit_mode) kitErrs.push("Choose a kit mode.");
      if (components.length === 0) kitErrs.push("A kit needs at least one component.");
      const seenComp = new Set<string>();
      components.forEach((c, idx) => {
        if (!c.sku) kitErrs.push(`Component ${idx + 1}: choose a component item.`);
        if (c.sku && c.sku === form.sku.trim()) kitErrs.push("A kit cannot contain itself — choose a different component.");
        if (c.sku && seenComp.has(c.sku)) kitErrs.push(`Duplicate component “${c.sku}”.`);
        seenComp.add(c.sku);
        const q = Number(c.qty);
        if (!Number.isInteger(q) || q < 1) kitErrs.push(`Component ${c.sku || idx + 1}: quantity must be at least 1.`);
      });
      if (kitErrs.length > 0) errs.kit = kitErrs.join(" ");
      else parsedComponents = components.map((c) => ({ sku: c.sku, qty: Number(c.qty) }));
    }
    setErrors(errs);
    if (Object.keys(errs).length > 0) return;
    setBusy(true);
    setFormError(null);
    setNotice(null);
    const payload = {
      sku: form.sku.trim(),
      name: form.name.trim(),
      category: form.category,
      subcategory: form.subcategory,
      uom: form.uom.trim(),
      structure: form.structure,
      base_uom: baseUom || null,
      ...(parsedLadder ? { uom_ladder: parsedLadder } : {}),
      ...(form.structure === "kit"
        ? { kit_components: parsedComponents ?? [], kit_mode: form.kit_mode }
        : { kit_components: [], kit_mode: null }),
      hazard_class: form.hazard_class.trim() || null,
      sds_ref: form.sds_ref.trim() || null,
      lot_tracked: form.lot_tracked,
      serial_tracked: form.serial_tracked,
      expiry_tracked: form.expiry_tracked,
      unit_cost_cents: cents,
      formulary_code: form.formulary_code.trim() || null,
      asset_key: form.asset_key.trim() || null,
      active: form.active,
    };
    try {
      if (editingSku) {
        await client.updateInventoryItem(editingSku, payload);
        setNotice(`Item ${editingSku} updated.`);
      } else {
        await client.createInventoryItem(payload);
        setNotice(`Item ${payload.sku} created.`);
      }
      resetForm();
      ctx.bump();
    } catch (err) {
      setFormError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Item master</h3>
      <div className="inline-form" aria-label="Item filters">
        <label className="field">
          Category
          <select
            aria-label="Filter by category"
            value={category}
            disabled={!taxonomy}
            onChange={(e) => {
              setCategory(e.target.value);
              setSubcategory("");
            }}
          >
            <option value="">All categories</option>
            {(taxonomy?.categories ?? []).map((c) => (
              <option key={c.code} value={c.code}>{c.label}</option>
            ))}
          </select>
          {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
        </label>
        <label className="field">
          Subcategory
          <select
            aria-label="Filter by subcategory"
            value={subcategory}
            disabled={!category}
            onChange={(e) => setSubcategory(e.target.value)}
          >
            <option value="">All subcategories</option>
            {filterSubcategories.map((s) => (
              <option key={s.code} value={s.code}>{s.label}</option>
            ))}
          </select>
          {!category && <span className="field-error">Choose a category first</span>}
        </label>
        <label className="field">
          Search
          <input
            aria-label="Search items"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="SKU or name"
          />
        </label>
        <label className="field">
          Active only
          <input
            type="checkbox"
            aria-label="Active only"
            checked={activeOnly}
            onChange={(e) => setActiveOnly(e.target.checked)}
          />
        </label>
      </div>

      {list.loading ? (
        <Loading label="Loading items…" />
      ) : list.error ? (
        <ErrorView error={list.error} />
      ) : !list.data || list.data.length === 0 ? (
        <Empty label="No items match the current filters." icon="boxes" />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">SKU</th><th scope="col">Name</th>
              <th scope="col">Category</th><th scope="col">Subcategory</th>
              <th scope="col">UoM</th><th scope="col">Structure</th>
              <th scope="col">Tracking</th>
              <th scope="col">Unit cost</th><th scope="col">Status</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((item) => (
              <tr key={item.sku}>
                <td>{item.sku}</td>
                <td>{item.name}</td>
                <td>{categoryLabel(ctx, item.category)}</td>
                <td>{item.subcategory}</td>
                <td>{item.uom}</td>
                <td>
                  <StatusBadge tone={(item.structure ?? "simple") === "kit" ? "warn" : "neutral"}>
                    {item.structure ?? "simple"}
                  </StatusBadge>
                  <br />
                  <span className="muted">{ladderSummary(item)}</span>
                  {(item.structure ?? "simple") === "kit" && (
                    <>
                      <br />
                      <span className="muted">
                        {item.kit_mode ?? "—"} · {(item.kit_components ?? []).length} component{(item.kit_components ?? []).length === 1 ? "" : "s"}
                      </span>
                    </>
                  )}
                  {item.hazard_class && (
                    <>
                      <br />
                      <span className="muted">Hazard: {item.hazard_class}</span>
                    </>
                  )}
                </td>
                <td>
                  {[item.lot_tracked && "Lot", item.serial_tracked && "Serial", item.expiry_tracked && "Expiry"]
                    .filter(Boolean)
                    .join(", ") || "None"}
                </td>
                <td>{money(item.unit_cost_cents)}</td>
                <td>
                  {item.active ? (
                    <StatusBadge tone="ok">active</StatusBadge>
                  ) : (
                    <StatusBadge tone="neutral">inactive</StatusBadge>
                  )}
                </td>
                <td>
                  {canManage ? (
                    <button type="button" onClick={() => startEdit(item)}>
                      Edit {item.sku}
                    </button>
                  ) : (
                    <span className="hint">Read-only</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {notice && <p className="state state-empty" role="status">{notice}</p>}

      {canManage ? (
        <form
          className="inline-form"
          aria-label={editingSku ? "Edit item" : "Create item"}
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <h4>{editingSku ? `Edit item ${editingSku}` : "Create item"}</h4>
          <label className="field">
            SKU *
            <input
              aria-label="Item SKU"
              value={form.sku}
              disabled={editingSku !== null}
              onChange={(e) => patch({ sku: e.target.value })}
              placeholder="e.g. SUT-001"
            />
            {errors.sku && <span className="field-error" role="alert">{errors.sku}</span>}
          </label>
          <label className="field">
            Name *
            <input
              aria-label="Item name"
              value={form.name}
              onChange={(e) => patch({ name: e.target.value })}
            />
            {errors.name && <span className="field-error" role="alert">{errors.name}</span>}
          </label>
          <label className="field">
            Category *
            <select
              aria-label="Item category"
              value={form.category}
              disabled={!taxonomy}
              onChange={(e) => patch({ category: e.target.value, subcategory: "" })}
            >
              <option value="">Select a category</option>
              {(taxonomy?.categories ?? []).map((c) => (
                <option key={c.code} value={c.code}>{c.label}</option>
              ))}
            </select>
            {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
            {errors.category && <span className="field-error" role="alert">{errors.category}</span>}
          </label>
          <label className="field">
            Subcategory *
            <select
              aria-label="Item subcategory"
              value={form.subcategory}
              disabled={!form.category}
              onChange={(e) => patch({ subcategory: e.target.value })}
            >
              <option value="">Select a subcategory</option>
              {formSubcategories.map((s) => (
                <option key={s.code} value={s.code}>{s.label}</option>
              ))}
            </select>
            {!form.category && <span className="field-error">Choose a category first</span>}
            {errors.subcategory && <span className="field-error" role="alert">{errors.subcategory}</span>}
          </label>
          <label className="field">
            Unit of measure *
            <input
              aria-label="Item unit of measure"
              value={form.uom}
              onChange={(e) => patch({ uom: e.target.value })}
              placeholder="each"
            />
            {errors.uom && <span className="field-error" role="alert">{errors.uom}</span>}
          </label>
          <label className="field">
            Structure *
            <select
              aria-label="Item structure"
              value={form.structure}
              onChange={(e) => patch({ structure: e.target.value })}
            >
              <option value="simple">Simple</option>
              <option value="kit">Kit</option>
            </select>
          </label>
          <label className="field">
            Base unit of measure
            <input
              aria-label="Item base unit of measure"
              value={form.base_uom}
              onChange={(e) => patch({ base_uom: e.target.value })}
              placeholder="Defaults to the unit of measure"
            />
          </label>
          <label className="field">
            Hazard class
            <input
              aria-label="Item hazard class"
              value={form.hazard_class}
              onChange={(e) => patch({ hazard_class: e.target.value })}
            />
          </label>
          <label className="field">
            SDS reference
            <input
              aria-label="Item SDS reference"
              value={form.sds_ref}
              onChange={(e) => patch({ sds_ref: e.target.value })}
            />
          </label>
          <div aria-label="Unit ladder editor">
            <h5>Unit ladder</h5>
            <p className="hint">Largest level first; the last level is the base unit and its contains-next is 1.</p>
            {ladder.map((lvl, idx) => (
              <span key={idx} className="inline-form">
                <label className="field">
                  Ladder level {idx + 1} code
                  <input
                    aria-label={`Ladder level ${idx + 1} code`}
                    value={lvl.code}
                    onChange={(e) => setLadder((ls) => ls.map((l, j) => (j === idx ? { ...l, code: e.target.value } : l)))}
                  />
                </label>
                <label className="field">
                  Contains next
                  <input
                    aria-label={`Ladder level ${idx + 1} contains next`}
                    type="number"
                    min="1"
                    value={lvl.containsNext}
                    onChange={(e) => setLadder((ls) => ls.map((l, j) => (j === idx ? { ...l, containsNext: e.target.value } : l)))}
                  />
                </label>
                <label className="field">
                  Barcode
                  <input
                    aria-label={`Ladder level ${idx + 1} barcode`}
                    value={lvl.barcode}
                    onChange={(e) => setLadder((ls) => ls.map((l, j) => (j === idx ? { ...l, barcode: e.target.value } : l)))}
                  />
                </label>
                <button type="button" className="linklike" onClick={() => setLadder((ls) => ls.filter((_, j) => j !== idx))}>
                  Remove ladder level {idx + 1}
                </button>
              </span>
            ))}
            <button
              type="button"
              className="linklike"
              onClick={() => setLadder((ls) => [...ls, { code: "", containsNext: ls.length === 0 ? "2" : "1", barcode: "" }])}
            >
              Add ladder level
            </button>
            {errors.ladder && <span className="field-error" role="alert">{errors.ladder}</span>}
          </div>
          {form.structure === "kit" && (
            <div aria-label="Kit editor">
              <h5>Kit definition</h5>
              <label className="field">
                Kit mode *
                <select
                  aria-label="Kit mode"
                  value={form.kit_mode}
                  onChange={(e) => patch({ kit_mode: e.target.value })}
                >
                  <option value="track-whole">Track whole</option>
                  <option value="break-on-receipt">Break on receipt</option>
                </select>
              </label>
              {components.map((comp, idx) => (
                <span key={idx} className="inline-form">
                  <ItemSelect
                    label={`Kit component ${idx + 1}`}
                    value={comp.sku}
                    onChange={(v) => setComponents((cs) => cs.map((c, j) => (j === idx ? { ...c, sku: v } : c)))}
                    items={ctx.items.filter((i) => (i.structure ?? "simple") === "simple")}
                  />
                  <label className="field">
                    Component quantity
                    <input
                      aria-label={`Kit component ${idx + 1} quantity`}
                      type="number"
                      min="1"
                      value={comp.qty}
                      onChange={(e) => setComponents((cs) => cs.map((c, j) => (j === idx ? { ...c, qty: e.target.value } : c)))}
                    />
                  </label>
                  <button type="button" className="linklike" onClick={() => setComponents((cs) => cs.filter((_, j) => j !== idx))}>
                    Remove component {idx + 1}
                  </button>
                </span>
              ))}
              <button
                type="button"
                className="linklike"
                disabled={ctx.items.length === 0}
                onClick={() => setComponents((cs) => [...cs, { sku: "", qty: "1" }])}
              >
                Add component
              </button>
              {ctx.items.length === 0 && (
                <span className="field-error">No items in the item master yet — create a component item first.</span>
              )}
              {errors.kit && <span className="field-error" role="alert">{errors.kit}</span>}
            </div>
          )}
          <label className="field">
            Lot tracked
            <input
              type="checkbox"
              aria-label="Lot tracked"
              checked={form.lot_tracked}
              onChange={(e) => patch({ lot_tracked: e.target.checked })}
            />
          </label>
          <label className="field">
            Serial tracked
            <input
              type="checkbox"
              aria-label="Serial tracked"
              checked={form.serial_tracked}
              onChange={(e) => patch({ serial_tracked: e.target.checked })}
            />
          </label>
          <label className="field">
            Expiry tracked
            <input
              type="checkbox"
              aria-label="Expiry tracked"
              checked={form.expiry_tracked}
              onChange={(e) => patch({ expiry_tracked: e.target.checked })}
            />
          </label>
          <label className="field">
            Unit cost ($)
            <input
              aria-label="Item unit cost in dollars"
              value={form.costDollars}
              onChange={(e) => patch({ costDollars: e.target.value })}
              placeholder="0.00"
              inputMode="decimal"
            />
            {errors.cost && <span className="field-error" role="alert">{errors.cost}</span>}
          </label>
          <label className="field">
            Formulary code
            <input
              aria-label="Item formulary code"
              value={form.formulary_code}
              onChange={(e) => patch({ formulary_code: e.target.value })}
            />
          </label>
          <label className="field">
            Asset key
            <input
              aria-label="Item asset key"
              value={form.asset_key}
              onChange={(e) => patch({ asset_key: e.target.value })}
            />
          </label>
          <label className="field">
            Active
            <input
              type="checkbox"
              aria-label="Item active"
              checked={form.active}
              onChange={(e) => patch({ active: e.target.checked })}
            />
          </label>
          <button type="submit" disabled={busy}>
            {editingSku ? "Save item" : "Create item"}
          </button>
          {editingSku && (
            <button type="button" className="linklike" onClick={resetForm}>
              Cancel edit
            </button>
          )}
          {formError && (
            <p className="error-note" role="alert">{formError}</p>
          )}
        </form>
      ) : (
        <p className="hint">
          Item creation and editing need the inventory.manage capability —
          your role is read-only.
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stock tab: lot rows + PAR editor.
// ---------------------------------------------------------------------------

function LotActions({ ctx, row }: { ctx: Ctx; row: InvStockRow }) {
  const { client } = ctx;
  const lotId = row.lot_number ?? "no-lot";
  const [showVerify, setShowVerify] = useState(false);
  const [showStage, setShowStage] = useState(false);
  const [method, setMethod] = useState("");
  const [result, setResult] = useState("pass");
  const [note, setNote] = useState("");
  const [toStage, setToStage] = useState("");
  const [reason, setReason] = useState("");
  const [stageNote, setStageNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const stage = row.stage ?? "released";
  const transitions = LOT_STAGE_TRANSITIONS[stage] ?? [];
  const reasonOptions = stageReasonOptions(ctx.taxonomy, toStage);
  const needsReason = reasonOptions.length > 0 || ["quarantine", "recalled", "disposed"].includes(toStage);
  if (!ctx.canMove) return <span className="hint">Read-only</span>;
  if (!row.lot_number) {
    return <span className="hint">Lot actions need a lot number — this row has none.</span>;
  }
  async function doVerify() {
    if (!method.trim()) { setError("A verification method is required."); return; }
    setBusy(true); setError(null); setNotice(null);
    try {
      await client.inventoryLotVerify({
        sku: row.sku, location_code: row.location_code, lot_number: row.lot_number!,
        serial: row.serial ?? undefined, method: method.trim(), result,
        note: note.trim() || undefined,
      });
      setNotice(`Lot ${row.lot_number} verified (${result}).`);
      ctx.bump();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  async function doStage() {
    if (!toStage) { setError("Choose a target stage."); return; }
    if (needsReason && !reason) { setError(`A reason is required for a transition to ${toStage}.`); return; }
    setBusy(true); setError(null); setNotice(null);
    try {
      await client.inventoryLotStage({
        sku: row.sku, location_code: row.location_code, lot_number: row.lot_number!,
        serial: row.serial ?? undefined, to_stage: toStage,
        reason_code: needsReason ? reason : undefined,
        note: stageNote.trim() || undefined,
      });
      setNotice(`Lot ${row.lot_number} moved to ${toStage}.`);
      ctx.bump();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return (
    <span className="appt-actions">
      <button type="button" onClick={() => { setShowVerify((s) => !s); setShowStage(false); }}>
        Verify lot {lotId}
      </button>
      {transitions.length > 0 && (
        <button type="button" onClick={() => { setShowStage((s) => !s); setShowVerify(false); }}>
          Change stage {lotId}
        </button>
      )}
      {showVerify && (
        <form aria-label={`Verify lot ${lotId} form`} noValidate onSubmit={(e) => { e.preventDefault(); void doVerify(); }}>
          <label className="field">
            Method *
            <input aria-label={`Verify method for ${lotId}`} value={method} onChange={(e) => setMethod(e.target.value)} placeholder="e.g. lot-to-lot comparison" />
          </label>
          <label className="field">
            Result *
            <select aria-label={`Verify result for ${lotId}`} value={result} onChange={(e) => setResult(e.target.value)}>
              <option value="pass">pass</option>
              <option value="fail">fail</option>
            </select>
          </label>
          <label className="field">
            Note
            <input aria-label={`Verify note for ${lotId}`} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          <button type="submit" disabled={busy}>Submit verification for {lotId}</button>
        </form>
      )}
      {showStage && transitions.length > 0 && (
        <form aria-label={`Change stage for ${lotId} form`} noValidate onSubmit={(e) => { e.preventDefault(); void doStage(); }}>
          <label className="field">
            Target stage *
            <select aria-label={`Target stage for ${lotId}`} value={toStage} onChange={(e) => { setToStage(e.target.value); setReason(""); }}>
              <option value="">Select a stage</option>
              {transitions.map((s) => (<option key={s} value={s}>{s}</option>))}
            </select>
          </label>
          {needsReason && (
            <label className="field">
              Reason *
              <select aria-label={`Stage reason for ${lotId}`} value={reason} disabled={!ctx.taxonomy} onChange={(e) => setReason(e.target.value)}>
                <option value="">Select a reason</option>
                {reasonOptions.map((r) => (<option key={r.code} value={r.code}>{r.label}</option>))}
              </select>
              {!ctx.taxonomy && <span className="field-error">Loading taxonomy…</span>}
              {ctx.taxonomy && reasonOptions.length === 0 && (
                <span className="field-error">No governed reasons are published for {toStage}.</span>
              )}
            </label>
          )}
          <label className="field">
            Note
            <input aria-label={`Stage note for ${lotId}`} value={stageNote} onChange={(e) => setStageNote(e.target.value)} />
          </label>
          <button type="submit" disabled={busy}>Apply stage change for {lotId}</button>
        </form>
      )}
      {error && <p className="error-note" role="alert">{error}</p>}
      {notice && <p className="state state-empty" role="status">{notice}</p>}
    </span>
  );
}

function ContainerRow({ ctx, container, shelfExpiry }: { ctx: Ctx; container: InvContainer; shelfExpiry?: string | null }) {
  const { client } = ctx;
  const code = container.container_code;
  const [consumeQty, setConsumeQty] = useState("");
  const [disposeReason, setDisposeReason] = useState("");
  const [disposeNote, setDisposeNote] = useState("");
  const [transferTo, setTransferTo] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const terminal = ["disposed", "depleted", "recalled"].includes(container.stage);
  const disposalReasons = ctx.taxonomy?.disposalReasonCodes ?? [];
  const inUseGoverns = Boolean(
    container.in_use_expiry_date &&
      (!shelfExpiry || container.in_use_expiry_date <= shelfExpiry),
  );
  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true); setError(null); setNotice(null);
    try { await fn(); setNotice(`${label} — done.`); ctx.bump(); }
    catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  return (
    <tr>
      <td>{code}</td>
      <td className="num">{container.quantity}</td>
      <td><StatusBadge tone="neutral">{container.stage}</StatusBadge></td>
      <td>
        {container.effective_expiry ?? "—"}
        {inUseGoverns && container.effective_expiry ? " (in-use)" : ""}
        {container.in_use_expiry_date && (
          <><br /><span className="muted">in-use expiry {container.in_use_expiry_date}</span></>
        )}
      </td>
      <td>
        {ctx.canMove ? (
          <span className="appt-actions">
            <button
              type="button"
              disabled={busy || container.stage !== "released"}
              title={container.stage !== "released" ? `Open is only available for released containers — this container is ${container.stage}.` : undefined}
              onClick={() => void act(`Container ${code} opened`, () => client.inventoryContainerOpen({ container_code: code }))}
            >
              Open container {code}
            </button>
            {container.stage !== "released" && (
              <span className="field-error">Open needs a released container — this one is {container.stage}.</span>
            )}
            <label className="field">
              Consume quantity
              <input aria-label={`Consume quantity for ${code}`} type="number" min="1" value={consumeQty} onChange={(e) => setConsumeQty(e.target.value)} />
            </label>
            <button
              type="button"
              disabled={busy || container.stage !== "in_use"}
              title={container.stage !== "in_use" ? `Consume needs an opened (in_use) container — this container is ${container.stage}.` : undefined}
              onClick={() => {
                const q = Number(consumeQty);
                if (!Number.isFinite(q) || q <= 0) { setError("Consume quantity must be more than 0."); return; }
                void act(`Consumed ${q} from container ${code}`, () => client.inventoryContainerConsume({ container_code: code, quantity: q }));
              }}
            >
              Consume from container {code}
            </button>
            {container.stage !== "in_use" && (
              <span className="field-error">Consume needs an opened container — open it first (stage is {container.stage}).</span>
            )}
            <label className="field">
              Disposal reason
              <select aria-label={`Disposal reason for ${code}`} value={disposeReason} disabled={!ctx.taxonomy || terminal} onChange={(e) => setDisposeReason(e.target.value)}>
                <option value="">Select a reason</option>
                {disposalReasons.map((r) => (<option key={r.code} value={r.code}>{r.label}</option>))}
              </select>
              {!ctx.taxonomy && <span className="field-error">Loading taxonomy…</span>}
            </label>
            <label className="field">
              Disposal note
              <input aria-label={`Disposal note for ${code}`} value={disposeNote} onChange={(e) => setDisposeNote(e.target.value)} />
            </label>
            <button
              type="button"
              disabled={busy || terminal}
              title={terminal ? `Dispose is not available — this container is ${container.stage} (terminal).` : undefined}
              onClick={() => {
                if (!disposeReason) { setError("Choose a disposal reason from the governed list."); return; }
                void act(`Container ${code} disposed`, () => client.inventoryContainerDispose({ container_code: code, reason_code: disposeReason, note: disposeNote.trim() || undefined }));
              }}
            >
              Dispose container {code}
            </button>
            {terminal && <span className="field-error">Dispose is unavailable — this container is {container.stage} (terminal).</span>}
            <LocationSelect
              label={`Transfer destination for ${code}`}
              value={transferTo}
              onChange={setTransferTo}
              locations={ctx.locations}
              locationsReady={ctx.locationsReady}
            />
            <button
              type="button"
              disabled={busy || terminal || !transferTo}
              title={terminal ? `Transfer is not available — this container is ${container.stage} (terminal).` : !transferTo ? "Choose a transfer destination first." : undefined}
              onClick={() => void act(`Container ${code} transferred`, () => client.inventoryContainerTransfer({ container_code: code, to_location_code: transferTo }))}
            >
              Transfer container {code}
            </button>
            {terminal && <span className="field-error">Transfer is unavailable — this container is {container.stage} (terminal).</span>}
            {!terminal && !transferTo && <span className="field-error">Choose a destination to enable transfer.</span>}
            {error && <p className="error-note" role="alert">{error}</p>}
            {notice && <p className="state state-empty" role="status">{notice}</p>}
          </span>
        ) : (
          <span className="hint">Read-only</span>
        )}
      </td>
    </tr>
  );
}

function ContainersSection({ ctx, row }: { ctx: Ctx; row: InvStockRow }) {
  const { client } = ctx;
  const containers = useAsync(
    () =>
      client.inventoryContainers({
        sku: row.sku,
        location_code: row.location_code,
        lot_number: row.lot_number ?? undefined,
      }),
    [client, ctx.tick, row.sku, row.location_code, row.lot_number],
  );
  return (
    <section aria-label="Containers for selected lot">
      <h4>
        Containers — {row.sku} at {row.location_code}
        {row.lot_number ? ` (lot ${row.lot_number})` : ""}
      </h4>
      {containers.loading ? (
        <Loading label="Loading containers…" />
      ) : containers.error ? (
        <ErrorView error={containers.error} />
      ) : !containers.data || containers.data.length === 0 ? (
        <Empty label="No containers are recorded for this lot." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Container</th><th scope="col">Quantity</th>
              <th scope="col">Stage</th><th scope="col">Effective expiry</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {containers.data.map((c) => (
              <ContainerRow key={c.container_code} ctx={ctx} container={c} shelfExpiry={row.expiry_date} />
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function KitActions({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const kitItems = ctx.items.filter((i) => (i.structure ?? "simple") === "kit");
  const [sku, setSku] = useState("");
  const [locationCode, setLocationCode] = useState("");
  const [quantity, setQuantity] = useState("");
  const [lotNumber, setLotNumber] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function run(kind: "open" | "assemble" | "disassemble") {
    const qty = Number(quantity);
    if (!sku) { setError("Choose a kit item."); return; }
    if (!locationCode) { setError("Choose a location for the kit operation."); return; }
    if (!Number.isFinite(qty) || qty <= 0) { setError("Kit quantity must be more than 0."); return; }
    setBusy(true); setError(null); setNotice(null);
    try {
      const body = { sku, location_code: locationCode, quantity: qty, lot_number: lotNumber.trim() || undefined };
      if (kind === "open") await client.inventoryKitOpen(body);
      else if (kind === "assemble") await client.inventoryKitAssemble(body);
      else await client.inventoryKitDisassemble(body);
      setNotice(`Kit ${kind} for ${qty} × ${sku} at ${locationCode} — done.`);
      ctx.bump();
    } catch (err) { setError(errorText(err)); } finally { setBusy(false); }
  }
  if (!ctx.canMove) {
    return <p className="hint">Kit operations need the inventory.move capability — your role is read-only.</p>;
  }
  return (
    <section aria-label="Kit operations">
      <h3>Kit operations</h3>
      {kitItems.length === 0 ? (
        <p className="hint">No kit items in the item master yet — create an item with structure “kit” first; kit actions are disabled until one exists.</p>
      ) : null}
      <form className="inline-form" aria-label="Kit operations form" noValidate onSubmit={(e) => e.preventDefault()}>
        <ItemSelect label="Kit operation item" value={sku} onChange={setSku} items={kitItems} />
        <LocationSelect label="Kit operation location" value={locationCode} onChange={setLocationCode} locations={ctx.locations} locationsReady={ctx.locationsReady} />
        <label className="field">
          Kit quantity *
          <input aria-label="Kit operation quantity" type="number" min="1" value={quantity} onChange={(e) => setQuantity(e.target.value)} />
        </label>
        <label className="field">
          Kit lot number
          <input aria-label="Kit operation lot number" value={lotNumber} onChange={(e) => setLotNumber(e.target.value)} placeholder="Optional" />
        </label>
        <button type="button" disabled={busy || kitItems.length === 0} onClick={() => void run("open")}>Open kit</button>
        <button type="button" disabled={busy || kitItems.length === 0} onClick={() => void run("assemble")}>Assemble kit</button>
        <button type="button" disabled={busy || kitItems.length === 0} onClick={() => void run("disassemble")}>Disassemble kit</button>
        {kitItems.length === 0 && <span className="field-error">Kit actions are disabled — no kit items exist.</span>}
        {error && <p className="error-note" role="alert">{error}</p>}
        {notice && <p className="state state-empty" role="status">{notice}</p>}
      </form>
    </section>
  );
}

function StockPanel({ ctx }: { ctx: Ctx }) {
  const { client, taxonomy, canManage } = ctx;
  const [locationCode, setLocationCode] = useState("");
  const [category, setCategory] = useState("");
  const [selectedLot, setSelectedLot] = useState<InvStockRow | null>(null);
  const [parForm, setParForm] = useState({ sku: "", location_code: "", par_level: "", unit_level: "" });
  const [parError, setParError] = useState<string | null>(null);
  const [parNotice, setParNotice] = useState<string | null>(null);
  const [parBusy, setParBusy] = useState(false);

  const stock = useAsync(
    () =>
      client.inventoryStock({
        location_code: locationCode || undefined,
        category: category || undefined,
      }),
    [client, ctx.tick, locationCode, category],
  );
  const pars = useAsync(() => client.inventoryParLevels(), [client, ctx.tick]);

  async function savePar() {
    const level = Number(parForm.par_level);
    if (!parForm.sku) {
      setParError("Choose an item for the PAR level.");
      return;
    }
    if (!parForm.location_code) {
      setParError("Choose a location for the PAR level.");
      return;
    }
    if (!Number.isFinite(level) || level < 0) {
      setParError("PAR level must be a number of 0 or more.");
      return;
    }
    setParBusy(true);
    setParError(null);
    setParNotice(null);
    try {
      await client.setInventoryParLevel({
        sku: parForm.sku,
        location_code: parForm.location_code,
        par_level: level,
        unit_level: parForm.unit_level || undefined,
      });
      setParNotice(`PAR level for ${parForm.sku} at ${parForm.location_code} set to ${level}${parForm.unit_level ? ` ${parForm.unit_level}` : ""}.`);
      ctx.bump();
    } catch (err) {
      setParError(errorText(err));
    } finally {
      setParBusy(false);
    }
  }

  return (
    <div>
      <h3>Stock on hand — by lot</h3>
      <div className="inline-form" aria-label="Stock filters">
        <LocationSelect
          label="Stock location filter"
          value={locationCode}
          onChange={setLocationCode}
          locations={ctx.locations}
          locationsReady={ctx.locationsReady}
          allowAll
        />
        <label className="field">
          Category
          <select
            aria-label="Stock category filter"
            value={category}
            disabled={!taxonomy}
            onChange={(e) => setCategory(e.target.value)}
          >
            <option value="">All categories</option>
            {(taxonomy?.categories ?? []).map((c) => (
              <option key={c.code} value={c.code}>{c.label}</option>
            ))}
          </select>
          {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
        </label>
      </div>

      {stock.loading ? (
        <Loading label="Loading stock…" />
      ) : stock.error ? (
        <ErrorView error={stock.error} />
      ) : !stock.data || stock.data.length === 0 ? (
        <Empty label="No stock rows match the current filters." icon="boxes" />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">SKU</th><th scope="col">Item</th>
              <th scope="col">Location</th><th scope="col">Lot</th>
              <th scope="col">Serial</th><th scope="col">Expiry</th>
              <th scope="col">Quantity</th><th scope="col">Unit cost</th>
              <th scope="col">Value</th><th scope="col">Stage</th>
              <th scope="col">Verification</th><th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {stock.data.map((row, i) => (
              <tr key={`${row.sku}-${row.location_code}-${row.lot_number ?? ""}-${row.serial ?? ""}-${i}`}>
                <td>{row.sku}</td>
                <td>{row.name ?? nameFor(ctx, row.sku)}</td>
                <td>{row.location_code}</td>
                <td>{row.lot_number ?? "—"}</td>
                <td>{row.serial ?? "—"}</td>
                <td>{row.expiry_date ?? "—"}</td>
                <td className="num">{row.quantity}</td>
                <td>{money(row.unit_cost_cents)}</td>
                <td>{money(row.value_cents)}</td>
                <td>
                  <StatusBadge tone={(row.stage ?? "released") === "released" || (row.stage ?? "released") === "in_use" ? "ok" : (row.stage ?? "released") === "recalled" || (row.stage ?? "released") === "disposed" ? "bad" : "warn"}>
                    {row.stage ?? "released"}
                  </StatusBadge>
                  {row.recall_blocked && (
                    <>
                      <br />
                      <StatusBadge tone="bad">recall-blocked</StatusBadge>
                      {(row.blocked_by ?? []).length > 0 && (
                        <span className="muted">
                          {" "}by {(row.blocked_by ?? []).map((b) => `${b.sku}/${b.lot_number ?? "—"}`).join(", ")}
                        </span>
                      )}
                    </>
                  )}
                </td>
                <td>
                  {row.verification_result ? (
                    <span>
                      {row.verification_result}
                      {row.verified_on ? ` on ${row.verified_on}` : ""}
                      {row.verification_method ? ` (${row.verification_method})` : ""}
                      {row.verified_by ? ` by ${row.verified_by}` : ""}
                    </span>
                  ) : (
                    <span className="muted">not verified</span>
                  )}
                </td>
                <td>
                  <span className="appt-actions">
                    <LotActions ctx={ctx} row={row} />
                    <button type="button" onClick={() => setSelectedLot(row)}>
                      View containers for {row.lot_number ?? row.sku}
                    </button>
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedLot && <ContainersSection ctx={ctx} row={selectedLot} />}

      <KitActions ctx={ctx} />

      <h3>PAR levels</h3>
      {pars.loading ? (
        <Loading label="Loading PAR levels…" />
      ) : pars.error ? (
        <ErrorView error={pars.error} />
      ) : !pars.data || pars.data.length === 0 ? (
        <Empty label="No PAR levels set yet." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">SKU</th><th scope="col">Item</th>
              <th scope="col">Location</th><th scope="col">PAR level</th>
              <th scope="col">Entered as</th>
            </tr>
          </thead>
          <tbody>
            {pars.data.map((p) => (
              <tr key={`${p.sku}-${p.location_code}`}>
                <td>{p.sku}</td>
                <td>{p.name ?? nameFor(ctx, p.sku)}</td>
                <td>{p.location_code}</td>
                <td className="num">{p.par_level}</td>
                <td>
                  {p.par_unit_level
                    ? `${p.par_qty ?? p.par_level} ${p.par_unit_level}`
                    : "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canManage ? (
        <form
          className="inline-form"
          aria-label="Set PAR level"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void savePar();
          }}
        >
          <h4>Set a PAR level</h4>
          <ItemSelect
            label="PAR item"
            value={parForm.sku}
            onChange={(v) => setParForm((f) => ({ ...f, sku: v }))}
            items={ctx.items}
          />
          <LocationSelect
            label="PAR location"
            value={parForm.location_code}
            onChange={(v) => setParForm((f) => ({ ...f, location_code: v }))}
            locations={ctx.locations}
            locationsReady={ctx.locationsReady}
          />
          <label className="field">
            PAR level *
            <input
              aria-label="PAR level"
              type="number"
              min="0"
              value={parForm.par_level}
              onChange={(e) => setParForm((f) => ({ ...f, par_level: e.target.value }))}
            />
          </label>
          <UnitLevelSelect
            label="PAR unit level"
            value={parForm.unit_level}
            onChange={(v) => setParForm((f) => ({ ...f, unit_level: v }))}
            item={ctx.items.find((i) => i.sku === parForm.sku) ?? null}
          />
          <button type="submit" disabled={parBusy}>Save PAR level</button>
          {parError && <p className="error-note" role="alert">{parError}</p>}
          {parNotice && <p className="state state-empty" role="status">{parNotice}</p>}
        </form>
      ) : (
        <p className="hint">PAR editing needs the inventory.manage capability — your role is read-only.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Movements tab: the read-only ledger.
// ---------------------------------------------------------------------------

const MOVEMENT_EVENTS = [
  "receive", "issue", "transfer", "adjust", "case-issue",
  "kit_open", "kit_assemble", "kit_disassemble",
  "stage_transition", "container_open", "container_consume",
];

function MovementsPanel({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const [sku, setSku] = useState("");
  const [locationCode, setLocationCode] = useState("");
  const [event, setEvent] = useState("");
  const [reference, setReference] = useState("");
  const [patientRef, setPatientRef] = useState("");

  const movements = useAsync(
    () =>
      client.inventoryMovements({
        sku: sku.trim() || undefined,
        location_code: locationCode || undefined,
        event: event || undefined,
        reference: reference.trim() || undefined,
        patient_ref: patientRef.trim() || undefined,
      }),
    [client, ctx.tick, sku, locationCode, event, reference, patientRef],
  );
  const eventOptions = Array.from(
    new Set([
      ...MOVEMENT_EVENTS,
      ...((movements.data ?? [])
        .map((m) => m.event)
        .filter((e): e is string => Boolean(e))),
    ]),
  );

  return (
    <div>
      <h3>Movement ledger</h3>
      <p className="hint">
        The ledger is append-only: every receipt, issue, transfer,
        adjustment and case draw is a signed, audited row. It is
        read-only here — corrections are new adjustments, never edits.
      </p>
      <div className="inline-form" aria-label="Movement filters">
        <label className="field">
          Item SKU
          <input aria-label="Movement item SKU" value={sku} onChange={(e) => setSku(e.target.value)} placeholder="SKU" />
        </label>
        <LocationSelect
          label="Movement location filter"
          value={locationCode}
          onChange={setLocationCode}
          locations={ctx.locations}
          locationsReady={ctx.locationsReady}
          allowAll
        />
        <label className="field">
          Event
          <select aria-label="Movement event filter" value={event} onChange={(e) => setEvent(e.target.value)}>
            <option value="">All events</option>
            {eventOptions.map((ev) => (
              <option key={ev} value={ev}>{ev}</option>
            ))}
          </select>
        </label>
        <label className="field">
          Reference
          <input aria-label="Movement reference filter" value={reference} onChange={(e) => setReference(e.target.value)} />
        </label>
        <label className="field">
          Patient reference
          <input aria-label="Movement patient reference filter" value={patientRef} onChange={(e) => setPatientRef(e.target.value)} placeholder="Patient/…" />
        </label>
      </div>

      {movements.loading ? (
        <Loading label="Loading movements…" />
      ) : movements.error ? (
        <ErrorView error={movements.error} />
      ) : !movements.data || movements.data.length === 0 ? (
        <Empty label="No movements match the current filters." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Time</th><th scope="col">Item</th>
              <th scope="col">Event</th><th scope="col">Location</th>
              <th scope="col">Quantity</th><th scope="col">Lot / serial</th>
              <th scope="col">Unit level</th><th scope="col">Container</th>
              <th scope="col">Expiry</th><th scope="col">Value</th>
              <th scope="col">Reference</th><th scope="col">Actor</th>
            </tr>
          </thead>
          <tbody>
            {movements.data.map((m, i) => {
              const qty = m.quantity ?? m.quantity_delta ?? 0;
              const place = m.from_location || m.to_location
                ? `${m.from_location ?? "—"} → ${m.to_location ?? "—"}`
                : m.location_code ?? "—";
              return (
                <tr key={m.id ?? `${m.sku}-${m.created_at ?? ""}-${i}`}>
                  <td>{m.created_at ? m.created_at.slice(0, 19).replace("T", " ") : "—"}</td>
                  <td>{m.sku}{m.name ?? nameFor(ctx, m.sku) ? <><br /><span className="muted">{m.name ?? nameFor(ctx, m.sku)}</span></> : null}</td>
                  <td>{m.event ? <StatusBadge tone="neutral">{m.event}</StatusBadge> : "—"}</td>
                  <td>{place}</td>
                  <td className="num">{qty > 0 ? `+${qty}` : qty}</td>
                  <td>{m.lot_number ?? m.serial ?? "—"}</td>
                  <td>{m.unit_level ?? "—"}</td>
                  <td>{m.container_code ?? "—"}</td>
                  <td>{m.expiry_date ?? "—"}</td>
                  <td>{money(m.value_cents)}</td>
                  <td>{m.reference ?? "—"}</td>
                  <td>{m.actor ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Receiving & Issue tab: receive stepper, issue/transfer/adjust
// stepper, case draw, requisitions and an RFID event recorder.
// ---------------------------------------------------------------------------

function ReceivingPanel({ ctx }: { ctx: Ctx }) {
  if (!ctx.canMove) {
    return (
      <div>
        <h3>Receiving &amp; issue</h3>
        <p className="hint">
          Stock movements need the inventory.move capability — your role
          is read-only, so the receiving, issue, transfer, adjustment,
          case-draw and requisition actions are hidden. You can still
          review stock and the movement ledger in the other tabs.
        </p>
      </div>
    );
  }
  return (
    <div>
      <ReceiveStepper ctx={ctx} />
      <MoveStepper ctx={ctx} />
      <CaseIssuePanel ctx={ctx} />
      <RequisitionsPanel ctx={ctx} />
      <RfidPanel ctx={ctx} />
    </div>
  );
}

function ReceiveStepper({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const [step, setStep] = useState(0);
  const [scanValue, setScanValue] = useState("");
  const [scanMsg, setScanMsg] = useState<string | null>(null);
  const [scanBusy, setScanBusy] = useState(false);
  const [form, setForm] = useState({
    sku: "",
    location_code: "",
    quantity: "",
    lot_number: "",
    serial: "",
    expiry_date: "",
    costDollars: "",
    reference: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [unitLevel, setUnitLevel] = useState("");
  const [containerRows, setContainerRows] = useState<
    Array<{ container_code: string; quantity: string }>
  >([]);
  const [containerCount, setContainerCount] = useState("");
  const [componentLotValues, setComponentLotValues] = useState<
    Record<string, { lot_number: string; expiry_date: string }>
  >({});
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const item = ctx.items.find((i) => i.sku === form.sku) ?? null;
  const receivePolicy = resolveClassPolicy(ctx.taxonomy, item);
  const containerTracked = receivePolicy.containerTracked === true;
  const isKitItem = (item?.structure ?? "simple") === "kit";

  async function doScan() {
    const value = scanValue.trim();
    if (!value) return;
    setScanBusy(true);
    setScanMsg(null);
    try {
      const res = await client.inventoryScan({
        value,
        location_code: form.location_code || undefined,
      });
      if (res.resolved && res.item) {
        const lot = (res.lot ?? {}) as Record<string, unknown>;
        patch({
          sku: res.item.sku,
          lot_number: typeof lot.lot_number === "string" ? lot.lot_number : form.lot_number,
          expiry_date: typeof lot.expiry_date === "string" ? lot.expiry_date.slice(0, 10) : form.expiry_date,
        });
        setScanMsg(`Resolved via ${res.via ?? "scan"}: ${res.item.sku} — ${res.item.name}.`);
      } else {
        setScanMsg(`Not resolved: “${value}” did not match an item, lot or serial. Enter the item manually below.`);
      }
    } catch (err) {
      setScanMsg(`Scan failed: ${errorText(err)}`);
    } finally {
      setScanBusy(false);
    }
  }

  function nextFromStep0() {
    if (!form.sku) {
      setError("Choose the item to receive (scan a code or pick from the item master).");
      return;
    }
    if (!form.location_code) {
      setError("Choose the receiving location.");
      return;
    }
    setError(null);
    setStep(1);
  }

  async function submitReceive() {
    const qty = Number(form.quantity);
    if (!Number.isFinite(qty) || qty <= 0) {
      setError("Quantity must be more than 0.");
      return;
    }
    if (item?.lot_tracked && !form.lot_number.trim()) {
      setError(`${item.sku} is lot-tracked — a lot number is required.`);
      return;
    }
    if (item?.serial_tracked && !form.serial.trim()) {
      setError(`${item.sku} is serial-tracked — a serial is required.`);
      return;
    }
    if (item?.expiry_tracked && !form.expiry_date) {
      setError(`${item.sku} is expiry-tracked — an expiry date is required.`);
      return;
    }
    const cents = dollarsToCents(form.costDollars);
    if (Number.isNaN(cents)) {
      setError("Lot unit cost must be a dollar amount, e.g. 12.50.");
      return;
    }
    // Container declarations: each quantity must be positive; the
    // server checks that they sum to the received quantity.
    let containersPayload:
      | Array<{ container_code?: string; quantity: number }>
      | undefined;
    if (containerTracked && containerRows.length > 0) {
      const parsed = containerRows.map((r) => ({
        container_code: r.container_code.trim() || undefined,
        quantity: Number(r.quantity),
      }));
      if (parsed.some((r) => !Number.isFinite(r.quantity) || r.quantity <= 0)) {
        setError("Each declared container quantity must be more than 0.");
        return;
      }
      containersPayload = parsed;
    }
    let containerCountPayload: number | undefined;
    if (containerTracked && !containersPayload && containerCount.trim()) {
      const n = Number(containerCount);
      if (!Number.isInteger(n) || n <= 0) {
        setError("Container count must be a positive whole number.");
        return;
      }
      containerCountPayload = n;
    }
    // Kit component lots: one declaration per component.
    let componentLotsPayload:
      | Array<{ sku: string; lot_number?: string; expiry_date?: string }>
      | undefined;
    if (isKitItem && item) {
      const comps = item.kit_components ?? [];
      if (comps.length > 0) {
        componentLotsPayload = comps.map((c) => {
          const v = componentLotValues[c.sku] ?? { lot_number: "", expiry_date: "" };
          return {
            sku: c.sku,
            lot_number: v.lot_number.trim() || undefined,
            expiry_date: v.expiry_date || undefined,
          };
        });
        if (
          item.kit_mode === "track-whole" &&
          componentLotsPayload.some((c) => !c.lot_number)
        ) {
          setError("Track-whole kits need a component lot number for every component.");
          return;
        }
      }
    }
    setBusy(true);
    setError(null);
    try {
      const movement = await client.inventoryReceive({
        sku: form.sku,
        location_code: form.location_code,
        quantity: qty,
        lot_number: form.lot_number.trim() || undefined,
        serial: form.serial.trim() || undefined,
        expiry_date: form.expiry_date || undefined,
        unit_cost_cents: cents ?? undefined,
        reference: form.reference.trim() || undefined,
        unit_level: unitLevel || undefined,
        containers: containersPayload,
        container_count: containerCountPayload,
        component_lots: componentLotsPayload,
      });
      setDone(
        `Received ${qty} × ${form.sku} into ${form.location_code}` +
          `${form.lot_number.trim() ? ` (lot ${form.lot_number.trim()})` : ""}` +
          `${movement.id !== undefined ? ` — movement #${movement.id}` : ""}.`,
      );
      setStep(2);
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setForm({
      sku: "", location_code: "", quantity: "", lot_number: "", serial: "",
      expiry_date: "", costDollars: "", reference: "",
    });
    setScanValue("");
    setScanMsg(null);
    setDone(null);
    setError(null);
    setUnitLevel("");
    setContainerRows([]);
    setContainerCount("");
    setComponentLotValues({});
    setStep(0);
  }

  return (
    <section aria-label="Receive stock" id="inventory-receive">
      <h3>Receive stock</h3>
      <Stepper steps={["Item & location", "Quantity & lot details", "Received"]} current={step} />
      {step === 0 && (
        <div>
          <form
            className="inline-form"
            aria-label="Scan to prefill receipt"
            onSubmit={(e) => {
              e.preventDefault();
              void doScan();
            }}
          >
            <label className="field">
              Scan or type a code
              <input
                aria-label="Scan code for receiving"
                value={scanValue}
                onChange={(e) => setScanValue(e.target.value)}
                placeholder="Barcode, lot, serial or SKU"
              />
            </label>
            <button type="submit" disabled={scanBusy || !scanValue.trim()}>
              Scan
            </button>
          </form>
          {scanMsg && <p className="hint" role="status">{scanMsg}</p>}
          <div className="inline-form">
            <ItemSelect label="Receive item" value={form.sku} onChange={(v) => patch({ sku: v })} items={ctx.items} />
            <LocationSelect
              label="Receiving location"
              value={form.location_code}
              onChange={(v) => patch({ location_code: v })}
              locations={ctx.locations}
              locationsReady={ctx.locationsReady}
            />
            <button type="button" onClick={nextFromStep0}>Continue</button>
          </div>
        </div>
      )}
      {step === 1 && (
        <form
          className="inline-form"
          aria-label="Receive stock details"
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void submitReceive();
          }}
        >
          <p className="hint">
            Receiving {form.sku}{item ? ` — ${item.name}` : ""} into {form.location_code}.
          </p>
          <label className="field">
            Quantity *
            <input
              aria-label="Receive quantity"
              type="number"
              min="1"
              value={form.quantity}
              onChange={(e) => patch({ quantity: e.target.value })}
            />
          </label>
          <UnitLevelSelect
            label="Receive unit level"
            value={unitLevel}
            onChange={setUnitLevel}
            item={item}
          />
          {containerTracked && (
            <div aria-label="Receive containers">
              <h5>Containers (container-tracked class)</h5>
              <p className="hint">
                Optionally declare the containers this receipt is split
                across — their quantities must sum to the received
                quantity — or give a container count and the server
                splits it evenly. With neither, one container is created
                for the whole receipt.
              </p>
              {containerRows.map((row, idx) => (
                <span key={idx} className="inline-form">
                  <label className="field">
                    Container code
                    <input
                      aria-label={`Receive container ${idx + 1} code`}
                      value={row.container_code}
                      onChange={(e) => setContainerRows((rs) => rs.map((r, j) => (j === idx ? { ...r, container_code: e.target.value } : r)))}
                      placeholder="Optional — auto-generated"
                    />
                  </label>
                  <label className="field">
                    Container quantity
                    <input
                      aria-label={`Receive container ${idx + 1} quantity`}
                      type="number"
                      min="1"
                      value={row.quantity}
                      onChange={(e) => setContainerRows((rs) => rs.map((r, j) => (j === idx ? { ...r, quantity: e.target.value } : r)))}
                    />
                  </label>
                  <button type="button" className="linklike" onClick={() => setContainerRows((rs) => rs.filter((_, j) => j !== idx))}>
                    Remove container {idx + 1}
                  </button>
                </span>
              ))}
              <button type="button" className="linklike" onClick={() => setContainerRows((rs) => [...rs, { container_code: "", quantity: "" }])}>
                Add container
              </button>
              <label className="field">
                Container count
                <input
                  aria-label="Receive container count"
                  type="number"
                  min="1"
                  value={containerCount}
                  disabled={containerRows.length > 0}
                  onChange={(e) => setContainerCount(e.target.value)}
                  placeholder="Alternative to declaring containers"
                />
                {containerRows.length > 0 && (
                  <span className="field-error">Container count is disabled while containers are declared — give one or the other.</span>
                )}
              </label>
            </div>
          )}
          {isKitItem && item && (item.kit_components ?? []).length > 0 && (
            <div aria-label="Receive kit component lots">
              <h5>Kit component lots</h5>
              <p className="hint">
                One lot declaration per kit component
                {item.kit_mode === "track-whole"
                  ? " — required for track-whole kits; the kit expiry is the earliest component expiry."
                  : " — the receipt is broken into these component lots."}
              </p>
              {(item.kit_components ?? []).map((comp) => {
                const v = componentLotValues[comp.sku] ?? { lot_number: "", expiry_date: "" };
                return (
                  <span key={comp.sku} className="inline-form">
                    <label className="field">
                      Component lot for {comp.sku}
                      <input
                        aria-label={`Component lot for ${comp.sku}`}
                        value={v.lot_number}
                        onChange={(e) => setComponentLotValues((m) => ({ ...m, [comp.sku]: { ...(m[comp.sku] ?? { lot_number: "", expiry_date: "" }), lot_number: e.target.value } }))}
                      />
                    </label>
                    <label className="field">
                      Component expiry for {comp.sku}
                      <input
                        aria-label={`Component expiry for ${comp.sku}`}
                        type="date"
                        value={v.expiry_date}
                        onChange={(e) => setComponentLotValues((m) => ({ ...m, [comp.sku]: { ...(m[comp.sku] ?? { lot_number: "", expiry_date: "" }), expiry_date: e.target.value } }))}
                      />
                    </label>
                  </span>
                );
              })}
            </div>
          )}
          {item?.lot_tracked && (
            <label className="field">
              Lot number *
              <input aria-label="Receive lot number" value={form.lot_number} onChange={(e) => patch({ lot_number: e.target.value })} />
            </label>
          )}
          {item?.serial_tracked && (
            <label className="field">
              Serial *
              <input aria-label="Receive serial" value={form.serial} onChange={(e) => patch({ serial: e.target.value })} />
            </label>
          )}
          {item?.expiry_tracked && (
            <label className="field">
              Expiry date *
              <input aria-label="Receive expiry date" type="date" value={form.expiry_date} onChange={(e) => patch({ expiry_date: e.target.value })} />
            </label>
          )}
          {!item && (
            <p className="hint">Item not in the loaded catalogue — lot, serial and expiry fields follow the item&apos;s tracking flags once it is selected.</p>
          )}
          <label className="field">
            Lot unit cost ($)
            <input aria-label="Receive lot unit cost in dollars" value={form.costDollars} onChange={(e) => patch({ costDollars: e.target.value })} placeholder="Optional — defaults to item cost" inputMode="decimal" />
          </label>
          <label className="field">
            Reference
            <input aria-label="Receive reference" value={form.reference} onChange={(e) => patch({ reference: e.target.value })} placeholder="Delivery note / PO" />
          </label>
          <button type="button" className="linklike" onClick={() => setStep(0)}>Back</button>
          <button type="submit" disabled={busy}>Receive stock</button>
        </form>
      )}
      {step === 2 && done && (
        <div className="state state-empty" role="status">
          <p>{done}</p>
          <button type="button" onClick={reset}>Receive another</button>
        </div>
      )}
      {error && <p className="error-note" role="alert">{error}</p>}
    </section>
  );
}

type MoveMode = "issue" | "transfer" | "adjust";

function MoveStepper({ ctx }: { ctx: Ctx }) {
  const { client, taxonomy } = ctx;
  const [mode, setMode] = useState<MoveMode>("issue");
  const [form, setForm] = useState({
    sku: "",
    location_code: "",
    from_location: "",
    to_location: "",
    quantity: "",
    quantity_delta: "",
    lot_number: "",
    serial: "",
    reason_code: "",
    note: "",
    reference: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [unitLevel, setUnitLevel] = useState("");
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));
  const moveItem = ctx.items.find((i) => i.sku === form.sku) ?? null;

  async function submit() {
    setError(null);
    setDone(null);
    if (!form.sku) {
      setError("Choose an item.");
      return;
    }
    setBusy(true);
    try {
      if (mode === "issue") {
        const qty = Number(form.quantity);
        if (!form.location_code) throw new Error("Choose the issuing location.");
        if (!Number.isFinite(qty) || qty <= 0) throw new Error("Quantity must be more than 0.");
        await client.inventoryIssue({
          sku: form.sku,
          location_code: form.location_code,
          quantity: qty,
          lot_number: form.lot_number.trim() || undefined,
          reference: form.reference.trim() || undefined,
          unit_level: unitLevel || undefined,
        });
        setDone(`Issued ${qty} × ${form.sku} from ${form.location_code} (FEFO).`);
      } else if (mode === "transfer") {
        const qty = Number(form.quantity);
        if (!form.from_location || !form.to_location) throw new Error("Choose both the from and to locations.");
        if (form.from_location === form.to_location) throw new Error("From and to locations must differ.");
        if (!Number.isFinite(qty) || qty <= 0) throw new Error("Quantity must be more than 0.");
        await client.inventoryTransfer({
          sku: form.sku,
          from_location: form.from_location,
          to_location: form.to_location,
          quantity: qty,
          lot_number: form.lot_number.trim() || undefined,
          unit_level: unitLevel || undefined,
        });
        setDone(`Transferred ${qty} × ${form.sku} from ${form.from_location} to ${form.to_location}.`);
      } else {
        const delta = Number(form.quantity_delta);
        if (!form.location_code) throw new Error("Choose the location to adjust.");
        if (!Number.isFinite(delta) || delta === 0) throw new Error("Quantity change must be a non-zero number (negative removes stock).");
        if (!form.reason_code) throw new Error("Choose a reason code from the taxonomy.");
        if (form.reason_code === "other" && !form.note.trim()) {
          throw new Error("A note is required when the reason is “other”.");
        }
        await client.inventoryAdjust({
          sku: form.sku,
          location_code: form.location_code,
          lot_number: form.lot_number.trim() || undefined,
          serial: form.serial.trim() || undefined,
          quantity_delta: delta,
          reason_code: form.reason_code,
          note: form.note.trim() || undefined,
          unit_level: unitLevel || undefined,
        });
        setDone(`Adjusted ${form.sku} at ${form.location_code} by ${delta > 0 ? `+${delta}` : delta} (${form.reason_code}).`);
      }
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Issue, transfer or adjust stock">
      <h3>Issue / transfer / adjust</h3>
      <Stepper
        steps={["Movement type", "Details", "Result"]}
        current={done ? 2 : 1}
      />
      <div className="inline-form" role="group" aria-label="Movement mode">
        <label className="field">
          Movement type
          <select
            aria-label="Movement type"
            value={mode}
            onChange={(e) => {
              setMode(e.target.value as MoveMode);
              setError(null);
              setDone(null);
            }}
          >
            <option value="issue">Issue</option>
            <option value="transfer">Transfer</option>
            <option value="adjust">Adjust</option>
          </select>
        </label>
      </div>
      {mode === "issue" && (
        <p className="hint">
          Issues allocate first-expiry-first-out (FEFO): the
          earliest-expiring lot is drawn first unless a specific lot is
          named.
        </p>
      )}
      <form
        className="inline-form"
        aria-label="Issue transfer adjust form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <ItemSelect label="Movement item" value={form.sku} onChange={(v) => patch({ sku: v })} items={ctx.items} />
        <UnitLevelSelect
          label="Movement unit level"
          value={unitLevel}
          onChange={setUnitLevel}
          item={moveItem}
        />
        {mode === "issue" && (
          <>
            <LocationSelect label="Issuing location" value={form.location_code} onChange={(v) => patch({ location_code: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} />
            <label className="field">
              Quantity *
              <input aria-label="Issue quantity" type="number" min="1" value={form.quantity} onChange={(e) => patch({ quantity: e.target.value })} />
            </label>
            <label className="field">
              Lot number
              <input aria-label="Issue lot number" value={form.lot_number} onChange={(e) => patch({ lot_number: e.target.value })} placeholder="Optional — FEFO when blank" />
            </label>
            <label className="field">
              Reference
              <input aria-label="Issue reference" value={form.reference} onChange={(e) => patch({ reference: e.target.value })} />
            </label>
          </>
        )}
        {mode === "transfer" && (
          <>
            <LocationSelect label="From location" value={form.from_location} onChange={(v) => patch({ from_location: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} />
            <LocationSelect label="To location" value={form.to_location} onChange={(v) => patch({ to_location: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} />
            <label className="field">
              Quantity *
              <input aria-label="Transfer quantity" type="number" min="1" value={form.quantity} onChange={(e) => patch({ quantity: e.target.value })} />
            </label>
            <label className="field">
              Lot number
              <input aria-label="Transfer lot number" value={form.lot_number} onChange={(e) => patch({ lot_number: e.target.value })} placeholder="Optional — FEFO when blank" />
            </label>
          </>
        )}
        {mode === "adjust" && (
          <>
            <LocationSelect label="Adjustment location" value={form.location_code} onChange={(v) => patch({ location_code: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} />
            <label className="field">
              Lot number
              <input aria-label="Adjust lot number" value={form.lot_number} onChange={(e) => patch({ lot_number: e.target.value })} />
            </label>
            <label className="field">
              Serial
              <input aria-label="Adjust serial" value={form.serial} onChange={(e) => patch({ serial: e.target.value })} />
            </label>
            <label className="field">
              Quantity change *
              <input aria-label="Adjust quantity change" type="number" value={form.quantity_delta} onChange={(e) => patch({ quantity_delta: e.target.value })} placeholder="e.g. -2" />
            </label>
            <label className="field">
              Reason *
              <select aria-label="Adjust reason" value={form.reason_code} disabled={!taxonomy} onChange={(e) => patch({ reason_code: e.target.value })}>
                <option value="">Select a reason</option>
                {(taxonomy?.reasonCodes ?? []).map((r) => (
                  <option key={r.code} value={r.code}>{r.label}</option>
                ))}
              </select>
              {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
            </label>
            <label className="field">
              Note{form.reason_code === "other" ? " *" : ""}
              <input aria-label="Adjust note" value={form.note} onChange={(e) => patch({ note: e.target.value })} placeholder={form.reason_code === "other" ? "Required for “other”" : "Optional"} />
            </label>
          </>
        )}
        <button type="submit" disabled={busy}>
          {mode === "issue" ? "Issue stock" : mode === "transfer" ? "Transfer stock" : "Adjust stock"}
        </button>
        {error && <p className="error-note" role="alert">{error}</p>}
        {done && <p className="state state-empty" role="status">{done}</p>}
      </form>
    </section>
  );
}

function allocationText(alloc: Record<string, unknown>): string {
  const lot = typeof alloc.lot_number === "string" ? alloc.lot_number : null;
  const qty = alloc.quantity ?? alloc.qty;
  const serial = typeof alloc.serial === "string" ? alloc.serial : null;
  const parts = [
    lot ? `lot ${lot}` : null,
    serial ? `serial ${serial}` : null,
    qty !== undefined ? `× ${String(qty)}` : null,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : JSON.stringify(alloc);
}

function CaseIssuePanel({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const [form, setForm] = useState({ card_id: "", case_id: "", location_code: "", patient_ref: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<Awaited<ReturnType<ApiClient["inventoryCaseIssue"]>> | null>(null);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function submit() {
    if (!form.card_id.trim()) {
      setError("A case card id is required.");
      return;
    }
    if (!form.case_id.trim()) {
      setError("A case id is required.");
      return;
    }
    if (!form.location_code) {
      setError("Choose the location to draw from.");
      return;
    }
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const out = await client.inventoryCaseIssue({
        card_id: form.card_id.trim(),
        case_id: form.case_id.trim(),
        location_code: form.location_code,
        patient_ref: form.patient_ref.trim() || undefined,
      });
      setResult(out);
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="Case issue">
      <h3>Case issue — draw a case card</h3>
      <p className="hint">
        Drawing a card issues its bill of materials from the chosen
        location (FEFO) and records the allocations against the case —
        implants drawn this way appear in the implant trace.
      </p>
      <form
        className="inline-form"
        aria-label="Case issue form"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="field">
          Card id *
          <input aria-label="Case card id" value={form.card_id} onChange={(e) => patch({ card_id: e.target.value })} />
        </label>
        <label className="field">
          Case id *
          <input aria-label="Case id" value={form.case_id} onChange={(e) => patch({ case_id: e.target.value })} />
        </label>
        <LocationSelect label="Draw location" value={form.location_code} onChange={(v) => patch({ location_code: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} />
        <label className="field">
          Patient reference
          <input aria-label="Case patient reference" value={form.patient_ref} onChange={(e) => patch({ patient_ref: e.target.value })} placeholder="Patient/…" />
        </label>
        <button type="submit" disabled={busy}>Issue case</button>
        {error && <p className="error-note" role="alert">{error}</p>}
      </form>
      {result && (
        <div>
          <p role="status">
            Case {result.case_id} drawn from card {result.card_id}:{" "}
            {result.lines.length} line{result.lines.length === 1 ? "" : "s"} allocated.
          </p>
          {result.lines.length === 0 ? (
            <Empty label="The card allocated no lines." />
          ) : (
            <table className="data-table">
              <thead>
                <tr><th scope="col">SKU</th><th scope="col">Quantity</th><th scope="col">Allocations</th></tr>
              </thead>
              <tbody>
                {result.lines.map((line, i) => (
                  <tr key={`${line.sku}-${i}`}>
                    <td>{line.sku}</td>
                    <td className="num">{line.qty}</td>
                    <td>{line.allocations.map(allocationText).join("; ") || "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </section>
  );
}

function requisitionId(req: { id?: string; req_id?: string }): string {
  return req.req_id ?? req.id ?? "";
}

function RequisitionsPanel({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const [fromCode, setFromCode] = useState("");
  const [toCode, setToCode] = useState("");
  const [reference, setReference] = useState("");
  const [lines, setLines] = useState<Array<{ sku: string; quantity: string }>>([
    { sku: "", quantity: "" },
  ]);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const list = useAsync(() => client.inventoryRequisitions(), [client, ctx.tick]);

  async function act(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await fn();
      setNotice(`${label} — done.`);
      ctx.bump();
    } catch (err) {
      setError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    if (!fromCode || !toCode) {
      setError("Choose both the requesting (to) and supplying (from) locations.");
      return;
    }
    const parsed = lines
      .filter((l) => l.sku)
      .map((l) => ({ sku: l.sku, quantity: Number(l.quantity) }));
    if (parsed.length === 0 || parsed.some((l) => !Number.isFinite(l.quantity) || l.quantity <= 0)) {
      setError("Add at least one line with an item and a quantity above 0.");
      return;
    }
    await act("Requisition created", async () => {
      await client.createInventoryRequisition({
        from_location_code: fromCode,
        to_location_code: toCode,
        lines: parsed,
        reference: reference.trim() || undefined,
      });
      setLines([{ sku: "", quantity: "" }]);
      setReference("");
    });
  }

  return (
    <section aria-label="Requisitions">
      <h3>Requisitions</h3>
      <form
        className="inline-form"
        aria-label="Create requisition"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void create();
        }}
      >
        <h4>Request stock</h4>
        <LocationSelect label="Supplying location (from)" value={fromCode} onChange={setFromCode} locations={ctx.locations} locationsReady={ctx.locationsReady} />
        <LocationSelect label="Requesting location (to)" value={toCode} onChange={setToCode} locations={ctx.locations} locationsReady={ctx.locationsReady} />
        <label className="field">
          Reference
          <input aria-label="Requisition reference" value={reference} onChange={(e) => setReference(e.target.value)} />
        </label>
        {lines.map((line, i) => (
          <span key={i} className="inline-form">
            <ItemSelect
              label={`Requisition line ${i + 1} item`}
              value={line.sku}
              onChange={(v) =>
                setLines((ls) => ls.map((l, j) => (j === i ? { ...l, sku: v } : l)))
              }
              items={ctx.items}
            />
            <label className="field">
              Quantity
              <input
                aria-label={`Requisition line ${i + 1} quantity`}
                type="number"
                min="1"
                value={line.quantity}
                onChange={(e) =>
                  setLines((ls) => ls.map((l, j) => (j === i ? { ...l, quantity: e.target.value } : l)))
                }
              />
            </label>
            {lines.length > 1 && (
              <button
                type="button"
                className="linklike"
                onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}
              >
                Remove line {i + 1}
              </button>
            )}
          </span>
        ))}
        <button
          type="button"
          className="linklike"
          onClick={() => setLines((ls) => [...ls, { sku: "", quantity: "" }])}
        >
          Add line
        </button>
        <button type="submit" disabled={busy}>Create requisition</button>
        {error && <p className="error-note" role="alert">{error}</p>}
        {notice && <p className="state state-empty" role="status">{notice}</p>}
      </form>

      {list.loading ? (
        <Loading label="Loading requisitions…" />
      ) : list.error ? (
        <ErrorView error={list.error} />
      ) : !list.data || list.data.length === 0 ? (
        <Empty label="No requisitions yet." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Requisition</th><th scope="col">From</th>
              <th scope="col">To</th><th scope="col">Status</th>
              <th scope="col">Lines</th><th scope="col">Reference</th>
              <th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((req) => {
              const id = requisitionId(req);
              const open = req.status !== "fulfilled" && req.status !== "cancelled";
              return (
                <tr key={id || `${req.from_location_code}-${req.to_location_code}`}>
                  <td>{id || "—"}</td>
                  <td>{req.from_location_code}</td>
                  <td>{req.to_location_code}</td>
                  <td>
                    <StatusBadge tone={req.status === "fulfilled" ? "ok" : req.status === "cancelled" ? "bad" : "warn"}>
                      {req.status}
                    </StatusBadge>
                  </td>
                  <td>
                    {req.lines.map((l) => `${l.sku} × ${l.quantity}${l.issued ? ` (issued ${l.issued})` : ""}`).join("; ")}
                  </td>
                  <td>{req.reference ?? "—"}</td>
                  <td>
                    {open ? (
                      <span className="appt-actions">
                        <button type="button" disabled={busy} onClick={() => void act(`Requisition ${id} issued`, () => client.issueInventoryRequisition(id))}>
                          Issue requisition {id}
                        </button>
                        <button type="button" disabled={busy} onClick={() => void act(`Requisition ${id} cancelled`, () => client.cancelInventoryRequisition(id))}>
                          Cancel requisition {id}
                        </button>
                      </span>
                    ) : (
                      <span className="hint">Closed</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </section>
  );
}

function RfidPanel({ ctx }: { ctx: Ctx }) {
  const { client } = ctx;
  const [form, setForm] = useState({ epc: "", reader_id: "", zone: "", context: "receiving", location_code: "" });
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const patch = (p: Partial<typeof form>) => setForm((f) => ({ ...f, ...p }));

  async function submit() {
    if (!form.epc.trim()) {
      setError("An EPC value is required.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await client.inventoryRfidEvent({
        epc: form.epc.trim(),
        reader_id: form.reader_id.trim() || undefined,
        zone: form.zone.trim() || undefined,
        context: form.context.trim() || "receiving",
        location_code: form.location_code || undefined,
      });
      setNotice(`RFID event recorded for ${form.epc.trim()}.`);
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-label="RFID events">
      <h3>RFID event</h3>
      <p className="hint">
        Reader events are recorded through the API (the reader feed in
        this deployment is simulated — no live reader link is claimed).
      </p>
      <form
        className="inline-form"
        aria-label="Record RFID event"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <label className="field">
          EPC *
          <input aria-label="RFID EPC" value={form.epc} onChange={(e) => patch({ epc: e.target.value })} placeholder="EPC-…" />
        </label>
        <label className="field">
          Reader id
          <input aria-label="RFID reader id" value={form.reader_id} onChange={(e) => patch({ reader_id: e.target.value })} />
        </label>
        <label className="field">
          Zone
          <input aria-label="RFID zone" value={form.zone} onChange={(e) => patch({ zone: e.target.value })} />
        </label>
        <label className="field">
          Context
          <input aria-label="RFID context" value={form.context} onChange={(e) => patch({ context: e.target.value })} />
        </label>
        <LocationSelect label="RFID location" value={form.location_code} onChange={(v) => patch({ location_code: v })} locations={ctx.locations} locationsReady={ctx.locationsReady} allowAll />
        <button type="submit" disabled={busy}>Record RFID event</button>
        {error && <p className="error-note" role="alert">{error}</p>}
        {notice && <p className="state state-empty" role="status">{notice}</p>}
      </form>
    </section>
  );
}

// ---------------------------------------------------------------------------
// Counts tab: start a cycle count, enter counted quantities against
// the expected lines, complete with a reason when variances exist.
// ---------------------------------------------------------------------------

function countId(count: InvCount): string {
  return count.count_id ?? count.id ?? "";
}

function CountsPanel({ ctx }: { ctx: Ctx }) {
  const { client, taxonomy, canManage } = ctx;
  const [startLocation, setStartLocation] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [counted, setCounted] = useState<Record<string, string>>({});
  const [reasonCode, setReasonCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const list = useAsync(() => client.inventoryCounts(), [client, ctx.tick]);
  const detail = useAsync(
    () =>
      selectedId
        ? client.inventoryCount(selectedId)
        : Promise.resolve(null as InvCount | null),
    [client, ctx.tick, selectedId],
  );

  const lineKey = (line: InvCount["lines"][number], i: number) =>
    `${line.sku}|${line.lot_number ?? ""}|${line.serial ?? ""}|${i}`;

  const countedValue = (line: InvCount["lines"][number], i: number): string => {
    const key = lineKey(line, i);
    if (key in counted) return counted[key];
    if (line.counted !== null && line.counted !== undefined) return String(line.counted);
    if (line.expected !== undefined) return String(line.expected);
    return "";
  };

  const variances = (detail.data?.lines ?? []).map((line, i) => {
    const c = Number(countedValue(line, i));
    const expected = line.expected ?? 0;
    return Number.isFinite(c) ? c - expected : 0;
  });
  const hasVariance = variances.some((v) => v !== 0);
  const isOpen = detail.data != null && detail.data.status !== "completed" && detail.data.status !== "cancelled";

  async function startCount() {
    if (!startLocation) {
      setError("Choose a location to count.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await client.startInventoryCount(startLocation);
      setNotice(`Count started for ${startLocation}.`);
      setSelectedId(countId(created));
      setCounted({});
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function completeCount() {
    if (!detail.data || !selectedId) return;
    if (hasVariance && !reasonCode) {
      setError("Variances are present — choose a reason code from the taxonomy before completing.");
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await client.completeInventoryCount(selectedId, {
        lines: detail.data.lines.map((line, i) => ({
          sku: line.sku,
          lot_number: line.lot_number ?? null,
          serial: line.serial ?? null,
          counted: Number(countedValue(line, i)) || 0,
        })),
        reason_code: hasVariance ? reasonCode : undefined,
      });
      setNotice(`Count ${selectedId} completed${hasVariance ? " with variances recorded" : ""}.`);
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function cancelCount(id: string) {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await client.cancelInventoryCount(id);
      setNotice(`Count ${id} cancelled.`);
      if (selectedId === id) setSelectedId(null);
      ctx.bump();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>Cycle counts</h3>
      {canManage ? (
        <div className="inline-form" aria-label="Start a count">
          <LocationSelect
            label="Count location"
            value={startLocation}
            onChange={setStartLocation}
            locations={ctx.locations}
            locationsReady={ctx.locationsReady}
          />
          <button type="button" disabled={busy} onClick={() => void startCount()}>
            Start count
          </button>
        </div>
      ) : (
        <p className="hint">Starting and completing counts needs the inventory.manage capability — your role is read-only.</p>
      )}
      {error && <p className="error-note" role="alert">{error}</p>}
      {notice && <p className="state state-empty" role="status">{notice}</p>}

      {list.loading ? (
        <Loading label="Loading counts…" />
      ) : list.error ? (
        <ErrorView error={list.error} />
      ) : !list.data || list.data.length === 0 ? (
        <Empty label="No counts yet." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Count</th><th scope="col">Location</th>
              <th scope="col">Status</th><th scope="col">Lines</th>
              <th scope="col">Started</th><th scope="col">Actions</th>
            </tr>
          </thead>
          <tbody>
            {list.data.map((count) => {
              const id = countId(count);
              return (
                <tr key={id}>
                  <td>{id || "—"}</td>
                  <td>{count.location_code}</td>
                  <td>
                    <StatusBadge tone={count.status === "completed" ? "ok" : count.status === "cancelled" ? "bad" : "warn"}>
                      {count.status}
                    </StatusBadge>
                  </td>
                  <td className="num">{count.lines?.length ?? 0}</td>
                  <td>{count.created_at ? count.created_at.slice(0, 19).replace("T", " ") : "—"}</td>
                  <td>
                    <span className="appt-actions">
                      <button type="button" onClick={() => { setSelectedId(id); setCounted({}); setReasonCode(""); }}>
                        View count {id}
                      </button>
                      {canManage && count.status !== "completed" && count.status !== "cancelled" && (
                        <button type="button" disabled={busy} onClick={() => void cancelCount(id)}>
                          Cancel count {id}
                        </button>
                      )}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      {selectedId && (
        <section aria-label="Count detail">
          <h4>Count {selectedId} — expected vs counted</h4>
          {detail.loading ? (
            <Loading label="Loading count…" />
          ) : detail.error ? (
            <ErrorView error={detail.error} />
          ) : !detail.data ? (
            <Empty label="Count not found." />
          ) : detail.data.lines.length === 0 ? (
            <Empty label="This count has no expected lines (no stock at the location)." />
          ) : (
            <>
              <table className="data-table">
                <thead>
                  <tr>
                    <th scope="col">SKU</th><th scope="col">Item</th>
                    <th scope="col">Lot</th><th scope="col">Serial</th>
                    <th scope="col">Expected</th><th scope="col">Counted</th>
                    <th scope="col">Variance</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.data.lines.map((line, i) => {
                    const variance = variances[i] ?? 0;
                    return (
                      <tr key={lineKey(line, i)}>
                        <td>{line.sku}</td>
                        <td>{line.name ?? nameFor(ctx, line.sku)}</td>
                        <td>{line.lot_number ?? "—"}</td>
                        <td>{line.serial ?? "—"}</td>
                        <td className="num">{line.expected ?? "—"}</td>
                        <td>
                          <input
                            aria-label={`Counted quantity for ${line.sku}`}
                            type="number"
                            min="0"
                            disabled={!canManage || !isOpen}
                            value={countedValue(line, i)}
                            onChange={(e) =>
                              setCounted((m) => ({ ...m, [lineKey(line, i)]: e.target.value }))
                            }
                          />
                        </td>
                        <td>
                          {variance === 0 ? (
                            <StatusBadge tone="ok">0</StatusBadge>
                          ) : (
                            <StatusBadge tone="bad">{variance > 0 ? `+${variance}` : variance}</StatusBadge>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              {canManage && isOpen && (
                <form
                  className="inline-form"
                  aria-label="Complete count"
                  noValidate
                  onSubmit={(e) => {
                    e.preventDefault();
                    void completeCount();
                  }}
                >
                  {hasVariance && (
                    <label className="field">
                      Variance reason *
                      <select
                        aria-label="Count variance reason"
                        value={reasonCode}
                        disabled={!taxonomy}
                        onChange={(e) => setReasonCode(e.target.value)}
                      >
                        <option value="">Select a reason</option>
                        {(taxonomy?.reasonCodes ?? []).map((r) => (
                          <option key={r.code} value={r.code}>{r.label}</option>
                        ))}
                      </select>
                      {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
                    </label>
                  )}
                  <button type="submit" disabled={busy}>Complete count</button>
                </form>
              )}
            </>
          )}
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Worklists tab: low stock, expiry, implant trace.
// ---------------------------------------------------------------------------

function WorklistsPanel({ ctx }: { ctx: Ctx }) {
  const { client, taxonomy } = ctx;
  const [lowLocation, setLowLocation] = useState("");
  const [lowCategory, setLowCategory] = useState("");
  const [expLocation, setExpLocation] = useState("");
  const [expCategory, setExpCategory] = useState("");
  const [traceForm, setTraceForm] = useState({ lot_number: "", patient_ref: "", sku: "" });
  const [traceError, setTraceError] = useState<string | null>(null);
  const [traceRows, setTraceRows] = useState<Awaited<ReturnType<ApiClient["inventoryImplantTrace"]>> | null>(null);
  const [traceBusy, setTraceBusy] = useState(false);

  const lowStock = useAsync(
    () =>
      client.inventoryLowStock({
        location_code: lowLocation || undefined,
        category: lowCategory || undefined,
      }),
    [client, ctx.tick, lowLocation, lowCategory],
  );
  const expiry = useAsync(
    () =>
      client.inventoryExpiry({
        location_code: expLocation || undefined,
        category: expCategory || undefined,
      }),
    [client, ctx.tick, expLocation, expCategory],
  );

  const warningDays = taxonomy?.expiryWarningDays ?? 90;

  async function runTrace() {
    if (!traceForm.lot_number.trim() && !traceForm.patient_ref.trim() && !traceForm.sku.trim()) {
      setTraceError("Enter a lot number, a patient reference or a SKU to trace.");
      return;
    }
    setTraceBusy(true);
    setTraceError(null);
    setTraceRows(null);
    try {
      const rows = await client.inventoryImplantTrace({
        lot_number: traceForm.lot_number.trim() || undefined,
        patient_ref: traceForm.patient_ref.trim() || undefined,
        sku: traceForm.sku.trim() || undefined,
      });
      setTraceRows(rows);
    } catch (err) {
      setTraceError(errorText(err));
    } finally {
      setTraceBusy(false);
    }
  }

  const categorySelect = (
    label: string,
    value: string,
    onChange: (v: string) => void,
  ) => (
    <label className="field">
      Category
      <select aria-label={label} value={value} disabled={!taxonomy} onChange={(e) => onChange(e.target.value)}>
        <option value="">All categories</option>
        {(taxonomy?.categories ?? []).map((c) => (
          <option key={c.code} value={c.code}>{c.label}</option>
        ))}
      </select>
      {!taxonomy && <span className="field-error">Loading taxonomy…</span>}
    </label>
  );

  return (
    <div>
      <h3>Low stock — below PAR</h3>
      <div className="inline-form" aria-label="Low stock filters">
        <LocationSelect label="Low-stock location filter" value={lowLocation} onChange={setLowLocation} locations={ctx.locations} locationsReady={ctx.locationsReady} allowAll />
        {categorySelect("Low-stock category filter", lowCategory, setLowCategory)}
      </div>
      {lowStock.loading ? (
        <Loading label="Loading low-stock worklist…" />
      ) : lowStock.error ? (
        <ErrorView error={lowStock.error} />
      ) : !lowStock.data || lowStock.data.length === 0 ? (
        <Empty label="Nothing is below PAR at the selected locations." icon="boxes" />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">SKU</th><th scope="col">Item</th>
              <th scope="col">Location</th><th scope="col">On hand</th>
              <th scope="col">PAR</th><th scope="col">Deficit</th>
            </tr>
          </thead>
          <tbody>
            {lowStock.data.map((row) => (
              <tr key={`${row.sku}-${row.location_code}`}>
                <td>{row.sku}</td>
                <td>{row.name}</td>
                <td>{row.location_code}</td>
                <td className="num">{row.on_hand}</td>
                <td className="num">{row.par_level}</td>
                <td>
                  <StatusBadge tone="warn">deficit {row.deficit}</StatusBadge>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Expiry — within {warningDays} days or expired</h3>
      <div className="inline-form" aria-label="Expiry filters">
        <LocationSelect label="Expiry location filter" value={expLocation} onChange={setExpLocation} locations={ctx.locations} locationsReady={ctx.locationsReady} allowAll />
        {categorySelect("Expiry category filter", expCategory, setExpCategory)}
      </div>
      {expiry.loading ? (
        <Loading label="Loading expiry worklist…" />
      ) : expiry.error ? (
        <ErrorView error={expiry.error} />
      ) : !expiry.data || expiry.data.length === 0 ? (
        <Empty label="No lots are expired or inside the expiry warning window." />
      ) : (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">SKU</th><th scope="col">Item</th>
              <th scope="col">Location</th><th scope="col">Lot</th>
              <th scope="col">Expiry</th><th scope="col">Quantity</th>
              <th scope="col">Days left</th><th scope="col">Status</th>
            </tr>
          </thead>
          <tbody>
            {expiry.data.map((row, i) => (
              <tr key={`${row.sku}-${row.location_code}-${row.lot_number ?? ""}-${i}`}>
                <td>{row.sku}</td>
                <td>{row.name}</td>
                <td>{row.location_code}</td>
                <td>{row.lot_number ?? "—"}</td>
                <td>{row.expiry_date ?? "—"}</td>
                <td className="num">{row.quantity}</td>
                <td className="num">{row.days_to_expiry ?? "—"}</td>
                <td>
                  {row.expired ? (
                    <StatusBadge tone="bad">expired</StatusBadge>
                  ) : (
                    <StatusBadge tone="warn">expiring</StatusBadge>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Implant trace</h3>
      <p className="hint">
        Trace in either direction: a lot number shows which patient and
        case it reached; a patient reference shows every traced lot
        they received.
      </p>
      <form
        className="inline-form"
        aria-label="Implant trace lookup"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void runTrace();
        }}
      >
        <label className="field">
          Lot number
          <input aria-label="Trace lot number" value={traceForm.lot_number} onChange={(e) => setTraceForm((f) => ({ ...f, lot_number: e.target.value }))} placeholder="LOT-…" />
        </label>
        <label className="field">
          Patient reference
          <input aria-label="Trace patient reference" value={traceForm.patient_ref} onChange={(e) => setTraceForm((f) => ({ ...f, patient_ref: e.target.value }))} placeholder="Patient/…" />
        </label>
        <label className="field">
          SKU
          <input aria-label="Trace SKU" value={traceForm.sku} onChange={(e) => setTraceForm((f) => ({ ...f, sku: e.target.value }))} />
        </label>
        <button type="submit" disabled={traceBusy}>Trace</button>
        {traceError && <p className="error-note" role="alert">{traceError}</p>}
      </form>
      {traceRows !== null &&
        (traceRows.length === 0 ? (
          <Empty label="No implant trace entries match that lookup." />
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th scope="col">SKU</th><th scope="col">Lot</th>
                <th scope="col">Serial</th><th scope="col">Patient</th>
                <th scope="col">Case</th><th scope="col">Location</th>
                <th scope="col">Event</th><th scope="col">Time</th>
              </tr>
            </thead>
            <tbody>
              {traceRows.map((row, i) => (
                <tr key={`${row.sku ?? ""}-${row.lot_number ?? ""}-${row.patient_ref ?? ""}-${i}`}>
                  <td>{row.sku ?? "—"}</td>
                  <td>{row.lot_number ?? "—"}</td>
                  <td>{row.serial ?? "—"}</td>
                  <td>{row.patient_ref ?? "—"}</td>
                  <td>{row.case_id ?? "—"}</td>
                  <td>{row.location_code ?? "—"}</td>
                  <td>{row.event ?? "—"}</td>
                  <td>{row.created_at ? String(row.created_at).slice(0, 19).replace("T", " ") : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ))}
    </div>
  );
}
