import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { codeLabel } from "../../api/display";
import type {
  AllergyIntolerance,
  DosageInstruction,
  MedicationSigVocabulary,
  PlacedOrder,
  SigFrequency,
} from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { allergyPosture } from "./AllergiesTab";

interface Props {
  client: ApiClient;
  patientId: string;
  /** orders.place in the runtime permissions matrix for the role. */
  canPlace: boolean;
}

function orderLabel(order: PlacedOrder): string {
  if (order.resourceType === "MedicationRequest") {
    const mr = order as { medicationCodeableConcept?: Parameters<typeof codeLabel>[0] };
    return mr.medicationCodeableConcept ? codeLabel(mr.medicationCodeableConcept) : (order.id ?? "");
  }
  const sr = order as { code?: Parameters<typeof codeLabel>[0] };
  return sr.code ? codeLabel(sr.code) : (order.id ?? "");
}

/** The sig text carried on a placed order (dosageInstruction[0].text),
 *  shown in the order list and reused by the MAR. */
export function orderSigText(order: PlacedOrder): string | null {
  const mr = order as { dosageInstruction?: DosageInstruction[] };
  const text = mr.dosageInstruction?.[0]?.text;
  return typeof text === "string" && text ? text : null;
}

export interface SigInput {
  medicationDisplay: string;
  doseValue: number;
  doseUnit: string;
  routeCode: string;
  routeDisplay: string;
  frequency: SigFrequency;
  durationValue?: number;
  durationUnit?: string; // "d" | "wk"
  prn: boolean;
  prnReason?: string;
  indication?: string;
  system?: string;
}

/** Compose the FHIR dosageInstruction for the sig builder (Wave A,
 *  CHART-B3). The shape mirrors what the backend already parses:
 *  doseAndRate[0].doseQuantity {value, unit, code} (pharmacy
 *  dose_quantity_of), route.coding[0].code (pharmacy_ops route read),
 *  timing.repeat {frequency, period, periodUnit} + boundsDuration
 *  (pharmacy doses_per_day_of / MAR interval maths). An as-needed
 *  frequency carries timing.code instead of a repeat — no scheduled
 *  interval exists for it. The human-readable sig text rides on
 *  dosageInstruction[0].text, e.g. "Amoxicillin 500 mg oral three
 *  times daily for 7 days". */
export function composeSig(input: SigInput): { dosageInstruction: DosageInstruction[]; text: string } {
  const system = input.system ?? "urn:opencare:medication-sig";
  const durationLabel = input.durationValue
    ? input.durationUnit === "wk"
      ? `${input.durationValue} week${input.durationValue === 1 ? "" : "s"}`
      : `${input.durationValue} day${input.durationValue === 1 ? "" : "s"}`
    : null;
  let text =
    `${input.medicationDisplay} ${input.doseValue} ${input.doseUnit} ` +
    `${input.routeDisplay.toLowerCase()} ${(input.frequency.display ?? input.frequency.code).toLowerCase()}`;
  if (input.prn && input.prnReason) text += `, as needed for ${input.prnReason}`;
  if (durationLabel) text += ` for ${durationLabel}`;
  if (input.indication) text += ` — indication: ${input.indication}`;

  const boundsDuration = input.durationValue
    ? { value: input.durationValue, unit: input.durationUnit ?? "d", code: input.durationUnit ?? "d" }
    : undefined;
  const timing: NonNullable<DosageInstruction["timing"]> = {};
  if (input.frequency.repeat) {
    timing.repeat = {
      ...input.frequency.repeat,
      ...(boundsDuration ? { boundsDuration } : {}),
    };
  } else {
    timing.code = {
      coding: [{ system, code: input.frequency.code, display: input.frequency.display }],
      text: input.frequency.display,
    };
    if (boundsDuration) timing.repeat = { boundsDuration };
  }

  const entry: DosageInstruction = {
    text,
    doseAndRate: [
      { doseQuantity: { value: input.doseValue, unit: input.doseUnit, code: input.doseUnit } },
    ],
    route: {
      coding: [{ system, code: input.routeCode, display: input.routeDisplay }],
      text: input.routeDisplay,
    },
    timing,
    ...(input.prn
      ? {
          asNeededBoolean: true,
          ...(input.prnReason
            ? { asNeededCodeableConcept: { text: input.prnReason } }
            : {}),
        }
      : {}),
  };
  return { dosageInstruction: [entry], text };
}

/** Lifecycle actions offered per state — the server enforces the
 *  published workflow/order-flow machine; this is only presentation. */
const ACTIONS: Record<string, { to: string; label: string }[]> = {
  draft: [
    { to: "signed", label: "Sign" },
    { to: "cancelled", label: "Cancel" },
  ],
  signed: [
    { to: "active", label: "Activate" },
    { to: "discontinued", label: "Discontinue" },
    { to: "cancelled", label: "Cancel" },
  ],
  active: [
    { to: "completed", label: "Complete" },
    { to: "discontinued", label: "Discontinue" },
    { to: "cancelled", label: "Cancel" },
  ],
};

/** Orders tab: place single orders or expand a published order set,
 *  then work the lifecycle. Order types, required fields and sets all
 *  come from published Studio config fetched at runtime. Medication
 *  orders carry a structured sig built from the published
 *  medication-sig vocabulary (Wave A, CHART-B3), and the patient's
 *  active allergies are shown above the form (CHART-B2) — the backend
 *  hard stop at placement is the backstop, not the first notice. */
export function OrdersTab({ client, patientId, canPlace }: Props) {
  const { data, loading, error, reload } = useAsync(async () => {
    const [types, sets, list, allergies] = await Promise.all([
      client.orderTypes(),
      client.orderSets(),
      client.orders(patientId),
      typeof client.allergies === "function"
        ? client.allergies(patientId).catch(() => [] as AllergyIntolerance[])
        : Promise.resolve([] as AllergyIntolerance[]),
    ]);
    return {
      types: types.order_types.concepts.filter((c) => c.active !== false),
      sets: sets.sets,
      orders: list.items,
      sigVocab: (types.medication_sig ?? null) as MedicationSigVocabulary | null,
      allergies,
    };
  }, [client, patientId]);

  const [typeCode, setTypeCode] = useState("procedure");
  const [code, setCode] = useState("");
  const [display, setDisplay] = useState("");
  const [performerRole, setPerformerRole] = useState("");
  const [modality, setModality] = useState("");
  const [setKey, setSetKey] = useState("");
  const [warnings, setWarnings] = useState<string[]>([]);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Sig builder state (medication orders only).
  const [doseValue, setDoseValue] = useState("");
  const [doseUnit, setDoseUnit] = useState("");
  const [routeCode, setRouteCode] = useState("");
  const [frequencyCode, setFrequencyCode] = useState("");
  const [durationValue, setDurationValue] = useState("");
  const [durationUnit, setDurationUnit] = useState("d");
  const [prn, setPrn] = useState(false);
  const [prnReason, setPrnReason] = useState("");
  const [indication, setIndication] = useState("");
  const [sigErrors, setSigErrors] = useState<Record<string, string>>({});

  if (loading) return <Loading label="Loading orders…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No order data." />;

  const selectedType = data.types.find((t) => t.code === typeCode);
  const needs = (field: string) => (selectedType?.requiredFields ?? []).includes(field);
  const isMedication = typeCode === "medication";
  const vocab = data.sigVocab;
  const routes = (vocab?.routes ?? []).filter((c) => c.active !== false);
  const frequencies = (vocab?.frequencies ?? []).filter((c) => c.active !== false);
  const units = (vocab?.units ?? []).filter((c) => c.active !== false);
  const selectedRoute = routes.find((r) => r.code === routeCode);
  const selectedFrequency = frequencies.find((f) => f.code === frequencyCode);

  const sigPreview =
    isMedication && vocab && Number(doseValue) > 0 && doseUnit && selectedRoute && selectedFrequency
      ? composeSig({
          medicationDisplay: display || code || "Medication",
          doseValue: Number(doseValue),
          doseUnit,
          routeCode: selectedRoute.code,
          routeDisplay: selectedRoute.display ?? selectedRoute.code,
          frequency: selectedFrequency,
          durationValue: durationValue ? Number(durationValue) : undefined,
          durationUnit,
          prn,
          prnReason: prnReason.trim() || undefined,
          indication: indication.trim() || undefined,
          system: vocab.system,
        }).text
      : null;

  const validateSig = (): Record<string, string> => {
    const errs: Record<string, string> = {};
    if (!isMedication || !vocab) return errs;
    if (!doseValue || !(Number(doseValue) > 0)) errs.dose = "Dose must be greater than 0";
    if (!doseUnit) errs.unit = "Dose unit is required";
    if (!routeCode) errs.route = "Route is required";
    if (!frequencyCode) errs.frequency = "Frequency is required";
    if (durationValue && !(Number(durationValue) > 0))
      errs.duration = "Duration must be greater than 0";
    if (prn && !prnReason.trim()) errs.prnReason = "PRN reason is required when PRN is selected";
    return errs;
  };

  const placeSingle = () => {
    const errs = validateSig();
    setSigErrors(errs);
    if (Object.keys(errs).length > 0) return;
    const sig =
      isMedication && vocab && selectedRoute && selectedFrequency
        ? composeSig({
            medicationDisplay: display || code,
            doseValue: Number(doseValue),
            doseUnit,
            routeCode: selectedRoute.code,
            routeDisplay: selectedRoute.display ?? selectedRoute.code,
            frequency: selectedFrequency,
            durationValue: durationValue ? Number(durationValue) : undefined,
            durationUnit,
            prn,
            prnReason: prnReason.trim() || undefined,
            indication: indication.trim() || undefined,
            system: vocab.system,
          })
        : null;
    void run(() =>
      client.placeOrder({
        type: typeCode,
        code,
        ...(display ? { display } : {}),
        patient: patientId,
        ...(performerRole ? { performerRole } : {}),
        ...(modality ? { modality } : {}),
        ...(sig ? { dosageInstruction: sig.dosageInstruction } : {}),
      }),
    );
  };

  const run = async (fn: () => Promise<{ warnings: string[] } | unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      const out = (await fn()) as { warnings?: string[] };
      setWarnings(out.warnings ?? []);
      reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Order action failed");
    } finally {
      setBusy(false);
    }
  };

  const posture = allergyPosture(data.allergies);

  return (
    <section aria-label="Orders">
      <div className="order-allergies" aria-label="Patient allergies">
        {posture.kind === "active" && (
          <p role="alert">
            <strong>Allergies:</strong> {posture.names.join(", ")} — ordering a
            conflicting medication will be refused unless an override reason
            is recorded.
          </p>
        )}
        {posture.kind === "nkda" && (
          <p role="status"><strong>Allergies:</strong> No known allergies (NKDA)</p>
        )}
        {posture.kind === "not-recorded" && (
          <p role="status"><strong>Allergies:</strong> not recorded</p>
        )}
      </div>
      {canPlace && (
        <div className="order-place">
          <h4>Place an order</h4>
          <div className="form-field">
            <label htmlFor="order-type">Order type</label>
            <select
              id="order-type"
              value={typeCode}
              onChange={(e) => setTypeCode(e.target.value)}
            >
              {data.types.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.display ?? t.code}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="order-code">Code</label>
            <input
              id="order-code"
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="order-display">Display (optional)</label>
            <input
              id="order-display"
              type="text"
              value={display}
              onChange={(e) => setDisplay(e.target.value)}
            />
          </div>
          {needs("performerRole") && (
            <div className="form-field">
              <label htmlFor="order-performer">Performing role</label>
              <input
                id="order-performer"
                type="text"
                value={performerRole}
                onChange={(e) => setPerformerRole(e.target.value)}
              />
            </div>
          )}
          {needs("modality") && (
            <div className="form-field">
              <label htmlFor="order-modality">Modality</label>
              <input
                id="order-modality"
                type="text"
                value={modality}
                onChange={(e) => setModality(e.target.value)}
              />
            </div>
          )}
          {isMedication && (
            <fieldset className="sig-builder">
              <legend>Sig (dose / route / frequency)</legend>
              {!vocab && (
                <p className="hint" role="status">
                  Sig builder unavailable: the medication-sig vocabulary is
                  not published, so this order will carry no structured
                  dosage. Publish terminology/medication-sig in Studio to
                  enable it.
                </p>
              )}
              {vocab && (
                <>
                  <div className="form-field">
                    <label htmlFor="sig-dose">Dose</label>
                    <input
                      id="sig-dose"
                      type="number"
                      min="0"
                      step="any"
                      value={doseValue}
                      onChange={(e) => setDoseValue(e.target.value)}
                    />
                    {sigErrors.dose && (
                      <p role="alert" className="form-error">{sigErrors.dose}</p>
                    )}
                  </div>
                  <div className="form-field">
                    <label htmlFor="sig-unit">Dose unit</label>
                    <select
                      id="sig-unit"
                      value={doseUnit}
                      onChange={(e) => setDoseUnit(e.target.value)}
                    >
                      <option value="">Select a unit…</option>
                      {units.map((u) => (
                        <option key={u.code} value={u.code}>
                          {u.display ?? u.code}
                        </option>
                      ))}
                    </select>
                    {sigErrors.unit && (
                      <p role="alert" className="form-error">{sigErrors.unit}</p>
                    )}
                  </div>
                  <div className="form-field">
                    <label htmlFor="sig-route">Route</label>
                    <select
                      id="sig-route"
                      value={routeCode}
                      onChange={(e) => setRouteCode(e.target.value)}
                    >
                      <option value="">Select a route…</option>
                      {routes.map((r) => (
                        <option key={r.code} value={r.code}>
                          {r.display ?? r.code}
                        </option>
                      ))}
                    </select>
                    {sigErrors.route && (
                      <p role="alert" className="form-error">{sigErrors.route}</p>
                    )}
                  </div>
                  <div className="form-field">
                    <label htmlFor="sig-frequency">Frequency</label>
                    <select
                      id="sig-frequency"
                      value={frequencyCode}
                      onChange={(e) => setFrequencyCode(e.target.value)}
                    >
                      <option value="">Select a frequency…</option>
                      {frequencies.map((f) => (
                        <option key={f.code} value={f.code}>
                          {f.display ?? f.code}
                        </option>
                      ))}
                    </select>
                    {sigErrors.frequency && (
                      <p role="alert" className="form-error">{sigErrors.frequency}</p>
                    )}
                  </div>
                  <div className="form-field">
                    <label htmlFor="sig-duration">Duration (optional)</label>
                    <input
                      id="sig-duration"
                      type="number"
                      min="0"
                      step="any"
                      value={durationValue}
                      onChange={(e) => setDurationValue(e.target.value)}
                    />
                    <select
                      aria-label="Duration unit"
                      value={durationUnit}
                      onChange={(e) => setDurationUnit(e.target.value)}
                    >
                      <option value="d">days</option>
                      <option value="wk">weeks</option>
                    </select>
                    {sigErrors.duration && (
                      <p role="alert" className="form-error">{sigErrors.duration}</p>
                    )}
                  </div>
                  <div className="form-field">
                    <label htmlFor="sig-prn">
                      <input
                        id="sig-prn"
                        type="checkbox"
                        checked={prn}
                        onChange={(e) => setPrn(e.target.checked)}
                      />{" "}
                      PRN (as needed)
                    </label>
                  </div>
                  {prn && (
                    <div className="form-field">
                      <label htmlFor="sig-prn-reason">PRN reason</label>
                      <input
                        id="sig-prn-reason"
                        type="text"
                        value={prnReason}
                        onChange={(e) => setPrnReason(e.target.value)}
                      />
                      {sigErrors.prnReason && (
                        <p role="alert" className="form-error">{sigErrors.prnReason}</p>
                      )}
                    </div>
                  )}
                  <div className="form-field">
                    <label htmlFor="sig-indication">Indication (optional)</label>
                    <input
                      id="sig-indication"
                      type="text"
                      value={indication}
                      onChange={(e) => setIndication(e.target.value)}
                    />
                  </div>
                  {sigPreview && (
                    <p className="sig-preview" role="status">
                      Sig: {sigPreview}
                    </p>
                  )}
                </>
              )}
            </fieldset>
          )}
          <button
            type="button"
            disabled={busy || !code}
            onClick={placeSingle}
          >
            Place order
          </button>

          <h4>Place from an order set</h4>
          <div className="form-field">
            <label htmlFor="order-set">Order set</label>
            <select
              id="order-set"
              value={setKey}
              onChange={(e) => setSetKey(e.target.value)}
            >
              <option value="">Select a set…</option>
              {data.sets.map((s) => (
                <option key={s.key} value={s.key}>
                  {s.name ?? s.key} ({s.orders.length} orders)
                </option>
              ))}
            </select>
          </div>
          <button
            type="button"
            disabled={busy || !setKey}
            onClick={() =>
              run(() => client.placeOrderSet({ set: setKey, patient: patientId }))
            }
          >
            Place order set
          </button>
        </div>
      )}

      {warnings.length > 0 && (
        <ul role="status" className="order-warnings">
          {warnings.map((w) => (
            <li key={w}>{w}</li>
          ))}
        </ul>
      )}
      {actionError && (
        <p role="alert" className="form-error">
          {actionError}
        </p>
      )}

      <h4>Orders for this patient</h4>
      {data.orders.length === 0 ? (
        <Empty label="No orders yet." />
      ) : (
        <ul className="order-list">
          {data.orders.map((order) => (
            <li key={order.id}>
              <span>
                {orderLabel(order)} — <span className="badge">{order.status}</span>
                {orderSigText(order) ? (
                  <span className="order-sig"> · Sig: {orderSigText(order)}</span>
                ) : null}
              </span>
              {canPlace &&
                order.id &&
                (ACTIONS[order.status ?? ""] ?? []).map((action) => (
                  <button
                    key={action.to}
                    type="button"
                    disabled={busy}
                    onClick={() => run(() => client.transitionOrder(order.id!, action.to))}
                  >
                    {action.label}
                  </button>
                ))}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
