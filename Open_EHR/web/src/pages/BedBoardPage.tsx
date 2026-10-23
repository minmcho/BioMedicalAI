import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import { getOrgTree } from "../api/org";
import type {
  BedEntry,
  FormDefinition,
  LocationDraftOp,
  QuestionnaireAnswerItem,
  Role,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { FormRenderer } from "../components/FormRenderer";
import { ModuleHeader, scrollToSection } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

/** Bed-status tone map (UX spec P-3). */
export function bedTone(status?: string | null): StatusTone {
  switch (status) {
    case "available":
      return "ok";
    case "occupied":
      return "info";
    case "cleaning":
    case "reserved":
      return "warn";
    case "blocked":
      return "bad";
    default:
      return "neutral";
  }
}

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** Flatten FormRenderer answers to {linkId: scalar} for plan fields. */
function answerScalars(answers: QuestionnaireAnswerItem[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (const item of answers) {
    const first = item.answer?.[0];
    if (!first) continue;
    const coding = first.valueCoding as { code?: string } | undefined;
    if (coding?.code) out[item.linkId] = coding.code;
    else if (typeof first.valueDate === "string") out[item.linkId] = first.valueDate;
    else if (typeof first.valueString === "string") out[item.linkId] = first.valueString;
  }
  return out;
}

export function BedBoardPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["inpatient.work"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [admitForm, setAdmitForm] = useState({ patient: "", bed: "" });
  const [transferBed, setTransferBed] = useState<Record<string, string>>({});
  const [dischargeFor, setDischargeFor] = useState<BedEntry | null>(null);
  const [checklistForm, setChecklistForm] = useState<FormDefinition | null>(null);
  // UX pass (spec P-7): the board and the Units & beds manager are
  // tabs, not one stacked scroll.
  const [tab, setTab] = useState("board");

  const censusState = useAsync(() => client.inpatientCensus(), [client, tick]);
  const census = censusState.data;

  // The discharge checklist is a published Studio form — load its
  // definition when a discharge panel opens (runtime config, like the
  // note templates in the chart).
  useEffect(() => {
    let cancelled = false;
    if (!dischargeFor) {
      setChecklistForm(null);
      return;
    }
    if (typeof client.effectiveConfig === "function") {
      client
        .effectiveConfig("form", "discharge-checklist")
        .then((cfg) => {
          if (!cancelled) {
            setChecklistForm(cfg.payload as unknown as FormDefinition);
          }
        })
        .catch(() => {
          if (!cancelled) setChecklistForm(null);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [client, dischargeFor]);

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

  const availableBeds = (census?.units ?? []).flatMap((u) =>
    u.beds.filter((b) => b.status === "available"),
  );

  async function submitChecklist(answers: QuestionnaireAnswerItem[]) {
    if (!dischargeFor?.encounter_id || !dischargeFor.patient_ref) return;
    const scalars = answerScalars(answers);
    await perform("Discharge recorded", async () => {
      const qr = await client.create("QuestionnaireResponse", {
        resourceType: "QuestionnaireResponse",
        questionnaire: "Questionnaire/discharge-checklist",
        status: "completed",
        subject: { reference: dischargeFor.patient_ref },
        encounter: { reference: `Encounter/${dischargeFor.encounter_id}` },
        item: answers,
      });
      await client.setDischargePlan(dischargeFor.encounter_id as string, {
        targetDate: scalars["target-discharge-date"],
        disposition: scalars["disposition"],
        barriers: scalars["barriers"],
        checklistResponseId: qr.id,
      });
      await client.dischargePatient(
        dischargeFor.encounter_id as string,
        scalars["disposition"],
      );
      setDischargeFor(null);
    });
  }

  function renderBed(bed: BedEntry) {
    const transferTarget = transferBed[bed.encounter_id ?? ""] ?? "";
    return (
      <li key={bed.code} className="board-card">
        <div>
          <strong>{bed.code}</strong>{" "}
          <StatusBadge tone={bedTone(bed.status)}>{bed.status}</StatusBadge>
        </div>
        <div className="hint">{bed.display ?? bed.code}</div>
        {bed.status === "occupied" && (
          <div className="hint">
            Patient {bed.patient_ref ? refLabel({ reference: bed.patient_ref }) : "—"}
            {bed.los_days != null ? ` · LOS ${bed.los_days} d` : ""}
            {bed.admitted_at
              ? ` · admitted ${new Date(bed.admitted_at).toLocaleDateString()}`
              : ""}
          </div>
        )}
        {canWork && bed.status === "cleaning" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} marked available`, () =>
                  client.setBedStatus(bed.code, "available"))}
            >
              Mark available
            </button>
          </div>
        )}
        {canWork && bed.status === "available" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} blocked`, () =>
                  client.setBedStatus(bed.code, "blocked"))}
            >
              Block bed
            </button>
          </div>
        )}
        {canWork && bed.status === "blocked" && (
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform(`Bed ${bed.code} marked available`, () =>
                  client.setBedStatus(bed.code, "available"))}
            >
              Mark available
            </button>
          </div>
        )}
        {canWork && bed.status === "occupied" && bed.encounter_id && (
          <div className="row-actions">
            <label>
              Transfer to{" "}
              <select
                aria-label={`Transfer target for ${bed.code}`}
                value={transferTarget}
                onChange={(e) =>
                  setTransferBed((prev) => ({
                    ...prev,
                    [bed.encounter_id as string]: e.target.value,
                  }))}
              >
                <option value="">Select bed…</option>
                {availableBeds.map((b) => (
                  <option key={b.code} value={b.code}>{b.code}</option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={busy || !transferTarget}
              onClick={() =>
                perform("Patient transferred", () =>
                  client.transferPatient(bed.encounter_id as string, transferTarget))}
            >
              Transfer
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => setDischargeFor(bed)}
            >
              Discharge…
            </button>
          </div>
        )}
      </li>
    );
  }

  const boardPanel = (
    <>
      {censusState.loading && <Loading label="Loading bed board…" />}
      {censusState.error != null && <ErrorView error={censusState.error} />}
      {census && (
        <p className="hint" data-testid="census-totals">
          {census.totals.beds_total} beds · {census.totals.occupied} occupied ·{" "}
          {census.totals.available} available · {census.totals.cleaning} cleaning ·{" "}
          {census.totals.blocked} blocked · occupancy {census.totals.occupancy_pct}%
        </p>
      )}

      {census && census.expected_discharges_today.total > 0 && (
        <div>
          <h3>Expected discharges today ({census.expected_discharges_today.total})</h3>
          <ul className="board-list">
            {census.expected_discharges_today.discharges.map((d) => (
              <li key={d.encounter_id} className="board-card">
                <strong>{d.bed_code ?? "—"}</strong>{" "}
                <span className="hint">
                  Patient {d.patient_ref ? refLabel({ reference: d.patient_ref }) : "—"}
                  {d.disposition ? ` · disposition ${d.disposition}` : ""}
                  {d.barriers ? ` · barriers: ${d.barriers}` : ""}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {canWork && census && (
        <form
          id="bedboard-admit-form"
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!admitForm.patient.trim() || !admitForm.bed) return;
            void perform("Patient admitted", async () => {
              await client.admitPatient({
                patient: admitForm.patient.trim(),
                bed: admitForm.bed,
              });
              setAdmitForm({ patient: "", bed: "" });
            });
          }}
        >
          <h3>Admit a patient</h3>
          <label>
            Patient id{" "}
            <input
              aria-label="Admit patient id"
              value={admitForm.patient}
              onChange={(e) => setAdmitForm({ ...admitForm, patient: e.target.value })}
              placeholder="pat-1"
            />
          </label>
          <label>
            Bed{" "}
            <select
              aria-label="Admit bed"
              value={admitForm.bed}
              onChange={(e) => setAdmitForm({ ...admitForm, bed: e.target.value })}
            >
              <option value="">Select bed…</option>
              {availableBeds.map((b) => (
                <option key={b.code} value={b.code}>
                  {b.code} — {b.display ?? b.code}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy}>Admit</button>
        </form>
      )}

      {dischargeFor && (
        <div className="inline-form">
          <h3>Discharge — {dischargeFor.code}</h3>
          <p className="hint">
            Patient {dischargeFor.patient_ref
              ? refLabel({ reference: dischargeFor.patient_ref })
              : "—"}. The checklist is the published Studio form
            (form/discharge-checklist); submitting it records the
            discharge plan and discharges the patient (the bed goes to
            cleaning).
          </p>
          {checklistForm ? (
            <FormRenderer
              form={checklistForm}
              submitLabel="Submit checklist & discharge"
              onSubmit={submitChecklist}
            />
          ) : (
            <p className="hint">Loading the published discharge checklist…</p>
          )}
          <div className="row-actions">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                perform("Patient discharged", async () => {
                  await client.dischargePatient(dischargeFor.encounter_id as string);
                  setDischargeFor(null);
                })}
            >
              Discharge without checklist
            </button>
            <button type="button" onClick={() => setDischargeFor(null)}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {census && census.units.length === 0 && (
        <Empty label="No units in the published locations configuration." />
      )}
      {census && census.units.map((unit) => (
        <div key={unit.code}>
          <h3>
            {unit.display ?? unit.code} — {unit.occupied}/{unit.beds_total} occupied
            ({unit.occupancy_pct}%)
          </h3>
          <ul className="board-list">{unit.beds.map(renderBed)}</ul>
        </div>
      ))}
    </>
  );

  return (
    <section>
      <ModuleHeader
        icon="bed"
        group="Operations"
        title="Bed board"
        purpose="Live bed state by unit — admit, transfer and discharge patients, and manage units and beds."
        primaryAction={canWork
          ? {
              label: "Admit a patient",
              onClick: () => {
                setTab("board");
                setTimeout(() => scrollToSection("bedboard-admit-form"), 0);
              },
            }
          : undefined}
      />
      <p className="hint">
        Hospital locations and beds are published Studio configuration
        (terminology/locations) materialised as FHIR Location resources;
        every admission, transfer and discharge writes an ADT event
        (A01/A02/A03) and an audited bed-status change — beds are never
        edited silently. A discharged or transferred-out bed goes to
        cleaning until housekeeping marks it available.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}
      <Tabs
        tabs={[
          { key: "board", label: "Board" },
          { key: "manager", label: "Units & beds manager" },
        ]}
        activeKey={tab}
        onChange={setTab}
        ariaLabel="Bed board views"
        panels={{
          board: boardPanel,
          manager: <UnitsBedsManager client={client} canWork={canWork} />,
        }}
      />
    </section>
  );
}

// ---------------------------------------------------------------------------
// Front Door pass: Units & Beds manager — governed draft composition
// ---------------------------------------------------------------------------

const CODE_RE = /^[A-Za-z0-9][A-Za-z0-9-]*$/;

/** Units & Beds manager. Shows the PUBLISHED locations config; every
 *  change is composed into the governed Studio DRAFT of
 *  terminology/locations and goes live only when a human publishes
 *  that draft in System Studio. The pending state is labelled in
 *  text (badge + sentence), never colour alone. */
function UnitsBedsManager({
  client,
  canWork,
}: {
  client: ApiClient;
  canWork: boolean;
}) {
  const [tick, setTick] = useState(0);
  const locationsState = useAsync(() => client.inpatientLocations(), [client, tick]);
  const draftState = useAsync(() => client.locationsDraftStatus(), [client, tick]);
  const [notice, setNotice] = useState<string | null>(null);
  const [mgrError, setMgrError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [unitForm, setUnitForm] = useState({ code: "", display: "", department: "" });
  const [regionForm, setRegionForm] = useState({ code: "", display: "" });
  const [deptForm, setDeptForm] = useState({ code: "", display: "", location: "" });
  const [bedForm, setBedForm] = useState({ code: "", display: "", unit: "", room: "", bed: "" });
  const [renameForm, setRenameForm] = useState({ code: "", display: "" });
  const [retireCode, setRetireCode] = useState("");
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const locations = locationsState.data?.locations ?? [];
  const units = locations.filter((l) => l.type === "unit");
  const facilities = locations.filter((l) => l.type === "facility");
  const departments = locations.filter((l) => l.type === "department");
  const draft = draftState.data?.draft ?? null;

  // Organisation hierarchy (Placement pass): the four-level tree
  // from GET /org-hierarchy, shown above the flat published list.
  const orgState = useAsync(() => getOrgTree(client), [client, tick]);

  async function compose(op: LocationDraftOp, label: string) {
    setBusy(true);
    setMgrError(null);
    setNotice(null);
    try {
      const out = await client.composeLocationsDraft([op]);
      setNotice(
        `${label} — added to locations draft v${out.version}. ` +
          "Nothing is live yet: a human must publish the draft in " +
          "System Studio → Terminology → locations.",
      );
      setTick((t) => t + 1);
    } catch (err) {
      setMgrError(`${label} failed: ${errorText(err)}`);
    } finally {
      setBusy(false);
    }
  }

  function codeError(key: string, code: string, display: string) {
    const errors: Record<string, string> = {};
    if (!CODE_RE.test(code.trim())) {
      errors[key] = "Codes are letters, digits and dashes, starting with a letter or digit.";
    }
    if (!display.trim()) errors[`${key}Display`] = "A display name is required.";
    setFieldErrors(errors);
    return Object.keys(errors).length === 0;
  }

  return (
    <section aria-label="Units and beds manager">
      <h3>Units &amp; beds manager</h3>
      <p className="hint">
        The list below is the <strong>published</strong> locations
        configuration (v{locationsState.data?.version ?? "—"}). Add,
        rename and retire actions compose a new <strong>draft</strong>{" "}
        version — nothing changes on the board above until a human
        publishes the draft in System Studio.
      </p>
      {draft ? (
        <p className="state state-empty" role="status">
          <span className="badge badge-warn">Draft pending publish</span>{" "}
          Locations draft v{draft.version} (by {draft.updated_by ?? "—"}) is
          waiting in System Studio → Terminology → locations. The board
          above still shows published v
          {draftState.data?.published_version ?? "—"}.
        </p>
      ) : (
        <p className="hint">No locations draft is pending publish.</p>
      )}
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {mgrError && (
        <p className="error-note" role="alert">{mgrError}</p>
      )}
      {locationsState.loading && <Loading label="Loading locations…" />}
      {locationsState.error != null && (
        <ErrorView error={locationsState.error} />
      )}
      {locations.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th scope="col">Code</th>
              <th scope="col">Name</th>
              <th scope="col">Type</th>
              <th scope="col">Parent</th>
              <th scope="col">State</th>
            </tr>
          </thead>
          <tbody>
            {locations.map((l) => (
              <tr key={l.code}>
                <td>{l.code}</td>
                <td>{l.display ?? l.code}</td>
                <td>{l.type}</td>
                <td>{l.parent ?? "—"}</td>
                <td>{l.active ? "Active" : "Retired"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Organisation hierarchy</h4>
      <p className="hint">
        Region → location → department → unit, from the published
        hierarchy (GET /org-hierarchy
        {orgState.data ? `, v${orgState.data.version ?? "—"}` : ""}).
      </p>
      {orgState.loading && <Loading label="Loading organisation hierarchy…" />}
      {orgState.error != null && (
        <p className="hint">
          Organisation hierarchy unavailable: {errorText(orgState.error)}
        </p>
      )}
      {orgState.data && orgState.data.regions.length === 0 && (
        <Empty label="No regions in the published organisation hierarchy." />
      )}
      {orgState.data && orgState.data.regions.length > 0 && (
        <ul aria-label="Organisation hierarchy tree">
          {orgState.data.regions.map((region) => (
            <li key={region.code}>
              <strong>{region.display ?? region.code}</strong> (region)
              <ul>
                {region.locations.map((location) => (
                  <li key={location.code}>
                    {location.display ?? location.code} (location)
                    <ul>
                      {location.departments.map((department) => (
                        <li key={department.code}>
                          {department.display ?? department.code} (department)
                          <ul>
                            {department.units.map((unit) => (
                              <li key={unit.code}>
                                {unit.display ?? unit.code} — {unit.bed_count} beds
                              </li>
                            ))}
                          </ul>
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}

      {canWork && (
        <>
          <form
      noValidate
      className="inline-form"
      aria-label="Add region"
            onSubmit={(e) => {
              e.preventDefault();
              if (!codeError("region", regionForm.code, regionForm.display)) return;
              void compose(
                { op: "add-region", code: regionForm.code.trim(), display: regionForm.display.trim() },
                `Region ${regionForm.code.trim()} drafted`,
              );
              setRegionForm({ code: "", display: "" });
            }}
          >
            <h4>Add a region (draft)</h4>
            <label className="field">
              Region code *
              <input
                aria-label="New region code"
                value={regionForm.code}
                onChange={(e) => setRegionForm({ ...regionForm, code: e.target.value })}
                placeholder="region-north"
              />
              {fieldErrors.region && (
                <span className="field-error" role="alert">{fieldErrors.region}</span>
              )}
            </label>
            <label className="field">
              Region name *
              <input
                aria-label="New region name"
                value={regionForm.display}
                onChange={(e) => setRegionForm({ ...regionForm, display: e.target.value })}
                placeholder="North Region"
              />
              {fieldErrors.regionDisplay && (
                <span className="field-error" role="alert">{fieldErrors.regionDisplay}</span>
              )}
            </label>
            <button type="submit" disabled={busy}>Add region to draft</button>
            <span className="hint">
              Draft only — nothing is live until published in System Studio.
            </span>
          </form>

          <form
      noValidate
      className="inline-form"
      aria-label="Add department"
            onSubmit={(e) => {
              e.preventDefault();
              if (!codeError("department", deptForm.code, deptForm.display)) return;
              if (!deptForm.location) {
                setFieldErrors({ deptLocation: "Choose the location the department belongs to." });
                return;
              }
              void compose(
                {
                  op: "add-department", code: deptForm.code.trim(),
                  display: deptForm.display.trim(), location: deptForm.location,
                },
                `Department ${deptForm.code.trim()} drafted`,
              );
              setDeptForm({ code: "", display: "", location: "" });
            }}
          >
            <h4>Add a department (draft)</h4>
            <label className="field">
              Department code *
              <input
                aria-label="New department code"
                value={deptForm.code}
                onChange={(e) => setDeptForm({ ...deptForm, code: e.target.value })}
                placeholder="dept-surgery"
              />
              {fieldErrors.department && (
                <span className="field-error" role="alert">{fieldErrors.department}</span>
              )}
            </label>
            <label className="field">
              Department name *
              <input
                aria-label="New department name"
                value={deptForm.display}
                onChange={(e) => setDeptForm({ ...deptForm, display: e.target.value })}
                placeholder="Surgery"
              />
              {fieldErrors.departmentDisplay && (
                <span className="field-error" role="alert">{fieldErrors.departmentDisplay}</span>
              )}
            </label>
            <label className="field">
              Location *
              <select
                aria-label="New department location"
                value={deptForm.location}
                disabled={facilities.length === 0}
                onChange={(e) => setDeptForm({ ...deptForm, location: e.target.value })}
              >
                <option value="">Select location…</option>
                {facilities.map((f) => (
                  <option key={f.code} value={f.code}>
                    {f.display ?? f.code}
                  </option>
                ))}
              </select>
              {facilities.length === 0 && (
                <span className="field-error">
                  No locations (facilities) in the published locations config.
                </span>
              )}
              {fieldErrors.deptLocation && (
                <span className="field-error" role="alert">{fieldErrors.deptLocation}</span>
              )}
            </label>
            <button type="submit" disabled={busy}>Add department to draft</button>
            <span className="hint">
              Draft only — nothing is live until published in System Studio.
            </span>
          </form>

          <form
      noValidate
      className="inline-form"
      aria-label="Add unit"
            onSubmit={(e) => {
              e.preventDefault();
              if (!codeError("unit", unitForm.code, unitForm.display)) return;
              void compose(
                {
                  op: "add-unit", code: unitForm.code.trim(),
                  display: unitForm.display.trim(),
                  ...(unitForm.department ? { department: unitForm.department } : {}),
                },
                `Unit ${unitForm.code.trim()} drafted`,
              );
              setUnitForm({ code: "", display: "", department: "" });
            }}
          >
            <h4>Add a unit (draft)</h4>
            <label className="field">
              Unit code *
              <input
                aria-label="New unit code"
                value={unitForm.code}
                onChange={(e) => setUnitForm({ ...unitForm, code: e.target.value })}
                placeholder="unit-stepdown"
              />
              {fieldErrors.unit && (
                <span className="field-error" role="alert">{fieldErrors.unit}</span>
              )}
            </label>
            <label className="field">
              Unit name *
              <input
                aria-label="New unit name"
                value={unitForm.display}
                onChange={(e) => setUnitForm({ ...unitForm, display: e.target.value })}
                placeholder="Step-down Unit"
              />
              {fieldErrors.unitDisplay && (
                <span className="field-error" role="alert">{fieldErrors.unitDisplay}</span>
              )}
            </label>
            <label className="field">
              Department
              <select
                aria-label="New unit department"
                value={unitForm.department}
                disabled={departments.length === 0}
                onChange={(e) => setUnitForm({ ...unitForm, department: e.target.value })}
              >
                <option value="">(no department)</option>
                {departments.map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.display ?? d.code}
                  </option>
                ))}
              </select>
              {departments.length === 0 && (
                <span className="field-error">
                  No departments in the published locations config — the
                  unit will be added without a department.
                </span>
              )}
            </label>
            <button type="submit" disabled={busy}>Add unit to draft</button>
          </form>

          <form
      noValidate
      className="inline-form"
      aria-label="Add bed"
            onSubmit={(e) => {
              e.preventDefault();
              if (!codeError("bed", bedForm.code, bedForm.display)) return;
              if (!bedForm.unit) {
                setFieldErrors({ bedUnit: "Choose the unit the bed belongs to." });
                return;
              }
              void compose(
                {
                  op: "add-bed", code: bedForm.code.trim(),
                  display: bedForm.display.trim(), unit: bedForm.unit,
                  room: bedForm.room.trim() || undefined,
                  bed: bedForm.bed.trim() || undefined,
                },
                `Bed ${bedForm.code.trim()} drafted`,
              );
              setBedForm({ code: "", display: "", unit: "", room: "", bed: "" });
            }}
          >
            <h4>Add a bed (draft)</h4>
            <label className="field">
              Bed code *
              <input
                aria-label="New bed code"
                value={bedForm.code}
                onChange={(e) => setBedForm({ ...bedForm, code: e.target.value })}
                placeholder="MS-107A"
              />
              {fieldErrors.bed && (
                <span className="field-error" role="alert">{fieldErrors.bed}</span>
              )}
            </label>
            <label className="field">
              Bed name *
              <input
                aria-label="New bed name"
                value={bedForm.display}
                onChange={(e) => setBedForm({ ...bedForm, display: e.target.value })}
                placeholder="Med-Surg 107A"
              />
              {fieldErrors.bedDisplay && (
                <span className="field-error" role="alert">{fieldErrors.bedDisplay}</span>
              )}
            </label>
            <label className="field">
              Unit *
              <select
                aria-label="New bed unit"
                value={bedForm.unit}
                disabled={units.length === 0}
                onChange={(e) => setBedForm({ ...bedForm, unit: e.target.value })}
              >
                <option value="">Select unit…</option>
                {units.map((u) => (
                  <option key={u.code} value={u.code}>
                    {u.display ?? u.code}
                  </option>
                ))}
              </select>
              {units.length === 0 && (
                <span className="field-error">
                  No units in the published locations config.
                </span>
              )}
              {fieldErrors.bedUnit && (
                <span className="field-error" role="alert">{fieldErrors.bedUnit}</span>
              )}
            </label>
            <label className="field">
              Room
              <input
                aria-label="New bed room"
                value={bedForm.room}
                onChange={(e) => setBedForm({ ...bedForm, room: e.target.value })}
              />
            </label>
            <label className="field">
              Bed label
              <input
                aria-label="New bed label"
                value={bedForm.bed}
                onChange={(e) => setBedForm({ ...bedForm, bed: e.target.value })}
              />
            </label>
            <button type="submit" disabled={busy}>Add bed to draft</button>
          </form>

          <form
      noValidate
      className="inline-form"
      aria-label="Rename location"
            onSubmit={(e) => {
              e.preventDefault();
              if (!renameForm.code) {
                setFieldErrors({ rename: "Choose a location to rename." });
                return;
              }
              if (!renameForm.display.trim()) {
                setFieldErrors({ renameDisplay: "A new name is required." });
                return;
              }
              setFieldErrors({});
              void compose(
                { op: "rename", code: renameForm.code, display: renameForm.display.trim() },
                `Rename of ${renameForm.code} drafted`,
              );
              setRenameForm({ code: "", display: "" });
            }}
          >
            <h4>Rename (draft)</h4>
            <label className="field">
              Location *
              <select
                aria-label="Rename location"
                value={renameForm.code}
                disabled={locations.length === 0}
                onChange={(e) => setRenameForm({ ...renameForm, code: e.target.value })}
              >
                <option value="">Select location…</option>
                {locations.map((l) => (
                  <option key={l.code} value={l.code}>
                    {l.code} — {l.display ?? l.code}
                  </option>
                ))}
              </select>
              {fieldErrors.rename && (
                <span className="field-error" role="alert">{fieldErrors.rename}</span>
              )}
            </label>
            <label className="field">
              New name *
              <input
                aria-label="Rename new name"
                value={renameForm.display}
                onChange={(e) => setRenameForm({ ...renameForm, display: e.target.value })}
              />
              {fieldErrors.renameDisplay && (
                <span className="field-error" role="alert">{fieldErrors.renameDisplay}</span>
              )}
            </label>
            <button type="submit" disabled={busy}>Rename in draft</button>
          </form>

          <form
      noValidate
      className="inline-form"
      aria-label="Retire location"
            onSubmit={(e) => {
              e.preventDefault();
              if (!retireCode) {
                setFieldErrors({ retire: "Choose a location to retire." });
                return;
              }
              setFieldErrors({});
              void compose(
                { op: "retire", code: retireCode },
                `Retirement of ${retireCode} drafted`,
              );
              setRetireCode("");
            }}
          >
            <h4>Retire (draft)</h4>
            <label className="field">
              Location *
              <select
                aria-label="Retire location"
                value={retireCode}
                disabled={locations.length === 0}
                onChange={(e) => setRetireCode(e.target.value)}
              >
                <option value="">Select location…</option>
                {locations
                  .filter((l) => l.active)
                  .map((l) => (
                    <option key={l.code} value={l.code}>
                      {l.code} — {l.display ?? l.code}
                    </option>
                  ))}
              </select>
              {fieldErrors.retire && (
                <span className="field-error" role="alert">{fieldErrors.retire}</span>
              )}
            </label>
            <button type="submit" disabled={busy}>Retire in draft</button>
            <span className="hint">
              Retiring keeps the record (active: false) — beds are never
              deleted, and an occupied bed cannot be retired.
            </span>
          </form>
        </>
      )}
    </section>
  );
}
