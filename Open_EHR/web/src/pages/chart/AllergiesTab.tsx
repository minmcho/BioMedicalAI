import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { codeLabel } from "../../api/display";
import type { AllergyIntolerance } from "../../api/types";
import { Empty } from "../../components/StatusViews";

/** Wave A (CHART-B2) — allergies in the chart.
 *
 *  AllergyIntolerance is the single source: this tab, the chart
 *  banner, the diet tray-check, DUR and the orders allergy hard stop
 *  (app.orders._allergy_conflict) all read the same resources. The
 *  tab rides the generic FHIR surface — list via the ChartPage chart
 *  load (client.search), create via client.createAllergy, edit via
 *  client.updateAllergy (a re-POST under the same id; the FHIR create
 *  path upserts — pinned by backend/tests/test_wave_a_chart.py).
 *
 *  NKDA design (no representation existed anywhere in the tree): an
 *  explicit "Record NKDA" creates an AllergyIntolerance whose code
 *  coding is {system: "urn:opencare:allergy", code:
 *  "no-known-allergy", display: "No known allergies"},
 *  clinicalStatus active, verificationStatus confirmed. "Reviewed,
 *  none" (an NKDA record exists) is therefore distinguishable from
 *  "never asked" (no records at all — the "Allergies not recorded"
 *  posture). The NKDA code is not a medication code, so it never
 *  trips the orders allergy conflict check. */

export const NKDA_SYSTEM = "urn:opencare:allergy";
export const NKDA_CODE = "no-known-allergy";

export function allergyCode(a: AllergyIntolerance): string | null {
  return a.code?.coding?.[0]?.code ?? null;
}

export function isNkda(a: AllergyIntolerance): boolean {
  return allergyCode(a) === NKDA_CODE;
}

export function clinicalCode(a: AllergyIntolerance): string {
  return a.clinicalStatus?.coding?.[0]?.code ?? "active";
}

export function verificationCode(a: AllergyIntolerance): string {
  return a.verificationStatus?.coding?.[0]?.code ?? "unconfirmed";
}

export function isActiveAllergy(a: AllergyIntolerance): boolean {
  const s = clinicalCode(a);
  return s !== "inactive" && s !== "resolved";
}

export type AllergyPosture =
  | { kind: "not-recorded" }
  | { kind: "nkda" }
  | { kind: "active"; names: string[] };

/** The banner posture for a patient's allergy list: active substance
 *  names, NKDA, or not-recorded — exactly one visual source. */
export function allergyPosture(list: AllergyIntolerance[]): AllergyPosture {
  const active = list.filter(isActiveAllergy);
  const substances = active.filter((a) => !isNkda(a));
  if (substances.length > 0) {
    return { kind: "active", names: substances.map((a) => codeLabel(a.code)) };
  }
  if (active.some(isNkda)) return { kind: "nkda" };
  return { kind: "not-recorded" };
}

function reactionText(a: AllergyIntolerance): string {
  const parts: string[] = [];
  for (const r of a.reaction ?? []) {
    const m = (r.manifestation ?? []).map((x) => codeLabel(x)).filter(Boolean);
    if (m.length) parts.push(m.join(", "));
    else if (r.description) parts.push(r.description);
    if (r.severity) parts.push(`(${r.severity})`);
  }
  return parts.join(" ");
}

interface Props {
  client: ApiClient;
  patientId: string;
  /** fhir.write in the runtime permissions matrix for the role —
   *  read-only roles see the data without add/edit controls. */
  canWrite: boolean;
  allergies: AllergyIntolerance[];
  onChanged?: (list: AllergyIntolerance[]) => void;
}

const CLINICAL_STATUSES = ["active", "inactive", "resolved"];
const VERIFICATION_STATUSES = ["confirmed", "unconfirmed", "refuted"];

function AllergyRow({
  allergy,
  canWrite,
  onSave,
  busy,
}: {
  allergy: AllergyIntolerance;
  canWrite: boolean;
  onSave: (a: AllergyIntolerance, clinical: string, verification: string) => void;
  busy: boolean;
}) {
  const [clinical, setClinical] = useState(clinicalCode(allergy));
  const [verification, setVerification] = useState(verificationCode(allergy));
  const id = allergy.id ?? codeLabel(allergy.code);
  return (
    <li>
      <strong>{codeLabel(allergy.code)}</strong>
      {allergy.criticality ? ` · criticality ${allergy.criticality}` : ""}
      {allergy.type ? ` · ${allergy.type}` : ""}
      {reactionText(allergy) ? ` · reaction: ${reactionText(allergy)}` : ""}
      {` · clinical: ${clinicalCode(allergy)} · verification: ${verificationCode(allergy)}`}
      {allergy.recordedDate ? ` · recorded ${allergy.recordedDate.slice(0, 10)}` : ""}
      {allergy.recorder?.display ? ` · by ${allergy.recorder.display}` : ""}
      {allergy.onsetString ? ` · onset: ${allergy.onsetString}` : ""}
      {canWrite && (
        <span className="allergy-edit">
          <label>
            Clinical status{" "}
            <select
              aria-label={`Clinical status for ${codeLabel(allergy.code)}`}
              value={clinical}
              onChange={(e) => setClinical(e.target.value)}
            >
              {CLINICAL_STATUSES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <label>
            Verification status{" "}
            <select
              aria-label={`Verification status for ${codeLabel(allergy.code)}`}
              value={verification}
              onChange={(e) => setVerification(e.target.value)}
            >
              {VERIFICATION_STATUSES.map((s) => (
                <option key={s} value={s}>{s}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() => onSave(allergy, clinical, verification)}
          >
            Save allergy {id}
          </button>
        </span>
      )}
    </li>
  );
}

export function AllergiesTab({ client, patientId, canWrite, allergies, onChanged }: Props) {
  const [list, setList] = useState<AllergyIntolerance[]>(allergies);
  const [code, setCode] = useState("");
  const [display, setDisplay] = useState("");
  const [category, setCategory] = useState("medication");
  const [criticality, setCriticality] = useState("low");
  const [kind, setKind] = useState("allergy");
  const [reaction, setReaction] = useState("");
  const [severity, setSeverity] = useState("moderate");
  const [onset, setOnset] = useState("");
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Keep the local list in step when the chart (re)loads it.
  const [seenProp, setSeenProp] = useState(allergies);
  if (allergies !== seenProp) {
    setSeenProp(allergies);
    setList(allergies);
  }

  const apply = (next: AllergyIntolerance[]) => {
    setList(next);
    onChanged?.(next);
  };

  const run = async (fn: () => Promise<AllergyIntolerance>, mode: "add" | "replace") => {
    setBusy(true);
    setActionError(null);
    try {
      const saved = await fn();
      apply(
        mode === "add"
          ? [...list, saved]
          : list.map((a) => (a.id === saved.id ? saved : a)),
      );
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Allergy save failed");
    } finally {
      setBusy(false);
    }
  };

  const addAllergy = () => {
    const nextErrors: Record<string, string> = {};
    if (!code.trim()) nextErrors.code = "Substance code is required";
    if (!display.trim()) nextErrors.display = "Substance display is required";
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    const body: Record<string, unknown> = {
      resourceType: "AllergyIntolerance",
      id: `alg-${patientId}-${code.trim()}-${Date.now()}`,
      clinicalStatus: { coding: [{ code: "active" }] },
      verificationStatus: { coding: [{ code: "unconfirmed" }] },
      type: kind,
      category: [category],
      criticality,
      code: {
        coding: [{ code: code.trim(), display: display.trim() }],
        text: display.trim(),
      },
      patient: { reference: `Patient/${patientId}` },
      recordedDate: new Date().toISOString(),
      ...(reaction.trim()
        ? { reaction: [{ manifestation: [{ text: reaction.trim() }], severity }] }
        : {}),
      ...(onset.trim() ? { onsetString: onset.trim() } : {}),
    };
    void run(() => client.createAllergy(body), "add").then(() => {
      setCode("");
      setDisplay("");
      setReaction("");
      setOnset("");
    });
  };

  const recordNkda = () => {
    const body: Record<string, unknown> = {
      resourceType: "AllergyIntolerance",
      id: `nkda-${patientId}`,
      clinicalStatus: { coding: [{ code: "active" }] },
      verificationStatus: { coding: [{ code: "confirmed" }] },
      code: {
        coding: [{ system: NKDA_SYSTEM, code: NKDA_CODE, display: "No known allergies" }],
        text: "No known allergies",
      },
      patient: { reference: `Patient/${patientId}` },
      recordedDate: new Date().toISOString(),
    };
    void run(() => client.createAllergy(body), "add");
  };

  const saveEdit = (a: AllergyIntolerance, clinical: string, verification: string) => {
    const body: Record<string, unknown> = {
      ...a,
      clinicalStatus: { coding: [{ code: clinical }] },
      verificationStatus: { coding: [{ code: verification }] },
    };
    void run(() => client.updateAllergy(body), "replace");
  };

  const posture = allergyPosture(list);

  return (
    <section aria-label="Allergies">
      {posture.kind === "not-recorded" && (
        <div className="allergy-not-recorded" role="status">
          <strong>Allergies not recorded</strong> — no allergy information
          has been recorded for this patient yet. Absence of a record is
          not evidence of no allergies.
          {canWrite && (
            <>
              {" "}
              <button type="button" disabled={busy} onClick={recordNkda}>
                Record NKDA
              </button>
            </>
          )}
        </div>
      )}
      {posture.kind === "nkda" && (
        <p className="allergy-nkda" role="status">
          <strong>No known allergies (NKDA)</strong> — recorded and confirmed.
        </p>
      )}
      {actionError && (
        <p role="alert" className="form-error">{actionError}</p>
      )}

      {list.length === 0 ? (
        <Empty label="No allergy records." />
      ) : (
        <ul className="allergy-list">
          {list.map((a) => (
            <AllergyRow
              key={a.id ?? codeLabel(a.code)}
              allergy={a}
              canWrite={canWrite}
              onSave={saveEdit}
              busy={busy}
            />
          ))}
        </ul>
      )}

      {canWrite && (
        <div className="allergy-add">
          <h4>Record an allergy</h4>
          <div className="form-field">
            <label htmlFor="allergy-code">Substance code</label>
            <input
              id="allergy-code"
              type="text"
              value={code}
              onChange={(e) => setCode(e.target.value)}
            />
            {errors.code && (
              <p role="alert" className="form-error">{errors.code}</p>
            )}
          </div>
          <div className="form-field">
            <label htmlFor="allergy-display">Substance display</label>
            <input
              id="allergy-display"
              type="text"
              value={display}
              onChange={(e) => setDisplay(e.target.value)}
            />
            {errors.display && (
              <p role="alert" className="form-error">{errors.display}</p>
            )}
          </div>
          <div className="form-field">
            <label htmlFor="allergy-category">Category</label>
            <select
              id="allergy-category"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
            >
              {["medication", "food", "environment", "biologic"].map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="allergy-criticality">Criticality</label>
            <select
              id="allergy-criticality"
              value={criticality}
              onChange={(e) => setCriticality(e.target.value)}
            >
              {["low", "high", "unable-to-assess"].map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="allergy-type">Type</label>
            <select
              id="allergy-type"
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              {["allergy", "intolerance"].map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="allergy-reaction">Reaction (optional)</label>
            <input
              id="allergy-reaction"
              type="text"
              value={reaction}
              onChange={(e) => setReaction(e.target.value)}
            />
          </div>
          <div className="form-field">
            <label htmlFor="allergy-severity">Reaction severity</label>
            <select
              id="allergy-severity"
              value={severity}
              onChange={(e) => setSeverity(e.target.value)}
            >
              {["mild", "moderate", "severe"].map((c) => (
                <option key={c} value={c}>{c}</option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="allergy-onset">Onset note (optional)</label>
            <input
              id="allergy-onset"
              type="text"
              value={onset}
              onChange={(e) => setOnset(e.target.value)}
            />
          </div>
          <button type="button" disabled={busy} onClick={addAllergy}>
            Add allergy
          </button>
        </div>
      )}
    </section>
  );
}
