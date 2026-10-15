import { useState } from "react";
import type { ApiClient } from "../api/client";
import { codeLabel, observationValue, resourceDate, resourceStatus } from "../api/display";
import type { CodeableConcept, FhirResource } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
}

/** Results inbox — GET /fhir/inbox?patient= (staff roles only on the
 *  server; a 403 renders as permission denied, not an empty inbox). */
export function InboxPage({ client }: Props) {
  const [patient, setPatient] = useState("pat-1");
  const [submitted, setSubmitted] = useState("pat-1");

  const { data, loading, error } = useAsync<FhirResource[]>(
    () => client.inbox(submitted),
    [client, submitted],
  );

  return (
    <section>
      <h2>Results inbox</h2>
      <form
        className="inline-form"
        onSubmit={(e) => {
          e.preventDefault();
          setSubmitted(patient.trim());
        }}
      >
        <label className="field">
          Patient id
          <input value={patient} onChange={(e) => setPatient(e.target.value)} placeholder="pat-1" />
        </label>
        <button type="submit">Load results</button>
      </form>

      {loading ? (
        <Loading label="Loading results…" />
      ) : error ? (
        <ErrorView error={error} />
      ) : !data || data.length === 0 ? (
        <Empty label={`No results for patient ${submitted}.`} />
      ) : (
        <ul className="result-list">
          {data.map((r) => (
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
    </section>
  );
}
