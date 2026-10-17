import { useState } from "react";
import type { ApiClient } from "../api/client";
import { codeLabel, observationValue, patientName, refLabel, resourceDate, resourceStatus } from "../api/display";
import type {
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
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";
import { AdmissionTab } from "./chart/AdmissionTab";
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
}

interface Props {
  client: ApiClient;
  patientId: string;
  role?: Role;
}

type ChartTab = "overview" | "orders" | "notes" | "flowsheet" | "mar" | "admission";

const TABS: { key: ChartTab; label: string }[] = [
  { key: "overview", label: "Overview" },
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
export function ChartPage({ client, patientId, role }: Props) {
  const [tab, setTab] = useState<ChartTab>("overview");
  const { permissions } = useRuntimeConfig();
  const canPlaceOrders = role ? permissions["orders.place"]?.[role] === true : false;
  const canWriteNotes = role ? permissions["notes.write"]?.[role] === true : false;
  const canWorkInpatient = role ? permissions["inpatient.work"]?.[role] === true : false;

  const { data, loading, error } = useAsync<ChartData>(async () => {
    const patient = await client.get<Patient>("Patient", patientId);
    const params = { patient: patientId };
    const [conditions, medications, observations, reports, procedures, encounters] =
      await Promise.all([
        client.search<Condition>("Condition", params),
        client.search<MedicationRequest>("MedicationRequest", params),
        client.search<Observation>("Observation", params),
        client.search<DiagnosticReport>("DiagnosticReport", params),
        client.search<Procedure>("Procedure", params),
        client.search<Encounter>("Encounter", params),
      ]);
    return { patient, conditions, medications, observations, reports, procedures, encounters };
  }, [client, patientId]);

  if (loading) return <Loading label="Loading chart…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No chart data." />;

  const { patient } = data;

  return (
    <section aria-label="Patient chart">
      <h2>{patientName(patient)}</h2>
      <p className="patient-meta">
        {patient.id}
        {patient.gender ? ` · ${patient.gender}` : ""}
        {patient.birthDate ? ` · born ${patient.birthDate}` : ""}
      </p>

      <div className="studio-tabs" role="tablist" aria-label="Chart sections">
        {TABS.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            className={tab === t.key ? "studio-tab active" : "studio-tab"}
            onClick={() => setTab(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "orders" && (
        <OrdersTab client={client} patientId={patientId} canPlace={canPlaceOrders} />
      )}
      {tab === "notes" && (
        <NotesTab client={client} patientId={patientId} canWrite={canWriteNotes} />
      )}
      {tab === "flowsheet" && (
        <FlowsheetTab client={client} patientId={patientId} canWrite={canWriteNotes} />
      )}
      {tab === "mar" && <MarTab client={client} patientId={patientId} />}
      {tab === "admission" && (
        <AdmissionTab client={client} patientId={patientId} canWork={canWorkInpatient} />
      )}

      {tab === "overview" && (
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
      )}
    </section>
  );
}
