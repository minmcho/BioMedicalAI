import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { RichTextEditor } from "../components/RichTextEditor";
import { RichTextView } from "../components/RichTextView";
import { htmlToText } from "../components/richtext";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
  /** The radiology study (ImagingStudy id) this panel drafts for. */
  studyId: string;
  /** Optional human label for the study, shown in the panel heading. */
  studyLabel?: string;
}

// Phase 16 (WS-E / K2) — radiology vision-model assist, mounted by the
// coordinator inside the Radiology page. The governed agent DRAFTS;
// the radiologist disposes. Everything the model produced is labelled
// as a draft (never a report), acceptance creates a PRELIMINARY report
// only, and there is deliberately NO control here that finalises or
// verifies a report. When no vision model is configured the agent run
// refuses (503) and the panel shows that honestly — no draft is
// invented.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Draft {
  id: number;
  studyId: string;
  patientRef: string | null;
  findingsDraft: string | null;
  impressionDraft: string | null;
  protocolSuggestion: string | null;
  triageSuggestion: string | null;
  measurements: Array<Record<string, unknown>>;
  priorStudyId: string | null;
  agentRunId: string | null;
  status: "draft" | "accepted" | "edited" | "rejected";
  reportId: string | null;
  createdBy: string;
  skipped_instances?: Array<{ sop_uid: string; reason: string }>;
  framesConverted?: number;
}

function draftTone(status: Draft["status"]): StatusTone {
  if (status === "accepted" || status === "edited") return "ok";
  if (status === "rejected") return "bad";
  return "warn";
}

export function ImagingAssistPanel({ client, studyId, studyLabel }: Props) {
  const drafts = useAsync<{ items?: Draft[] } | Draft[]>(
    () => client.request(`/imaging-assist/studies/${encodeURIComponent(studyId)}/drafts`),
    [client, studyId],
  );
  const [error, setError] = useState<string | null>(null);
  const [offState, setOffState] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const items: Draft[] = Array.isArray(drafts.data)
    ? drafts.data
    : (drafts.data?.items ?? []);

  async function requestAssist() {
    setBusy(true);
    setError(null);
    setOffState(null);
    try {
      await client.request(`/imaging-assist/studies/${encodeURIComponent(studyId)}/draft`, {
        method: "POST",
        body: JSON.stringify({}),
      });
      drafts.reload();
    } catch (err) {
      if (err instanceof ApiError && (err.status === 503 || err.status === 404 || err.status === 409)) {
        // agents' honest-off states: no model configured / agent not
        // published+enabled. Nothing was drafted.
        setOffState(err.detail);
      } else {
        setError(errorText(err));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <h3>Vision assist{studyLabel ? ` — ${studyLabel}` : ""}</h3>
      <p className="hint">
        The governed radiology vision assistant drafts findings from the study&apos;s archived
        DICOM pixels. A draft is <strong>not a report</strong>: accepting one files a{" "}
        <strong>preliminary</strong> report only, and final verification stays with the
        radiologist&apos;s normal report workflow — there is no finalise control here.
      </p>
      <button type="button" disabled={busy} onClick={() => void requestAssist()}>
        Draft with vision assist
      </button>
      {offState && (
        <div className="state state-error" role="alert">
          <strong>Vision assist is off.</strong> {offState} No draft was created. A site enables
          it by publishing the <code>radiology-vision-assistant</code> agent in System Studio with
          a vision model route.
        </div>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      {drafts.loading && <Loading label="Loading assist drafts…" />}
      {drafts.error != null && <ErrorView error={drafts.error} />}
      {drafts.data && items.length === 0 && !offState && (
        <Empty label="No assist drafts for this study yet. Request one above — it needs archived DICOM pixels and a configured vision model." />
      )}
      {items.map((draft) => (
        <DraftView key={draft.id} client={client} draft={draft} onChanged={() => drafts.reload()} />
      ))}
    </div>
  );
}

function DraftView({
  client,
  draft,
  onChanged,
}: {
  client: ApiClient;
  draft: Draft;
  onChanged: () => void;
}) {
  const [editedText, setEditedText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function accept() {
    setBusy(true);
    setError(null);
    try {
      const out = await client.request<{ reportId?: string; report?: { id: string; status: string } }>(
        `/imaging-assist/drafts/${draft.id}/accept-into-report`,
        {
          method: "POST",
          body: JSON.stringify(htmlToText(editedText).trim() ? { editedText } : {}),
        },
      );
      const reportId = out.report?.id ?? out.reportId;
      setNotice(
        `Accepted into a PRELIMINARY report${reportId ? ` (DiagnosticReport/${reportId})` : ""}. It still requires the radiologist's verification through the normal report workflow.`,
      );
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function reject() {
    if (!draft.agentRunId) return;
    setBusy(true);
    setError(null);
    try {
      await client.request(`/agents/runs/${encodeURIComponent(draft.agentRunId)}/disposition`, {
        method: "POST",
        body: JSON.stringify({ disposition: "rejected" }),
      });
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  const pending = draft.status === "draft" && !draft.reportId;

  return (
    <div className="card">
      <p>
        <strong>Draft — not a report</strong>{" "}
        <StatusBadge tone={draftTone(draft.status)}>{draft.status}</StatusBadge>
        <span className="hint"> · drafted by {draft.createdBy} · run {draft.agentRunId ?? "—"}</span>
      </p>
      <div aria-label="Draft findings — not a report">
        <p>
          <em>Draft — not a report.</em>
        </p>
        <h4>Draft findings</h4>
        {draft.findingsDraft ? (
          <RichTextView html={draft.findingsDraft} />
        ) : (
          <p>No findings text in this draft.</p>
        )}
        <h4>Draft impression</h4>
        {draft.impressionDraft ? (
          <RichTextView html={draft.impressionDraft} />
        ) : (
          <p>No impression text in this draft.</p>
        )}
        {draft.protocolSuggestion && (
          <p className="hint">Protocol suggestion: {draft.protocolSuggestion}</p>
        )}
        {draft.triageSuggestion && <p className="hint">Triage suggestion: {draft.triageSuggestion}</p>}
        {draft.measurements.length > 0 && (
          <ul className="result-list">
            {draft.measurements.map((m, i) => (
              <li key={i} className="result-row">
                <span className="result-type">{String(m.name ?? m.label ?? "measurement")}</span>
                <span className="result-meta">
                  {String(m.value ?? "")} {String(m.unit ?? "")}
                </span>
              </li>
            ))}
          </ul>
        )}
        <p className="hint">
          {draft.priorStudyId
            ? `Prior-study comparison: drafted against prior study ${draft.priorStudyId}; its report text was included in the draft prompt for comparison.`
            : "Prior-study comparison: no prior study was available for this patient, so no comparison was made — the draft says nothing about priors."}
        </p>
        {draft.skipped_instances && draft.skipped_instances.length > 0 && (
          <p className="hint">
            Skipped instances (not converted, reasons from the server):{" "}
            {draft.skipped_instances.map((s) => `${s.sop_uid}: ${s.reason}`).join("; ")}
          </p>
        )}
        {draft.reportId && (
          <p role="status">
            Filed as PRELIMINARY report DiagnosticReport/{draft.reportId} — not final, not
            verified.
          </p>
        )}
      </div>
      {pending && (
        <div>
          <label>
            Edited text (optional — used verbatim as the preliminary report conclusion)
            <RichTextEditor
              ariaLabel="Edited text"
              value={editedText}
              onChange={setEditedText}
              minRows={3}
            />
          </label>
          <button type="button" disabled={busy} onClick={() => void accept()}>
            Accept into preliminary report
          </button>{" "}
          <button type="button" disabled={busy || !draft.agentRunId} onClick={() => void reject()}>
            Reject draft
          </button>
          {error && (
            <p className="form-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
      {notice && <p role="status">{notice}</p>}
    </div>
  );
}
