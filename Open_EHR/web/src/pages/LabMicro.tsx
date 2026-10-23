import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  acknowledgeGramStain, addIsolate, getAntibiogram, getMicroDepthConfig,
  getMicroWorkup, notifyGramStain, recordAst, recordColony,
  recordGramStain, recordPlating, removeIsolate,
  type AntibiogramItem, type MicroDepthConfig, type MicroOption,
  type MicroWorkup,
} from "../api/labMicro";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

// LAB-D12 — microbiology workup depth surface (app.lab_micro).
// Mounted by the coordinator as a Lab page section (see the W6
// INTEGRATION NOTE). Every dropdown is fed by the governed config
// endpoint (published organisms / antibiotics / breakpoint tables /
// workup vocabularies); a control whose governed source is empty is
// disabled WITH the reason shown — no dead controls, no free-text
// where the server validates an enum.

function errorText(err: unknown): string {
  if (err instanceof ApiError) {
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  }
  return err instanceof Error ? err.message : String(err);
}

function Options({ options }: { options: MicroOption[] }) {
  return (
    <>
      {options.map((o) => (
        <option key={o.code} value={o.code}>{o.display}</option>
      ))}
    </>
  );
}

export function LabMicro({ client }: Props) {
  const configState = useAsync<MicroDepthConfig>(
    () => getMicroDepthConfig(client), [client]);
  const [orderId, setOrderId] = useState("");
  const [loadedOrder, setLoadedOrder] = useState("");
  const [view, setView] = useState<"lab" | "released">("lab");
  const [tick, setTick] = useState(0);
  const workupState = useAsync<{ workup: MicroWorkup; order_status: string } | null>(
    async () => (loadedOrder
      ? getMicroWorkup(client, loadedOrder, view) : null),
    [client, loadedOrder, view, tick]);
  const [error, setError] = useState<string | null>(null);
  const [antibiogram, setAntibiogram] = useState<AntibiogramItem[] | null>(null);

  // Plating form state
  const [media, setMedia] = useState<string[]>([]);
  const [streak, setStreak] = useState("");
  const [temperature, setTemperature] = useState("35");
  const [atmosphere, setAtmosphere] = useState("");
  const [duration, setDuration] = useState("24");
  const [cultureType, setCultureType] = useState("");
  // Gram stain state
  const [morphology, setMorphology] = useState("");
  const [quantity, setQuantity] = useState("");
  const [wbc, setWbc] = useState("");
  const [epithelial, setEpithelial] = useState("");
  const [adequacy, setAdequacy] = useState("");
  const [notifiedTo, setNotifiedTo] = useState("");
  const [readBack, setReadBack] = useState(false);
  // Colony state
  const [cfu, setCfu] = useState("");
  const [growth, setGrowth] = useState("");
  const [bottleSet, setBottleSet] = useState("");
  const [bottleType, setBottleType] = useState("");
  const [ttp, setTtp] = useState("");
  // Isolate + AST state
  const [organism, setOrganism] = useState("");
  const [idMethod, setIdMethod] = useState("");
  const [idConfidence, setIdConfidence] = useState("");
  const [astForms, setAstForms] = useState<Record<string, {
    antibiotic: string; mic: string; zone: string; standard: string;
    version: string;
  }>>({});

  if (configState.loading) return <Loading label="Loading microbiology config…" />;
  if (configState.error) return <ErrorView error={configState.error} />;
  const config = configState.data;
  if (!config) return <Empty label="No microbiology configuration available." />;
  const vocab = config.vocab;
  const workup = workupState.data?.workup ?? null;
  const orderStatus = workupState.data?.order_status ?? null;

  const run = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      setTick((t) => t + 1);
    } catch (err) {
      setError(errorText(err));
    }
  };

  const selectedMethod = vocab.idMethods.find((m) => m.code === idMethod);
  const breakpointVersions = (standard: string) =>
    config.breakpoint_tables.filter((t) => t.standard === standard);

  return (
    <section aria-label="Microbiology workup">
      <h2>Microbiology workup (LAB-D12)</h2>
      {error && <p role="alert">{error}</p>}

      <div>
        <label>
          Culture order ID{" "}
          <input aria-label="Culture order ID" value={orderId}
                 onChange={(e) => setOrderId(e.target.value)} />
        </label>
        <button type="button" disabled={!orderId.trim()}
                onClick={() => setLoadedOrder(orderId.trim())}>
          Load workup
        </button>
        {!orderId.trim() && <span> Enter an order ID to load its workup.</span>}
        <span> Report view: </span>
        <button type="button" aria-pressed={view === "lab"}
                onClick={() => setView("lab")}>Lab (full set)</button>
        <button type="button" aria-pressed={view === "released"}
                onClick={() => setView("released")}>Released set</button>
      </div>

      {loadedOrder && workupState.loading && <Loading label="Loading workup…" />}
      {loadedOrder && workupState.error ? (
        <ErrorView error={workupState.error} />) : null}
      {loadedOrder && workup && (
        <div>
          <p>
            Order {workup.order_id} — status {orderStatus}; patient{" "}
            {workup.patient_ref ?? "—"}; culture type{" "}
            {workup.culture_type ?? "not recorded"}
            {workup.finalized_at && <>; finalised {workup.finalized_at}</>}
          </p>
          {workup.mdro_alerts.length > 0 && (
            <p role="status">
              MDRO alerts filed with Infection Prevention:{" "}
              {workup.mdro_alerts.map((a) => `${a.organism} → ${a.kind} ${a.ref}`)
                .join("; ")}
            </p>
          )}

          <h3>Plating record</h3>
          {workup.plating ? (
            <p>
              Media: {workup.plating.media.join(", ")}; streak{" "}
              {workup.plating.streak_pattern}; incubation{" "}
              {workup.plating.incubation.temperature_c}°C,{" "}
              {workup.plating.incubation.atmosphere},{" "}
              {workup.plating.incubation.duration_hours} h; started{" "}
              {workup.plating.started_at} by {workup.plating.recorded_by}.
            </p>
          ) : (
            <p>No plating record — the workup cannot advance past
               plating until one is recorded.</p>
          )}
          <fieldset>
            <legend>Record plating</legend>
            <label>
              Media{" "}
              <select aria-label="Media" multiple value={media}
                      onChange={(e) => setMedia(
                        Array.from(e.target.selectedOptions,
                                   (o) => o.value))}>
                <Options options={vocab.media} />
              </select>
            </label>
            <label>
              Streak pattern{" "}
              <select aria-label="Streak pattern" value={streak}
                      onChange={(e) => setStreak(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.streakPatterns} />
              </select>
            </label>
            <label>
              Temperature °C{" "}
              <input aria-label="Incubation temperature" type="number"
                     value={temperature}
                     onChange={(e) => setTemperature(e.target.value)} />
            </label>
            <label>
              Atmosphere{" "}
              <select aria-label="Incubation atmosphere" value={atmosphere}
                      onChange={(e) => setAtmosphere(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.atmospheres} />
              </select>
            </label>
            <label>
              Duration hours{" "}
              <input aria-label="Incubation duration" type="number"
                     value={duration}
                     onChange={(e) => setDuration(e.target.value)} />
            </label>
            <label>
              Culture type{" "}
              <select aria-label="Culture type" value={cultureType}
                      onChange={(e) => setCultureType(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.cultureTypes} />
              </select>
            </label>
            <button type="button"
                    disabled={!media.length || !streak || !atmosphere}
                    onClick={() => run(() => recordPlating(client, loadedOrder, {
                      media, streak_pattern: streak,
                      incubation: { temperature_c: Number(temperature),
                                    atmosphere,
                                    duration_hours: Number(duration) },
                      culture_type: cultureType || undefined,
                    }))}>
              Save plating record
            </button>
            {(!media.length || !streak || !atmosphere) && (
              <span> Media, streak pattern and atmosphere are required.</span>
            )}
          </fieldset>

          <h3>Structured Gram stain</h3>
          {workup.gram_stain ? (
            <p>
              {workup.gram_stain.morphology}, {workup.gram_stain.quantity};
              WBC {workup.gram_stain.wbc}; epithelial{" "}
              {workup.gram_stain.epithelial_cells}; adequacy{" "}
              {workup.gram_stain.adequacy}.
            </p>
          ) : <p>No Gram stain recorded.</p>}
          <fieldset>
            <legend>Record Gram stain</legend>
            <label>
              Morphology{" "}
              <select aria-label="Gram morphology" value={morphology}
                      onChange={(e) => setMorphology(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.gramMorphologies} />
              </select>
            </label>
            <label>
              Quantity{" "}
              <select aria-label="Gram quantity" value={quantity}
                      onChange={(e) => setQuantity(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.quantities} />
              </select>
            </label>
            <label>
              WBC{" "}
              <select aria-label="Gram WBC" value={wbc}
                      onChange={(e) => setWbc(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.cellBands} />
              </select>
            </label>
            <label>
              Epithelial cells{" "}
              <select aria-label="Gram epithelial cells" value={epithelial}
                      onChange={(e) => setEpithelial(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.cellBands} />
              </select>
            </label>
            <label>
              Adequacy{" "}
              <select aria-label="Gram adequacy" value={adequacy}
                      onChange={(e) => setAdequacy(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.adequacyValues} />
              </select>
            </label>
            <button type="button"
                    disabled={!workup.plating || !morphology || !quantity ||
                              !wbc || !epithelial || !adequacy}
                    onClick={() => run(() => recordGramStain(client, loadedOrder, {
                      morphology, quantity, wbc,
                      epithelial_cells: epithelial, adequacy,
                    }))}>
              Save Gram stain
            </button>
            {!workup.plating && (
              <span> Disabled: a plating record is required first.</span>
            )}
          </fieldset>

          {workup.critical_call && (
            <fieldset>
              <legend>Critical call — positive blood-culture Gram stain</legend>
              <p>State: {workup.critical_call.state}
                {workup.critical_call.notified_to &&
                  <> — notified {workup.critical_call.notified_to}
                    {workup.critical_call.read_back ? " (read back)" : ""}</>}
              </p>
              {workup.critical_call.state === "pending" && view === "lab" && (
                <>
                  <label>
                    Notified to{" "}
                    <input aria-label="Notified to" value={notifiedTo}
                           onChange={(e) => setNotifiedTo(e.target.value)} />
                  </label>
                  <label>
                    <input aria-label="Read back" type="checkbox"
                           checked={readBack}
                           onChange={(e) => setReadBack(e.target.checked)} />
                    Receiver read the result back
                  </label>
                  <button type="button" disabled={!notifiedTo.trim() || !readBack}
                          onClick={() => run(() => notifyGramStain(
                            client, loadedOrder,
                            { notified_to: notifiedTo, read_back: readBack }))}>
                    Record critical call
                  </button>
                  {(!notifiedTo.trim() || !readBack) && (
                    <span> A named receiver and read-back are both required.</span>
                  )}
                </>
              )}
              {workup.critical_call.state === "notified" && (
                <button type="button"
                        onClick={() => run(() => acknowledgeGramStain(
                          client, loadedOrder))}>
                  Acknowledge critical call
                </button>
              )}
            </fieldset>
          )}

          <h3>Colony workup</h3>
          {workup.colony && (
            <p>
              {workup.colony.cfu_ml !== undefined &&
                <>Quantitation {workup.colony.cfu_ml} CFU/mL
                  ({workup.colony.cfu_band}). </>}
              {workup.colony.growth_amount &&
                <>Growth {workup.colony.growth_amount}. </>}
              {workup.colony.bottle &&
                <>Bottle set {workup.colony.bottle.set_id} (
                  {workup.colony.bottle.bottle_type}), time to positivity{" "}
                  {workup.colony.bottle.time_to_positivity_hours} h.</>}
            </p>
          )}
          <fieldset>
            <legend>Record colony workup</legend>
            <label>
              CFU/mL (urine){" "}
              <input aria-label="CFU per mL" type="number" value={cfu}
                     onChange={(e) => setCfu(e.target.value)} />
            </label>
            <label>
              Growth amount{" "}
              <select aria-label="Growth amount" value={growth}
                      onChange={(e) => setGrowth(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.quantities} />
              </select>
            </label>
            <label>
              Bottle set ID{" "}
              <input aria-label="Bottle set ID" value={bottleSet}
                     onChange={(e) => setBottleSet(e.target.value)} />
            </label>
            <label>
              Bottle type{" "}
              <select aria-label="Bottle type" value={bottleType}
                      onChange={(e) => setBottleType(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.bottleTypes} />
              </select>
            </label>
            <label>
              Time to positivity (h){" "}
              <input aria-label="Time to positivity" type="number" value={ttp}
                     onChange={(e) => setTtp(e.target.value)} />
            </label>
            <button type="button" disabled={!cfu && !growth}
                    onClick={() => run(() => recordColony(client, loadedOrder, {
                      cfu_ml: cfu ? Number(cfu) : undefined,
                      growth_amount: growth || undefined,
                      bottle: bottleSet
                        ? { set_id: bottleSet, bottle_type: bottleType,
                            time_to_positivity_hours: Number(ttp) }
                        : undefined,
                    }))}>
              Save colony workup
            </button>
            {!cfu && !growth && (
              <span> Quantitation (CFU/mL or growth amount) is required.</span>
            )}
          </fieldset>

          <h3>Isolates</h3>
          {workup.isolates.length === 0 && (
            <Empty label="No isolates recorded for this culture." />)}
          {workup.isolates.map((isolate) => {
            const form = astForms[isolate.isolate_id] ??
              { antibiotic: "", mic: "", zone: "", standard: "CLSI",
                version: "" };
            const setForm = (patch: Partial<typeof form>) =>
              setAstForms((prev) => ({
                ...prev,
                [isolate.isolate_id]: { ...form, ...patch },
              }));
            const versions = breakpointVersions(form.standard);
            return (
              <div key={isolate.isolate_id}>
                <p>
                  <strong>{isolate.organism_display ?? isolate.organism_code}</strong>
                  {" "}— ID by {isolate.id_method}
                  {isolate.id_confidence !== null &&
                    <> (confidence {isolate.id_confidence})</>}
                  {isolate.id_below_threshold &&
                    <> — BELOW the governed validity threshold</>}
                  <button type="button"
                          onClick={() => run(() => removeIsolate(
                            client, loadedOrder, isolate.isolate_id))}>
                    Remove isolate
                  </button>
                </p>
                {isolate.ast.length > 0 && (
                  <table>
                    <thead>
                      <tr><th>Antibiotic</th><th>MIC</th><th>Zone</th>
                        <th>Standard</th><th>Computed</th><th>Final</th>
                        <th>Expert rules / suppression</th></tr>
                    </thead>
                    <tbody>
                      {isolate.ast.map((entry, i) => (
                        <tr key={i}>
                          <td>{entry.antibiotic_display ?? entry.antibiotic}</td>
                          <td>{entry.mic ?? "—"}</td>
                          <td>{entry.zone_mm ?? "—"}</td>
                          <td>{entry.standard ?? ""} {entry.version ?? ""}</td>
                          <td>{entry.computed_interpretation ??
                            (entry.suppressed ? "—" :
                              (entry.interpretation_note ?? "not computed"))}</td>
                          <td>{entry.final_interpretation ?? "—"}</td>
                          <td>
                            {(entry.expert_traces ?? []).map((t) => (
                              <span key={t.rule_code}>
                                {t.rule_code}: {t.from_interpretation} →{" "}
                                {t.to_interpretation} ({t.reason}){" "}
                              </span>
                            ))}
                            {entry.suppressed &&
                              <span>Suppressed from the released set:{" "}
                                {entry.suppression_reason}</span>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <fieldset>
                  <legend>Add AST for {isolate.organism_code}</legend>
                  <label>
                    Antibiotic{" "}
                    <select aria-label={`AST antibiotic ${isolate.organism_code}`}
                            value={form.antibiotic}
                            onChange={(e) => setForm({ antibiotic: e.target.value })}>
                      <option value="">Select…</option>
                      <Options options={config.antibiotics} />
                    </select>
                  </label>
                  <label>
                    MIC mg/L{" "}
                    <input aria-label={`AST MIC ${isolate.organism_code}`}
                           type="number" value={form.mic}
                           onChange={(e) => setForm({ mic: e.target.value })} />
                  </label>
                  <label>
                    Zone mm{" "}
                    <input aria-label={`AST zone ${isolate.organism_code}`}
                           type="number" value={form.zone}
                           onChange={(e) => setForm({ zone: e.target.value })} />
                  </label>
                  <label>
                    Standard{" "}
                    <select aria-label={`AST standard ${isolate.organism_code}`}
                            value={form.standard}
                            onChange={(e) => setForm(
                              { standard: e.target.value, version: "" })}>
                      <option value="CLSI">CLSI</option>
                      <option value="EUCAST">EUCAST</option>
                    </select>
                  </label>
                  <label>
                    Version{" "}
                    {versions.length > 0 ? (
                      <select aria-label={`AST version ${isolate.organism_code}`}
                              value={form.version}
                              onChange={(e) => setForm({ version: e.target.value })}>
                        <option value="">Select…</option>
                        {versions.map((t) => (
                          <option key={t.version} value={t.version}>
                            {t.version}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <input aria-label={`AST version ${isolate.organism_code}`}
                             value={form.version}
                             onChange={(e) => setForm({ version: e.target.value })} />
                    )}
                  </label>
                  {versions.length === 0 && (
                    <span> No published {form.standard} breakpoint tables —
                      the entry will be stored without a computed
                      interpretation.</span>
                  )}
                  <button type="button"
                          disabled={!form.antibiotic || !form.version ||
                                    (!form.mic && !form.zone)}
                          onClick={() => run(() => recordAst(
                            client, loadedOrder, isolate.isolate_id, {
                              results: [{
                                antibiotic: form.antibiotic,
                                mic: form.mic ? Number(form.mic) : undefined,
                                zone_mm: form.zone ? Number(form.zone) : undefined,
                                standard: form.standard,
                                version: form.version,
                              }],
                            }))}>
                    Save AST entry
                  </button>
                </fieldset>
              </div>
            );
          })}
          <fieldset>
            <legend>Add isolate</legend>
            <label>
              Organism{" "}
              <select aria-label="Isolate organism" value={organism}
                      disabled={!config.organisms.length}
                      onChange={(e) => setOrganism(e.target.value)}>
                <option value="">Select…</option>
                <Options options={config.organisms} />
              </select>
            </label>
            {!config.organisms.length && (
              <span> Disabled: no published organism terminology.</span>
            )}
            <label>
              ID method{" "}
              <select aria-label="ID method" value={idMethod}
                      onChange={(e) => setIdMethod(e.target.value)}>
                <option value="">Select…</option>
                <Options options={vocab.idMethods} />
              </select>
            </label>
            <label>
              ID confidence{" "}
              <input aria-label="ID confidence" type="number"
                     value={idConfidence}
                     disabled={!selectedMethod?.carriesConfidence}
                     onChange={(e) => setIdConfidence(e.target.value)} />
            </label>
            {selectedMethod && !selectedMethod.carriesConfidence && (
              <span> Disabled: {selectedMethod.display} does not carry a
                confidence score.</span>
            )}
            <button type="button"
                    disabled={!organism || !idMethod ||
                              (!!selectedMethod?.carriesConfidence &&
                               !idConfidence)}
                    onClick={() => run(() => addIsolate(client, loadedOrder, {
                      organism_code: organism, id_method: idMethod,
                      id_confidence: selectedMethod?.carriesConfidence
                        ? Number(idConfidence) : undefined,
                    }))}>
              Add isolate
            </button>
          </fieldset>
        </div>
      )}

      <h3>Cumulative antibiogram</h3>
      <button type="button"
              onClick={() => run(async () => {
                const result = await getAntibiogram(client);
                setAntibiogram(result.items);
              })}>
        Load antibiogram
      </button>
      {antibiogram && antibiogram.length === 0 && (
        <Empty label="No finalised isolates in the antibiogram period." />)}
      {antibiogram && antibiogram.length > 0 && (
        <table>
          <thead>
            <tr><th>Organism</th><th>Antibiotic</th><th>Isolates</th>
              <th>%S</th></tr>
          </thead>
          <tbody>
            {antibiogram.map((item) => (
              <tr key={`${item.organism}-${item.antibiotic}`}>
                <td>{item.organism_display ?? item.organism}</td>
                <td>{item.antibiotic_display ?? item.antibiotic}</td>
                <td>{item.isolates}</td>
                <td>{item.suppressed
                  ? `Suppressed — ${item.suppression_reason}`
                  : `${item.pct_s}%`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
