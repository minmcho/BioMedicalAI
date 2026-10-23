import { useState } from "react";
import type { ApiClient } from "../../api/client";
import type { Role } from "../../api/types";
import { useRuntimeConfig } from "../../auth/runtimeConfig";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { errorText } from "./shared";

interface Props {
  client: ApiClient;
  role: Role;
}

interface IngredientRow {
  name: string;
  quantity: string;
  lotNumber: string;
}

/** Sterile compounding / IV room tab (USP <797> as config): batch
 *  records with per-ingredient lot capture; the beyond-use datetime is
 *  computed by the server from the published category configuration —
 *  the preparer never types it. Release needs a verifier who is not
 *  the preparer. This queue is separate from order verification. */
export function CompoundingTab({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canCompound = permissions["pharmacy.compound"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [prepName, setPrepName] = useState("");
  const [category, setCategory] = useState("");
  const [storage, setStorage] = useState("refrigerated");
  const [patientRef, setPatientRef] = useState("");
  const [ingredients, setIngredients] = useState<IngredientRow[]>([
    { name: "", quantity: "", lotNumber: "" },
  ]);

  const queueState = useAsync(() => client.compoundingQueue(), [client, tick]);
  const batches = queueState.data?.items ?? [];
  const categories = queueState.data?.categories ?? {};
  const categoryCodes = Object.keys(categories);
  const selectedCategory = category || categoryCodes[0] || "";
  const budHours =
    categories[selectedCategory]?.budHours?.[storage] ?? undefined;

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

  return (
    <div>
      <h3>Sterile compounding — IV room</h3>
      <p className="hint">
        Beyond-use dating comes from the published compounding category
        configuration (USP &lt;797&gt; parameters the site has adopted),
        computed at batch creation. Ingredient lots are captured on the
        batch record; a second person verifies before release.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      {canCompound && (
        <form
          className="toolbar"
          onSubmit={(e) => {
            e.preventDefault();
            void perform("Create batch", async () => {
              const batch = await client.createCompoundBatch({
                preparationName: prepName.trim(),
                category: selectedCategory,
                storage,
                ...(patientRef.trim()
                  ? { patientRef: patientRef.trim() }
                  : {}),
                ingredients: ingredients.map((i) => ({
                  name: i.name.trim(),
                  quantity: i.quantity.trim(),
                  lotNumber: i.lotNumber.trim(),
                })),
              });
              setNotice(
                `Batch ${batch.batch_id} created — beyond-use ${batch.beyond_use_at ?? "—"} (from category config).`,
              );
            });
          }}
        >
          <strong>New batch</strong>
          <label>
            Preparation{" "}
            <input
              aria-label="Preparation name"
              value={prepName}
              onChange={(e) => setPrepName(e.target.value)}
            />
          </label>
          <label>
            Category{" "}
            <select
              aria-label="Compounding category"
              value={selectedCategory}
              onChange={(e) => setCategory(e.target.value)}
            >
              {categoryCodes.map((code) => (
                <option key={code} value={code}>
                  {categories[code].label ?? code}
                </option>
              ))}
            </select>
          </label>
          <label>
            Storage{" "}
            <select
              aria-label="Storage condition"
              value={storage}
              onChange={(e) => setStorage(e.target.value)}
            >
              <option value="room">Room temperature</option>
              <option value="refrigerated">Refrigerated</option>
              <option value="frozen">Frozen</option>
            </select>
          </label>
          {budHours !== undefined && (
            <span className="hint">BUD from config: {budHours} h</span>
          )}
          <label>
            Patient ref (optional){" "}
            <input
              aria-label="Batch patient ref"
              value={patientRef}
              onChange={(e) => setPatientRef(e.target.value)}
              placeholder="Patient/pat-1"
            />
          </label>
          {ingredients.map((row, i) => (
            <span key={i}>
              <label>
                Ingredient {i + 1} name{" "}
                <input
                  aria-label={`Ingredient ${i + 1} name`}
                  value={row.name}
                  onChange={(e) =>
                    setIngredients((rows) =>
                      rows.map((r, j) =>
                        j === i ? { ...r, name: e.target.value } : r,
                      ),
                    )}
                />
              </label>
              <label>
                Qty{" "}
                <input
                  aria-label={`Ingredient ${i + 1} quantity`}
                  value={row.quantity}
                  onChange={(e) =>
                    setIngredients((rows) =>
                      rows.map((r, j) =>
                        j === i ? { ...r, quantity: e.target.value } : r,
                      ),
                    )}
                  style={{ width: "5rem" }}
                />
              </label>
              <label>
                Lot{" "}
                <input
                  aria-label={`Ingredient ${i + 1} lot`}
                  value={row.lotNumber}
                  onChange={(e) =>
                    setIngredients((rows) =>
                      rows.map((r, j) =>
                        j === i ? { ...r, lotNumber: e.target.value } : r,
                      ),
                    )}
                />
              </label>
            </span>
          ))}
          <button
            type="button"
            onClick={() =>
              setIngredients((rows) => [
                ...rows,
                { name: "", quantity: "", lotNumber: "" },
              ])}
          >
            Add ingredient
          </button>
          <button
            type="submit"
            disabled={
              busy ||
              !prepName.trim() ||
              !selectedCategory ||
              ingredients.some((i) => !i.lotNumber.trim())
            }
          >
            Create batch
          </button>
        </form>
      )}

      <h4>Compounding queue</h4>
      {queueState.loading && <Loading label="Loading compounding queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && batches.length === 0 && (
        <Empty label="No compounding batches yet." />
      )}
      {batches.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Batch</th>
              <th>Preparation</th>
              <th>Category / storage</th>
              <th>Compounded</th>
              <th>Beyond-use</th>
              <th>Ingredients (lots)</th>
              <th>Preparer</th>
              <th>Status</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {batches.map((b) => (
              <tr key={b.batch_id}>
                <td>{b.batch_id}</td>
                <td>{b.preparation_name}</td>
                <td>
                  {b.category} / {b.storage}
                </td>
                <td>{b.compounded_at?.replace("T", " ").slice(0, 16) ?? "—"}</td>
                <td>{b.beyond_use_at?.replace("T", " ").slice(0, 16) ?? "—"}</td>
                <td>
                  {b.ingredients
                    .map((i) => `${i.name ?? i.medication_code ?? "?"} [${i.lot_number}]`)
                    .join("; ")}
                </td>
                <td>
                  {b.preparer}
                  {b.verifier ? ` · verified by ${b.verifier}` : ""}
                </td>
                <td>{b.status}</td>
                <td>
                  {canCompound && b.status !== "verified" && (
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() =>
                        void perform("Verify batch", () =>
                          client.verifyCompoundBatch(b.batch_id),
                        )}
                    >
                      Verify
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
