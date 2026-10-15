import type { ApiClient } from "../api/client";
import { codeLabel, observationValue, resourceDate, resourceStatus } from "../api/display";
import type { CodeableConcept, Condition, FhirResource, MedicationRequest } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface PortalData {
  results: FhirResource[];
  conditions: Condition[];
  medications: MedicationRequest[];
}

interface Props {
  client: ApiClient;
}

/** Patient portal — the signed-in patient's own data only. Results come
 *  from GET /portal/my-results; conditions/medications come from FHIR
 *  search, which the server force-scopes to the caller's own record. */
export function PortalPage({ client }: Props) {
  const { data, loading, error } = useAsync<PortalData>(async () => {
    const [results, conditions, medications] = await Promise.all([
      client.myResults(),
      client.search<Condition>("Condition"),
      client.search<MedicationRequest>("MedicationRequest"),
    ]);
    return { results, conditions, medications };
  }, [client]);

  if (loading) return <Loading label="Loading your results…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="Nothing to show yet." />;

  return (
    <section aria-label="Patient portal">
      <h2>My results</h2>
      {data.results.length === 0 ? (
        <Empty label="No results yet." />
      ) : (
        <ul className="result-list">
          {data.results.map((r) => (
            <li key={`${r.resourceType}-${r.id}`} className="result-row">
              <span className="result-type">{r.resourceType}</span>
              <span>
                {codeLabel((r as { code?: CodeableConcept }).code)}
                {r.resourceType === "Observation" ? `: ${observationValue(r)}` : ""}
              </span>
              <span className="result-meta">
                {resourceStatus(r)} · {resourceDate(r)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <h3>My conditions</h3>
      {data.conditions.length === 0 ? (
        <Empty label="No conditions recorded." />
      ) : (
        <ul>
          {data.conditions.map((c) => (
            <li key={c.id}>{codeLabel(c.code)}</li>
          ))}
        </ul>
      )}

      <h3>My medications</h3>
      {data.medications.length === 0 ? (
        <Empty label="No medication requests recorded." />
      ) : (
        <ul>
          {data.medications.map((m) => (
            <li key={m.id}>
              {m.medicationCodeableConcept ? codeLabel(m.medicationCodeableConcept) : "Medication"}
              {m.status ? ` — ${m.status}` : ""}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
