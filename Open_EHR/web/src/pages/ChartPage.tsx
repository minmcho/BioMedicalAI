import { useEffect, useState } from "react";
import { fetchWristbandLabel } from "../api/barcodes";
import { ApiError, type ApiClient } from "../api/client";
import { saveDownloadedFile } from "../api/download";
import { codeLabel, observationValue, patientName, refLabel, resourceDate, resourceStatus } from "../api/display";
import type {
  AllergyIntolerance,
  Condition,
  DiagnosticReport,
  Encounter,
  MedicationRequest,
  Observation,
  Patient,
  Procedure,
  Role,
} from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { ModuleHeader } from "../components/ModuleHeader";
import { PatientEditForm } from "../components/PatientForms";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";
import { AdmissionTab } from "./chart/AdmissionTab";
import { AllergiesTab, allergyPosture } from "./chart/AllergiesTab";
import { FlowsheetTab } from "./chart/FlowsheetTab";
import { MarTab } from "./chart/MarTab";
import { NotesTab } from "./chart/NotesTab";
import { OrdersTab } from "./chart/OrdersTab";

interface ChartData {
  patient: Patient;
  conditions: Condition[];
  medications: MedicationRequest[];
  observations: Observation[];
  reports: DiagnosticReport[];
  procedures: Procedure[];
  encounters: Encounter[];
  allergies: AllergyIntolerance[];
}

interface Props {
  client: ApiClient;
  patientId: string;
  role?: Role;
}

type ChartTab = "overview" | "allergies" | "orders" | "notes" | "flowsheet" | "mar" | "admission";

const TABS: { key: ChartTab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "allergies", label: "Allergies" },
  { key: "orders", label: "Orders" },
  { key: "notes", label: "Notes" },
  { key: "flowsheet", label: "Flowsheet" },
  { key: "mar", label: "MAR" },
  { key: "admission", label: "Admission" },
];

/** Patient chart — the patient plus every chart section loaded with the
 *  real FHIR search endpoints, scoped with ?patient=<id>. Phase 5 adds
 *  tabs for orders, notes, flowsheets and the MAR; each tab loads its
 *  own data only when opened, and write actions follow the runtime
 *  permissions matrix for the signed-in role. */
function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

export function ChartPage({ client, patientId, role }: Props) {
  const [tab, setTab] = useState<ChartTab>("overview");
  const [wristbandBusy, setWristbandBusy] = useState(false);
  const [wristbandError, setWristbandError] = useState<string | null>(null);
  const [wristbandNotice, setWristbandNotice] = useState<string | null>(null);
  const [editingPatient, setEditingPatient] = useState(false);
  const [chartTick, setChartTick] = useState(0);
  const { permissions } = useRuntimeConfig();
  const canPlaceOrders = role ? permissions["orders.place"]?.[role] === true : false;
  const canWriteNotes = role ? permissions["notes.write"]?.[role] === true : false;
  const canWorkInpatient = role ? permissions["inpatient.work"]?.[role] === true : false;
  // Front Door pass: demographics edit rides the FHIR write gate.
  const canEditPatient = role ? permissions["fhir.write"]?.[role] === true : false;

  // Wave A (CHART-B2): allergy edits in the tab update this local
  // copy so the banner below reflects them without a full chart
  // reload; it resets whenever the chart itself (re)loads.
  const [allergyOverride, setAllergyOverride] =
    useState<AllergyIntolerance[] | null>(null);
  useEffect(() => {
    setAllergyOverride(null);
  }, [patientId, chartTick]);

  const { data, loading, error } = useAsync<ChartData>(async () => {
    const patient = await client.get<Patient>("Patient", patientId);
    const params = { patient: patientId };
    const [conditions, medications, observations, reports, procedures, encounters, allergies] =
      await Promise.all([
        client.search<Condition>("Condition", params),
        client.search<MedicationRequest>("MedicationRequest", params),
        client.search<Observation>("Observation", params),
        client.search<DiagnosticReport>("DiagnosticReport", params),
        client.search<Procedure>("Procedure", params),
        client.search<Encounter>("Encounter", params),
        client.search<AllergyIntolerance>("AllergyIntolerance", params),
      ]);
    return { patient, conditions, medications, observations, reports, procedures, encounters, allergies };
  }, [client, patientId, chartTick]);

  if (loading) return <Loading label="Loading chart…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No chart data." />;

  const { patient } = data;

  const printWristband = () => {
    setWristbandBusy(true);
    setWristbandError(null);
    setWristbandNotice(null);
    void (async () => {
      try {
        const file = await fetchWristbandLabel(client, patientId);
        saveDownloadedFile(file);
        setWristbandNotice(
          `Wristband label downloaded (${file.filename}). Print was audited.`,
        );
      } catch (err) {
        setWristbandError(`Print wristband failed: ${errorText(err)}`);
      } finally {
        setWristbandBusy(false);
      }
    })();
  };

  return (
    <section aria-label="Patient chart">
      <ModuleHeader
        title={patientName(patient)}
        purpose={
          `${patient.id}` +
          `${patient.gender ? ` · ${patient.gender}` : ""}` +
          `${patient.birthDate ? ` · born ${patient.birthDate}` : ""}` +
          `${patient.active === false ? " · entered in error" : ""}`
        }
        primaryAction={{
          label: "Print wristband",
          onClick: printWristband,
          disabled: wristbandBusy,
        }}
      />
      {wristbandNotice && (
        <p className="hint" role="status">{wristbandNotice}</p>
      )}
      {wristbandError && (
        <div className="state state-error" role="alert">{wristbandError}</div>
      )}
      {(() => {
        // Wave A (CHART-B2): the allergy banner — visible across the
        // whole chart, sourced from the same AllergyIntolerance list
        // the Allergies tab edits (single visual source).
        const allergies = allergyOverride ?? data.allergies;
        const posture = allergyPosture(allergies);
        if (posture.kind === "active") {
          return (
            <div className="allergy-banner allergy-banner-active" role="alert">
              Allergies: {posture.names.join(", ")}
            </div>
          );
        }
        if (posture.kind === "nkda") {
          return (
            <p className="allergy-banner allergy-banner-nkda" role="status">
              Allergies: No known allergies (NKDA)
            </p>
          );
        }
        return (
          <p className="allergy-banner allergy-banner-unknown" role="status">
            Allergies: not recorded
          </p>
        );
      })()}
      {canEditPatient && !editingPatient && (
        <p>
          <button
            type="button"
            className="linklike"
            onClick={() => setEditingPatient(true)}
          >
            Edit patient details
          </button>
        </p>
      )}
      {canEditPatient && editingPatient && (
        <section aria-label="Edit patient details">
          <h3>Edit patient details</h3>
          <PatientEditForm
            client={client}
            patient={patient}
            onSaved={() => {
              setEditingPatient(false);
              setChartTick((t) => t + 1);
            }}
            onCancel={() => setEditingPatient(false)}
          />
        </section>
      )}

      <Tabs
        ariaLabel="Chart sections"
        tabs={TABS}
        activeKey={tab}
        onChange={(k) => setTab(k as ChartTab)}
        panels={{
          allergies: (
            <AllergiesTab
              client={client}
              patientId={patientId}
              canWrite={canEditPatient}
              allergies={allergyOverride ?? data.allergies}
              onChanged={setAllergyOverride}
            />
          ),
          orders: (
            <OrdersTab client={client} patientId={patientId} canPlace={canPlaceOrders} />
          ),
          notes: (
            <NotesTab client={client} patientId={patientId} canWrite={canWriteNotes} />
          ),
          flowsheet: (
            <FlowsheetTab client={client} patientId={patientId} canWrite={canWriteNotes} />
          ),
          mar: <MarTab client={client} patientId={patientId} />,
          admission: (
            <AdmissionTab client={client} patientId={patientId} canWork={canWorkInpatient} />
          ),
          overview: (
        <>
      <h3>Conditions</h3>
      {data.conditions.length === 0 ? (
        <Empty label="No conditions recorded." />
      ) : (
        <ul>
          {data.conditions.map((c) => (
            <li key={c.id}>{codeLabel(c.code)}</li>
          ))}
        </ul>
      )}

      <h3>Medications</h3>
      {data.medications.length === 0 ? (
        <Empty label="No medication requests recorded." />
      ) : (
        <ul>
          {data.medications.map((m) => (
            <li key={m.id}>
              {m.medicationCodeableConcept
                ? codeLabel(m.medicationCodeableConcept)
                : refLabel(m.medicationReference)}
              {m.status ? ` — ${m.status}` : ""}
              {m.intent ? ` (${m.intent})` : ""}
            </li>
          ))}
        </ul>
      )}

      <h3>Observations</h3>
      {data.observations.length === 0 ? (
        <Empty label="No observations recorded." />
      ) : (
        <ul>
          {data.observations.map((o) => (
            <li key={o.id}>
              {codeLabel(o.code)}: <strong>{observationValue(o)}</strong>
              {o.effectiveDateTime ? ` · ${resourceDate(o)}` : ""}
            </li>
          ))}
        </ul>
      )}

      <h3>Diagnostic reports</h3>
      {data.reports.length === 0 ? (
        <Empty label="No diagnostic reports recorded." />
      ) : (
        <ul>
          {data.reports.map((r) => (
            <li key={r.id}>
              {codeLabel(r.code)} — {resourceStatus(r)}
              {r.effectiveDateTime ? ` · ${resourceDate(r)}` : ""}
              {r.conclusion ? ` — ${r.conclusion}` : ""}
            </li>
          ))}
        </ul>
      )}

      <h3>Procedures</h3>
      {data.procedures.length === 0 ? (
        <Empty label="No procedures recorded." />
      ) : (
        <ul>
          {data.procedures.map((p) => (
            <li key={p.id}>
              {codeLabel(p.code)}
              {p.performedDateTime ? ` · ${resourceDate(p)}` : ""}
            </li>
          ))}
        </ul>
      )}

      <h3>Encounters</h3>
      {data.encounters.length === 0 ? (
        <Empty label="No encounters recorded." />
      ) : (
        <ul>
          {data.encounters.map((e) => (
            <li key={e.id}>
              {resourceStatus(e)}
              {e.period?.start ? ` · ${resourceDate(e)}` : ""}
            </li>
          ))}
        </ul>
      )}
        </>
          ),
        }}
      />
    </section>
  );
}
