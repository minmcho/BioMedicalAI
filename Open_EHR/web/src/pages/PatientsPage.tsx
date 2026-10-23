import { useState } from "react";
import type { ApiClient } from "../api/client";
import { patientName } from "../api/display";
import type { Patient, RegistrationResult, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { MasterDetail } from "../components/MasterDetail";
import { ModuleHeader, scrollToSection } from "../components/ModuleHeader";
import {
  FrontDeskSteps,
  PatientEditForm,
  RegisterPatientForm,
} from "../components/PatientForms";
import { StatusBadge } from "../components/StatusBadge";
import { ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
  onOpenChart: (patientId: string) => void;
  /** Front-desk flow: after a registration, offer the next step
   *  (booking) for the new patient. */
  onRegistered?: (result: RegistrationResult) => void;
}

/** Patients: search/list, registration and record correction
 *  (Front Door pass). Registration is the governed server flow
 *  (MRN from published config, duplicate check with explicit
 *  confirm); editing is the FHIR Patient update; a wrong record is
 *  marked entered-in-error (active: false) — never deleted. */
export function PatientsPage({ client, role, onOpenChart, onRegistered }: Props) {
  const { permissions } = useRuntimeConfig();
  const canRegister =
    role != null && permissions["registration.work"]?.[role] === true;
  const canWrite =
    role != null && permissions["fhir.write"]?.[role] === true;
  const [query, setQuery] = useState("");
  const [tick, setTick] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [lastRegistered, setLastRegistered] =
    useState<RegistrationResult | null>(null);
  const { data, loading, error } = useAsync<Patient[]>(
    () => client.search<Patient>("Patient"),
    [client, tick],
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
      <ModuleHeader
        icon="users"
        group="Patient Care"
        title="Patients"
        purpose="Find a patient record, register a new patient (front-desk step 1) and open the chart."
        primaryAction={
          canRegister
            ? {
                label: "Start registration",
                onClick: () => scrollToSection("patients-register"),
              }
            : undefined
        }
      />

      {canRegister && (
        <section id="patients-register" aria-label="Register a new patient">
          <h3>Register a new patient</h3>
          <FrontDeskSteps current="register" />
          <p className="hint">
            The MRN is assigned from the published registration format.
            A matching name + date of birth asks for explicit
            confirmation before a second record is created.
          </p>
          <RegisterPatientForm
            client={client}
            onRegistered={(result) => {
              setLastRegistered(result);
              setTick((t) => t + 1);
            }}
          />
          {lastRegistered && onRegistered && (
            <p className="state state-empty" role="status">
              Next step:{" "}
              <button
                type="button"
                onClick={() => onRegistered(lastRegistered)}
              >
                Book an appointment for {lastRegistered.id} →
              </button>
            </p>
          )}
        </section>
      )}

      <label className="field">
        Filter by name, id, or identifier
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="e.g. pat-1"
        />
      </label>
      {/* Phase 15 (workstream F): the patient list is the shared
          MasterDetail — selecting a row shows the summary in the
          detail pane, whose Open-chart action is the existing
          onOpenChart flow; record correction (Edit) lives in the
          detail pane too, so no button nests inside the row button. */}
      <MasterDetail<Patient>
        items={patients}
        getKey={(p) => p.id ?? ""}
        listLabel="Patients"
        emptyText={
          query ? "No patients match the filter." : "No patients found."
        }
        detailEmptyText="Select a patient to see the summary."
        renderRowAction={(p) =>
          p.id ? (
            <button
              type="button"
              className="button-secondary"
              aria-label={`Open chart for ${patientName(p)}`}
              onClick={() => onOpenChart(p.id as string)}
            >
              Open chart
            </button>
          ) : null
        }
        renderListItem={(p) => (
          <>
            <span className="patient-name">{patientName(p)}</span>
            <span className="patient-meta">
              {p.id}
              {p.gender ? ` · ${p.gender}` : ""}
              {p.birthDate ? ` · born ${p.birthDate}` : ""}
              {p.active === false ? " · entered in error" : ""}
            </span>
          </>
        )}
        renderDetail={(p) => (
          <div>
            <h3>
              Patient summary — {patientName(p)} ({p.id})
            </h3>
            <p>Name: {patientName(p)}</p>
            <p>Record: {p.id}</p>
            {p.gender && <p>Gender: {p.gender}</p>}
            {p.birthDate && <p>Born: {p.birthDate}</p>}
            {(p.identifier ?? []).length > 0 && (
              <p>
                Identifiers:{" "}
                {(p.identifier ?? [])
                  .map((i) => i.value ?? "")
                  .filter(Boolean)
                  .join(", ")}
              </p>
            )}
            <p>
              Status:{" "}
              {p.active === false ? (
                <StatusBadge tone="warn">entered in error</StatusBadge>
              ) : (
                "active"
              )}
            </p>
            <div className="row-actions">
              <button
                type="button"
                onClick={() => p.id && onOpenChart(p.id)}
              >
                Open chart
              </button>
              {canWrite && p.id && editingId !== p.id && (
                <button
                  type="button"
                  className="linklike"
                  onClick={() => setEditingId(p.id as string)}
                >
                  Edit
                </button>
              )}
            </div>
            {canWrite && p.id && editingId === p.id && (
              <PatientEditForm
                client={client}
                patient={p}
                onSaved={() => {
                  setEditingId(null);
                  setTick((t) => t + 1);
                }}
                onCancel={() => setEditingId(null)}
              />
            )}
          </div>
        )}
      />
    </section>
  );
}
