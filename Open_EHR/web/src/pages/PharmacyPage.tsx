import { useEffect, useState } from "react";
import { fetchMedicationLabel } from "../api/barcodes";
import { ApiError, type ApiClient } from "../api/client";
import { codeLabel, refLabel } from "../api/display";
import { saveDownloadedFile } from "../api/download";
import type { BcmaOutcome, DispenseQueueItem, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { CompoundingTab } from "./pharmacy/CompoundingTab";
import { ControlledSubstancesTab } from "./pharmacy/ControlledSubstancesTab";
import { InventoryTab } from "./pharmacy/InventoryTab";
import { VerificationTab } from "./pharmacy/VerificationTab";

const TABS = [
  { key: "dispense", label: "Dispense & stock" },
  { key: "verification", label: "Verification & DUR" },
  { key: "inventory", label: "Inventory & procurement" },
  { key: "cs", label: "Controlled substances" },
  { key: "compounding", label: "Compounding" },
] as const;

// Depth pass (Workstream 1): section deep-link contract — nav
// sections (verification / inventory / controlled / compounding /
// bcma) map onto this page's tabs; bcma lives inside the dispense tab
// (the med-admin scan), and the controlled-substances tab reports
// back as "controlled".
const SECTION_TO_TAB: Record<string, string> = {
  verification: "verification",
  inventory: "inventory",
  controlled: "cs",
  cs: "cs",
  compounding: "compounding",
  bcma: "dispense",
  dispense: "dispense",
};
const TAB_TO_SECTION: Record<string, string> = {
  dispense: "bcma",
  verification: "verification",
  inventory: "inventory",
  cs: "controlled",
  compounding: "compounding",
};

interface Props {
  client: ApiClient;
  role: Role;
  section?: string;
  onSectionChange?: (section: string) => void;
}

/** Wave A (PHARM-B1): the dispense queue annotates every item with
 *  its verification state — the backend always sends it; the field
 *  is optional here only so older test fixtures keep rendering. */
export interface VerificationAnnotation {
  verified: boolean;
  verified_by: string | null;
  verified_at: string | null;
  state: "current" | "none" | "revoked" | "stale" | "expired";
  reason: string;
}

export type AnnotatedQueueItem = DispenseQueueItem & {
  verification?: VerificationAnnotation;
};

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Landing-tab rule (UX spec N-9 / F-10): the module lands on the
 *  entering role's primary work queue. A role holding
 *  `pharmacy.verify` in the published permissions matrix (the same
 *  matrix VerificationTab gates its Verify action on) lands on the
 *  Verification & DUR tab; every other role keeps the Dispense &
 *  stock default. An explicit deep-link section always wins — the
 *  caller applies that before consulting this helper. */
export function pharmacyLandingTab(canVerify: boolean): string {
  return canVerify ? "verification" : "dispense";
}

export function PharmacyPage({ client, role, section, onSectionChange }: Props) {
  const { permissions } = useRuntimeConfig();
  const canDispense = permissions["pharmacy.dispense"]?.[role] === true;
  const canAdminister = permissions["pharmacy.administer"]?.[role] === true;
  const canVerify = permissions["pharmacy.verify"]?.[role] === true;
  const [tab, setTabState] = useState<string>(
    () => (section
      ? SECTION_TO_TAB[section] ?? pharmacyLandingTab(canVerify)
      : pharmacyLandingTab(canVerify)),
  );
  // Honour deep-links: when the shell's section changes, sync the tab.
  useEffect(() => {
    if (section) {
      const target = SECTION_TO_TAB[section];
      if (target) setTabState(target);
    }
  }, [section]);
  function setTab(key: string) {
    setTabState(key);
    onSectionChange?.(TAB_TO_SECTION[key] ?? key);
  }
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [quantities, setQuantities] = useState<Record<string, string>>({});
  const [lastDispenseId, setLastDispenseId] = useState<string | null>(null);
  const [lastDispensePatient, setLastDispensePatient] = useState<string | null>(null);
  // Wave A (PHARM-M2): med-admin is the five-rights BCMA scan — the
  // weak typed-ID path is retired. The form takes scan payloads.
  const [wristbandPayload, setWristbandPayload] = useState("");
  const [medLabelPayload, setMedLabelPayload] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [lastAdministration, setLastAdministration] =
    useState<BcmaOutcome | null>(null);
  const [revokeFor, setRevokeFor] = useState<string | null>(null);
  const [revokeReason, setRevokeReason] = useState("");
  const [labelBusy, setLabelBusy] = useState(false);
  const [labelError, setLabelError] = useState<string | null>(null);
  const [labelNotice, setLabelNotice] = useState<string | null>(null);

  const queueState = useAsync(() => client.dispenseQueue(), [client, tick]);
  const stockState = useAsync(() => client.stock(), [client, tick]);
  const formularyState = useAsync(() => client.formulary(), [client]);

  const queue = (queueState.data ?? []) as AnnotatedQueueItem[];
  const stock = stockState.data ?? [];
  // The med-label print target is the dispense just made on this page.
  const labelDispenseId = lastDispenseId;

  /** Fill the BCMA scan fields with real payloads for a dispense —
   *  the same barcode endpoints a printed wristband/label pair uses
   *  (the MarTab scan simulator pattern). */
  async function fillScanPayloads(patientId: string, dispenseId: string) {
    const wb = await client.barcodeWristband(patientId);
    setWristbandPayload(wb.payload);
    const ml = await client.barcodeMedLabel(dispenseId);
    setMedLabelPayload(ml.payload);
  }

  /** Revoke a verification (Wave A, PHARM-B1): reason required; the
   *  order returns to the verification queue and can no longer be
   *  dispensed or administered against the revoked verification. */
  async function revokeVerification(orderId: string) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      const c = client as unknown as {
        request?: <T>(path: string, init?: RequestInit) => Promise<T>;
        revokeVerification?: (id: string, reason: string) => Promise<unknown>;
      };
      if (typeof c.request === "function") {
        await c.request(
          `/pharmacy/verification/${encodeURIComponent(orderId)}/revoke`,
          { method: "POST", body: JSON.stringify({ reason: revokeReason.trim() }) },
        );
      } else if (typeof c.revokeVerification === "function") {
        await c.revokeVerification(orderId, revokeReason.trim());
      }
      setNotice(`Verification for ${orderId} revoked (${revokeReason.trim()}).`);
      setRevokeFor(null);
      setRevokeReason("");
      setTick((t) => t + 1);
    } catch (err) {
      setActionError(`Revoke failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  async function printMedLabel() {
    if (!labelDispenseId) return;
    setLabelBusy(true);
    setLabelError(null);
    setLabelNotice(null);
    try {
      const file = await fetchMedicationLabel(client, labelDispenseId);
      saveDownloadedFile(file);
      setLabelNotice(
        `Medication label downloaded (${file.filename}). Print was audited.`,
      );
    } catch (err) {
      setLabelError(`Print med label failed: ${errorText(err)}`);
    } finally {
      setLabelBusy(false);
    }
  }

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
    <section>
      <ModuleHeader
        icon="pill"
        group="Diagnostics & Pharmacy"
        title="Pharmacy"
        purpose="Verify orders, dispense and administer medications, and run the pharmacy work centre."
      />
      <p className="hint">
        The formulary is Studio configuration
        {formularyState.data?.version
          ? ` (terminology/formulary v${formularyState.data.version})`
          : ""}
        . Dispensing an active request creates a MedicationDispense and
        decrements stock for stock-tracked medications — dispensing more
        than the on-hand stock is refused, and (Wave A) dispensing is
        refused 409 unless the order carries a CURRENT pharmacist
        verification: the queue shows each order&apos;s verification
        state and the reason when it is not current. Med-admin is the
        five-rights BCMA scan: scan the wristband payload and the
        medication label payload; the server checks patient, medication,
        dispense/lot, dose, route and time, requires the authorizing
        order&apos;s verification to still be current, and refuses a
        second administration of the same dispense. The other tabs are the
        department work centre: verification &amp; DUR, lot-level inventory
        &amp; procurement with DSCSA traceability, controlled substances,
        and sterile compounding — thresholds throughout are Studio
        configuration, and drug-interaction checking stays inert until a
        knowledge source is configured (see Verification &amp; DUR).
      </p>
      <Tabs
        tabs={TABS.map((t) => ({ key: t.key, label: t.label }))}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Pharmacy work centre"
        panels={{
          verification: <VerificationTab client={client} role={role} />,
          inventory: <InventoryTab client={client} role={role} />,
          cs: <ControlledSubstancesTab client={client} role={role} />,
          compounding: <CompoundingTab client={client} role={role} />,
          dispense: (
      <>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      <h3>Dispense queue</h3>
      {queueState.loading && <Loading label="Loading dispense queue…" />}
      {queueState.error != null && <ErrorView error={queueState.error} />}
      {!queueState.loading && !queueState.error && queue.length === 0 && (
        <Empty label="No active medication requests awaiting dispensing." />
      )}
      {queue.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Request</th>
              <th>Medication</th>
              <th>Code</th>
              <th>Patient</th>
              <th>Stock</th>
              <th>Verification</th>
              <th>Qty</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {queue.map((item) => {
              const rid = item.request.id as string;
              const oversold = Boolean(
                item.stock_tracked && item.stock_quantity !== null &&
                Number(quantities[rid] || "1") > (item.stock_quantity ?? 0));
              const v = item.verification;
              const verificationBlocked = Boolean(
                v && v.state !== "current");
              const patientId = (item.request.subject?.reference ?? "")
                .replace("Patient/", "");
              return (
                <tr key={rid}>
                  <td>{rid}</td>
                  <td>{codeLabel(item.request.medicationCodeableConcept)}</td>
                  <td>{item.medication_code ?? "—"}</td>
                  <td>{refLabel(item.request.subject)}</td>
                  <td>
                    {item.stock_tracked
                      ? `${item.stock_quantity ?? 0} on hand`
                      : item.in_formulary
                        ? "not stock-tracked"
                        : "not in formulary"}
                  </td>
                  <td>
                    {v == null ? (
                      "—"
                    ) : v.state === "current" ? (
                      <span>
                        verified by {v.verified_by ?? "—"}
                        {v.verified_at ? ` at ${v.verified_at}` : ""}
                      </span>
                    ) : (
                      <span className="hint">
                        {v.state}: {v.reason}
                      </span>
                    )}
                    {v?.state === "current" && canVerify && (
                      <>
                        {" "}
                        {revokeFor === rid ? (
                          <span>
                            <input
                              aria-label={`Revoke reason for ${rid}`}
                              value={revokeReason}
                              onChange={(e) => setRevokeReason(e.target.value)}
                              placeholder="Reason (required)"
                            />
                            <button
                              type="button"
                              disabled={busy || !revokeReason.trim()}
                              title={
                                revokeReason.trim()
                                  ? undefined
                                  : "A reason is required to revoke a verification"
                              }
                              onClick={() => void revokeVerification(rid)}
                            >
                              Confirm revoke
                            </button>
                            {!revokeReason.trim() && (
                              <span className="hint"> reason required</span>
                            )}
                          </span>
                        ) : (
                          <button
                            type="button"
                            disabled={busy}
                            onClick={() => {
                              setRevokeFor(rid);
                              setRevokeReason("");
                            }}
                          >
                            Revoke verification
                          </button>
                        )}
                      </>
                    )}
                  </td>
                  <td>
                    <input
                      aria-label={`Quantity for ${rid}`}
                      type="number"
                      min={1}
                      value={quantities[rid] ?? "1"}
                      onChange={(e) =>
                        setQuantities((q) => ({ ...q, [rid]: e.target.value }))}
                      style={{ width: "4.5rem" }}
                    />
                  </td>
                  <td>
                    {canDispense && (
                      <button
                        type="button"
                        disabled={busy || !item.in_formulary || oversold || verificationBlocked}
                        title={
                          verificationBlocked
                            ? (v?.reason ?? "No current verification")
                            : !item.in_formulary
                              ? "Medication is not in the published formulary"
                              : oversold
                                ? "Quantity exceeds the on-hand stock"
                                : undefined
                        }
                        onClick={() =>
                          void perform("Dispense", async () => {
                            const outcome = await client.dispense(
                              rid, Number(quantities[rid] || "1"));
                            const dispenseId = outcome.dispense.id as string;
                            setLastDispenseId(dispenseId);
                            setLastDispensePatient(patientId || null);
                            // Pre-fill the BCMA scan form with real
                            // payloads for the dispense just made.
                            if (patientId) {
                              await fillScanPayloads(patientId, dispenseId);
                            }
                          })
                        }
                      >
                        Dispense
                      </button>
                    )}
                    {oversold && <span className="hint"> exceeds stock</span>}
                    {verificationBlocked && (
                      <span className="hint"> {v?.reason}</span>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
      {lastDispenseId && (
        <p className="hint">
          Last dispense: <code>{lastDispenseId}</code> — real wristband
          and medication-label payloads for it have been filled into
          the BCMA scan form below.
        </p>
      )}
      {labelDispenseId && (
        <div>
          <p>
            <button
              type="button"
              disabled={labelBusy}
              onClick={() => void printMedLabel()}
            >
              <span aria-hidden="true">🖨 </span>Print med label
            </button>{" "}
            <span className="hint">
              for dispense <code>{labelDispenseId}</code>
            </span>
            {labelNotice && (
              <span className="hint" role="status"> {labelNotice}</span>
            )}
          </p>
          {labelError && (
            <div className="state state-error" role="alert">{labelError}</div>
          )}
        </div>
      )}

      <h3>Stock</h3>
      {stockState.loading && <Loading label="Loading stock…" />}
      {stockState.error != null && <ErrorView error={stockState.error} />}
      {stock.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Code</th>
              <th>Medication</th>
              <th>Tracked</th>
              <th>On hand</th>
            </tr>
          </thead>
          <tbody>
            {stock.map((item) => (
              <tr key={item.code}>
                <td>{item.code}</td>
                <td>{item.display ?? "—"}</td>
                <td>{item.stock_tracked ? "yes" : "no"}</td>
                <td>{item.stock_tracked ? (item.quantity ?? 0) : "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Med-admin scan (BCMA five rights)</h3>
      <p className="hint">
        Scan the patient wristband payload, then the medication label
        payload (keyboard-wedge input). The server checks the five
        rights and requires the authorizing order&apos;s pharmacist
        verification to still be current; a dose, route or time
        failure can proceed only with an override reason, which is
        audited and stamped on the administration. Refusals are shown
        verbatim below.
      </p>
      <form
        noValidate
        className="toolbar"
        onSubmit={(e) => {
          e.preventDefault();
          void perform("Administer", async () => {
            const outcome = await client.bcmaAdminister({
              wristbandPayload: wristbandPayload.trim(),
              medLabelPayload: medLabelPayload.trim(),
              ...(overrideReason.trim()
                ? { overrideReason: overrideReason.trim() }
                : {}),
            });
            setLastAdministration(outcome);
          });
        }}
      >
        <label>
          Wristband payload{" "}
          <input
            aria-label="Wristband payload"
            value={wristbandPayload}
            onChange={(e) => setWristbandPayload(e.target.value)}
            placeholder="OCWB1|…"
          />
        </label>
        <label>
          Medication label payload{" "}
          <input
            aria-label="Medication label payload"
            value={medLabelPayload}
            onChange={(e) => setMedLabelPayload(e.target.value)}
            placeholder="OCML1|…"
          />
        </label>
        <label>
          Override reason (dose/route/time only){" "}
          <input
            aria-label="BCMA override reason"
            value={overrideReason}
            onChange={(e) => setOverrideReason(e.target.value)}
          />
        </label>
        {canAdminister && (
          <button
            type="submit"
            disabled={busy || !wristbandPayload.trim() || !medLabelPayload.trim()}
            title={
              !wristbandPayload.trim() || !medLabelPayload.trim()
                ? "Scan both payloads (wristband, then medication label) to administer"
                : undefined
            }
          >
            Administer
          </button>
        )}
        {(!wristbandPayload.trim() || !medLabelPayload.trim()) && (
          <span className="hint">
            Scan both payloads to administer.
            {lastDispenseId && lastDispensePatient && (
              <>
                {" "}
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    void perform("Fill scan payloads", async () => {
                      await fillScanPayloads(lastDispensePatient, lastDispenseId);
                    })
                  }
                >
                  Fill payloads for last dispense
                </button>
              </>
            )}
          </span>
        )}
      </form>
      {lastAdministration && (
        <div role="status">
          <p className="hint">
            Administered: {codeLabel(lastAdministration.administration.medicationCodeableConcept)} to{" "}
            {refLabel(lastAdministration.administration.subject)} at{" "}
            {lastAdministration.administration.effectiveDateTime ?? "—"} (
            {lastAdministration.administration.id}).
          </p>
          <ul>
            {lastAdministration.bcma.checks.map((c) => (
              <li key={c.right}>
                {c.right}: <strong>{c.result}</strong> — {c.detail}
              </li>
            ))}
          </ul>
          {lastAdministration.bcma.override && (
            <p className="hint">
              Override recorded for{" "}
              {lastAdministration.bcma.override.rights.join(", ")}: “
              {lastAdministration.bcma.override.reason}”
            </p>
          )}
        </div>
      )}
      </>
          ),
        }}
      />
    </section>
  );
}
