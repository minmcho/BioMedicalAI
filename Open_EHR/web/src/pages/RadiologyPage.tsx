import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import { refLabel } from "../api/display";
import type { RadiologyStudyItem, Role } from "../api/types";
import { useRuntimeConfig } from "../auth/runtimeConfig";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role: Role;
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

export function RadiologyPage({ client, role }: Props) {
  const { permissions } = useRuntimeConfig();
  const canWork = permissions["radiology.work"]?.[role] === true;
  const canVerify = permissions["radiology.verify"]?.[role] === true;
  const [tick, setTick] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [orderId, setOrderId] = useState("");
  const [modality, setModality] = useState("CT");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [conclusion, setConclusion] = useState("");

  const studiesState = useAsync(() => client.radiologyStudies(), [client, tick]);
  const modalitiesState = useAsync(() => client.radiologyModalities(), [client]);
  const detailState = useAsync(
    () => (selectedId ? client.radiologyStudy(selectedId) : Promise.resolve(null)),
    [client, selectedId, tick],
  );

  const studies = studiesState.data ?? [];
  const modalities = (modalitiesState.data?.modalities ?? [])
    .filter((m) => m.active !== false);
  const detail = detailState.data;

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

  function renderRow(item: RadiologyStudyItem) {
    const study = item.study;
    const id = study.id as string;
    return (
      <tr key={id}>
        <td>{item.study_uid ? `${item.study_uid.slice(0, 18)}…` : "—"}</td>
        <td>{refLabel(study.subject)}</td>
        <td>{study.modality?.[0]?.code ?? "—"}</td>
        <td>{study.description ?? "—"}</td>
        <td>{study.started ? new Date(study.started).toLocaleString() : "—"}</td>
        <td>
          {item.report
            ? <span className="badge">{item.report.status}</span>
            : "no report"}
        </td>
        <td>
          {item.viewer_url ? (
            <a href={item.viewer_url} target="_blank" rel="noreferrer">
              Open in viewer
            </a>
          ) : (
            <span className="hint">viewer not configured</span>
          )}
        </td>
        <td>
          <button type="button" onClick={() => setSelectedId(id)}>
            Details
          </button>
        </td>
      </tr>
    );
  }

  return (
    <section>
      <h2>Radiology</h2>
      <p className="hint">
        Imaging studies are created from orders (ServiceRequests) with a
        modality validated against the published Studio config
        (terminology/imaging-modalities). <strong>Studies are
        metadata-only in v1: no pixel data is stored in OpenCare</strong> —
        DICOMweb serves study metadata, and “Open in viewer” links out to
        the external OHIF-style viewer whose base URL is Studio
        integration config (integration/dicom-viewer); there is no
        embedded viewer. Reports are drafted as preliminary and become
        final only on radiologist verification.
      </p>
      {notice && (
        <p className="state state-empty" role="status">{notice}</p>
      )}
      {actionError && (
        <div className="state state-error" role="alert">{actionError}</div>
      )}

      {canWork && (
        <form
          className="inline-form"
          onSubmit={(e) => {
            e.preventDefault();
            if (!orderId.trim()) return;
            void perform("Study created", async () => {
              const item = await client.createStudy({
                serviceRequestId: orderId.trim(),
                modality,
              });
              setSelectedId(item.study.id as string);
              setOrderId("");
            });
          }}
        >
          <h3>New study from order</h3>
          <label>
            Service request id{" "}
            <input
              aria-label="Service request id"
              value={orderId}
              onChange={(e) => setOrderId(e.target.value)}
              placeholder="sr-img-1"
            />
          </label>
          <label>
            Modality{" "}
            <select
              aria-label="Modality"
              value={modality}
              onChange={(e) => setModality(e.target.value)}
            >
              {modalities.map((m) => (
                <option key={m.code} value={m.code}>
                  {m.code} — {m.display ?? m.code}
                </option>
              ))}
            </select>
          </label>
          <button type="submit" disabled={busy}>Create study</button>
        </form>
      )}

      <h3>Studies</h3>
      {studiesState.loading && <Loading label="Loading studies…" />}
      {studiesState.error != null && <ErrorView error={studiesState.error} />}
      {!studiesState.loading && !studiesState.error && studies.length === 0 && (
        <Empty label="No imaging studies yet." />
      )}
      {studies.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Study UID</th>
              <th>Patient</th>
              <th>Modality</th>
              <th>Description</th>
              <th>Started</th>
              <th>Report</th>
              <th>Viewer</th>
              <th />
            </tr>
          </thead>
          <tbody>{studies.map(renderRow)}</tbody>
        </table>
      )}

      {selectedId && detailState.loading && <Loading label="Loading study…" />}
      {selectedId && detail && (
        <div>
          <h3>Study detail</h3>
          <p className="hint">
            {detail.study.description ?? "Imaging study"} ·{" "}
            {detail.study.modality?.[0]?.display ?? ""} · Patient{" "}
            {refLabel(detail.study.subject)} · UID {detail.study_uid}
            {" "}· No images — metadata-only study.
          </p>
          {detail.reports.length === 0 && (
            <Empty label="No report drafted for this study yet." />
          )}
          {detail.reports.map((report) => (
            <div key={report.id} className="board-card">
              <div>
                <strong>Report {report.id}</strong>{" "}
                <span className="badge">{report.status}</span>
              </div>
              <p>{(report.conclusion as string | undefined) ?? "—"}</p>
              {canVerify && report.status === "preliminary" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() =>
                    perform("Report verified", () =>
                      client.verifyImagingReport(report.id as string))}
                >
                  Verify report
                </button>
              )}
            </div>
          ))}
          {canWork && !detail.reports.some((r) => r.status === "preliminary") && (
            <form
              className="inline-form"
              onSubmit={(e) => {
                e.preventDefault();
                if (!conclusion.trim()) return;
                void perform("Report drafted", async () => {
                  await client.createImagingReport(selectedId, conclusion.trim());
                  setConclusion("");
                });
              }}
            >
              <label>
                Report conclusion{" "}
                <textarea
                  aria-label="Report conclusion"
                  value={conclusion}
                  onChange={(e) => setConclusion(e.target.value)}
                  rows={3}
                />
              </label>
              <button type="submit" disabled={busy}>Save report draft</button>
            </form>
          )}
        </div>
      )}
    </section>
  );
}
