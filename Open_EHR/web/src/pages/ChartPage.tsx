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
} from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

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
}

/** Patient chart — the patient plus every chart section loaded with the
 *  real FHIR search endpoints, scoped with ?patient=<id>. */
export function ChartPage({ client, patientId }: Props) {
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
    </section>
  );
}
