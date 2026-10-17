import { useState } from "react";
import type { ApiClient } from "../../api/client";
import type { MarDoseStatus } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

interface Props {
  client: ApiClient;
  patientId: string;
}

/** Format a Date for a datetime-local input (local time, no seconds). */
function toLocalInput(d: Date): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
    `T${pad(d.getHours())}:${pad(d.getMinutes())}`
  );
}

const STATUS_LABEL: Record<MarDoseStatus, string> = {
  given: "Given",
  due: "Due",
  missed: "Missed",
  scheduled: "Scheduled",
};

/** MAR tab: the medication administration record is COMPUTED by the
 *  server from signed/active medication orders and the administration
 *  records written by the pharmacy BCMA scan — administrations are
 *  recorded there, never here; missed is a computed state, not data. */
export function MarTab({ client, patientId }: Props) {
  const [fromInput, setFromInput] = useState(() =>
    toLocalInput(new Date(Date.now() - 24 * 3600_000)),
  );
  const [toInput, setToInput] = useState(() =>
    toLocalInput(new Date(Date.now() + 24 * 3600_000)),
  );
  const [range, setRange] = useState(() => ({
    from: new Date(Date.now() - 24 * 3600_000).toISOString(),
    to: new Date(Date.now() + 24 * 3600_000).toISOString(),
  }));

  const { data, loading, error } = useAsync(
    () => client.marDue(patientId, range.from, range.to),
    [client, patientId, range],
  );

  return (
    <section aria-label="Medication administration record">
      <div className="mar-controls">
        <div className="form-field">
          <label htmlFor="mar-from">From</label>
          <input
            id="mar-from"
            type="datetime-local"
            value={fromInput}
            onChange={(e) => setFromInput(e.target.value)}
          />
        </div>
        <div className="form-field">
          <label htmlFor="mar-to">To</label>
          <input
            id="mar-to"
            type="datetime-local"
            value={toInput}
            onChange={(e) => setToInput(e.target.value)}
          />
        </div>
        <button
          type="button"
          onClick={() =>
            setRange({
              from: new Date(fromInput).toISOString(),
              to: new Date(toInput).toISOString(),
            })
          }
        >
          Apply window
        </button>
      </div>

      {loading && <Loading label="Loading MAR…" />}
      {error != null && <ErrorView error={error} />}
      {data && (
        <>
          <p role="status" className="mar-summary">
            Given {data.summary.given} · Due {data.summary.due} · Missed{" "}
            {data.summary.missed} · Scheduled {data.summary.scheduled}
          </p>
          {data.doses.length === 0 ? (
            <Empty label="No doses scheduled in this window." />
          ) : (
            <table className="lab-table mar-table">
              <thead>
                <tr>
                  <th scope="col">Medication</th>
                  <th scope="col">Due</th>
                  <th scope="col">Window</th>
                  <th scope="col">Status</th>
                </tr>
              </thead>
              <tbody>
                {data.doses.map((dose, i) => (
                  <tr key={`${dose.order_id}-${dose.due_at}-${i}`}>
                    <td>{dose.medication_display ?? dose.medication_code ?? dose.order_id}</td>
                    <td>{dose.due_at.replace("T", " ").slice(0, 16)}</td>
                    <td>
                      {dose.window_start.slice(11, 16)}–{dose.window_end.slice(11, 16)}
                    </td>
                    <td>
                      <span className="badge">{STATUS_LABEL[dose.status]}</span>
                      {dose.status === "given" && dose.administration_id
                        ? ` · ${dose.administration_id}`
                        : ""}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          <p className="mar-note">
            Administrations are recorded with the pharmacy BCMA scan;
            this view only reads. A dose is “missed” when its window has
            closed with no administration recorded — computed, never stored.
          </p>
        </>
      )}
    </section>
  );
}
