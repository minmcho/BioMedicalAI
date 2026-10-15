import { useState } from "react";
import type { ApiClient } from "../api/client";
import { patientName } from "../api/display";
import type { Patient } from "../api/types";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  onOpenChart: (patientId: string) => void;
}

/** Patient search/list — GET /fhir/Patient (all Patients; the backend
 *  search has no name filter, so filtering the list is client-side). */
export function PatientsPage({ client, onOpenChart }: Props) {
  const [query, setQuery] = useState("");
  const { data, loading, error } = useAsync<Patient[]>(
    () => client.search<Patient>("Patient"),
    [client],
  );

  if (loading) return <Loading label="Loading patients…" />;
  if (error) return <ErrorView error={error} />;

  const patients = (data ?? []).filter((p) => {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      patientName(p).toLowerCase().includes(q) ||
      (p.id ?? "").toLowerCase().includes(q) ||
      (p.identifier ?? []).some((i) => (i.value ?? "").toLowerCase().includes(q))
    );
  });

  return (
    <section>
      <h2>Patients</h2>
      <label className="field">
        Filter by name, id, or identifier
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. pat-1"
        />
      </label>
      {patients.length === 0 ? (
        <Empty label={query ? "No patients match the filter." : "No patients found."} />
      ) : (
        <ul className="patient-list">
          {patients.map((p) => (
            <li key={p.id}>
              <button type="button" className="patient-row" onClick={() => p.id && onOpenChart(p.id)}>
                <span className="patient-name">{patientName(p)}</span>
                <span className="patient-meta">
                  {p.id}
                  {p.gender ? ` · ${p.gender}` : ""}
                  {p.birthDate ? ` · born ${p.birthDate}` : ""}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
