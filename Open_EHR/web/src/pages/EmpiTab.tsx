import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 16 (WS-G1 / K2) — EMPI merge governance, mounted by the
// coordinator as a tab inside the Data Platform page (where the
// Phase 14 dedup workqueue already lives). Governance is the point:
// a merge needs a proposal AND a second steward's decision (the
// proposer can never approve their own request — the server refuses
// with 403), execution requires an explicit confirm, and unmerge
// restores BOTH records from the snapshots captured in the merge
// cross-reference. Nothing on this tab merges by itself.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface Candidate {
  id: number;
  patient_a: string;
  patient_b: string;
  score: number;
  status: string;
  origin?: string;
  reasons?: { origin?: string };
}

interface MergeRequest {
  id: number;
  source_patient_id: string;
  survivor_patient_id: string;
  status: string;
  proposed_by: string;
  approved_by: string | null;
  executed_by: string | null;
  note: string | null;
}

function requestTone(status: string): StatusTone {
  if (status === "executed") return "ok";
  if (status === "approved") return "info";
  if (status === "rejected" || status === "reversed") return "bad";
  return "warn";
}

export function EmpiTab({ client }: Props) {
  const candidates = useAsync<{ items: Candidate[]; total: number; note?: string }>(
    () => client.request("/empi/candidates"),
    [client],
  );
  const requests = useAsync<{ items: MergeRequest[]; total: number }>(
    () => client.request("/empi/requests"),
    [client],
  );
  const [manual, setManual] = useState({ sourceId: "", survivorId: "" });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function reloadAll() {
    candidates.reload();
    requests.reload();
  }

  async function addManualCandidate() {
    setBusy(true);
    setError(null);
    try {
      await client.request("/empi/candidates", {
        method: "POST",
        body: JSON.stringify(manual),
      });
      setManual({ sourceId: "", survivorId: "" });
      reloadAll();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <h3>EMPI — merge governance</h3>
      <p className="hint">
        Duplicate candidates from the dedup scan are review items only. A merge happens in three
        governed steps: a steward proposes, a SECOND steward approves or rejects (the proposer
        cannot decide their own request), and execution needs an explicit confirmation. Unmerge
        restores both patients from the pre-merge snapshots stored in the merge cross-reference.
      </p>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}

      <h4>Merge candidates</h4>
      {candidates.loading && <Loading label="Loading dedup candidates…" />}
      {candidates.error != null && <ErrorView error={candidates.error} />}
      {candidates.data && candidates.data.items.length === 0 && (
        <Empty label="No dedup candidates are waiting. The dedup scan proposes pairs here; nothing merges automatically." />
      )}
      {candidates.data?.items.map((cand) => (
        <CandidateRow key={cand.id} client={client} candidate={cand} onChanged={reloadAll} />
      ))}
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void addManualCandidate();
        }}
      >
        <h4>Add a candidate pair manually</h4>
        <label>
          Source patient ID
          <input
            value={manual.sourceId}
            onChange={(e) => setManual({ ...manual, sourceId: e.target.value })}
            required
          />
        </label>
        <label>
          Survivor patient ID
          <input
            value={manual.survivorId}
            onChange={(e) => setManual({ ...manual, survivorId: e.target.value })}
            required
          />
        </label>
        <button type="submit" disabled={busy || !manual.sourceId || !manual.survivorId}>
          Add candidate (does not merge)
        </button>
      </form>

      <h4>Merge requests</h4>
      {requests.loading && <Loading label="Loading merge requests…" />}
      {requests.error != null && <ErrorView error={requests.error} />}
      {requests.data && requests.data.items.length === 0 && (
        <Empty label="No merge requests yet. Propose one from a candidate above." />
      )}
      {requests.data?.items.map((req) => (
        <RequestRow key={req.id} client={client} request={req} onChanged={reloadAll} />
      ))}
    </div>
  );
}

function CandidateRow({
  client,
  candidate,
  onChanged,
}: {
  client: ApiClient;
  candidate: Candidate;
  onChanged: () => void;
}) {
  const [sourceId, setSourceId] = useState(candidate.patient_a);
  const [survivorId, setSurvivorId] = useState(candidate.patient_b);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function propose() {
    setBusy(true);
    setError(null);
    try {
      await client.request("/empi/requests", {
        method: "POST",
        body: JSON.stringify({ candidateId: candidate.id, sourceId, survivorId, fieldChoices: {} }),
      });
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p>
        <strong>
          Patient/{candidate.patient_a} ↔ Patient/{candidate.patient_b}
        </strong>{" "}
        <StatusBadge tone="info">match score {candidate.score}</StatusBadge>{" "}
        <StatusBadge tone="neutral">{candidate.reasons?.origin ?? candidate.origin ?? "dedup-scan"}</StatusBadge>
      </p>
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void propose();
        }}
      >
        <label>
          Source (will be merged away)
          <select value={sourceId} onChange={(e) => setSourceId(e.target.value)}>
            <option value={candidate.patient_a}>Patient/{candidate.patient_a}</option>
            <option value={candidate.patient_b}>Patient/{candidate.patient_b}</option>
          </select>
        </label>
        <label>
          Survivor (keeps the chart)
          <select value={survivorId} onChange={(e) => setSurvivorId(e.target.value)}>
            <option value={candidate.patient_a}>Patient/{candidate.patient_a}</option>
            <option value={candidate.patient_b}>Patient/{candidate.patient_b}</option>
          </select>
        </label>
        <button type="submit" disabled={busy || sourceId === survivorId}>
          Propose merge
        </button>
        {sourceId === survivorId && (
          <span className="field-error">Source and survivor must be different patients.</span>
        )}
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </form>
    </div>
  );
}

function RequestRow({
  client,
  request,
  onChanged,
}: {
  client: ApiClient;
  request: MergeRequest;
  onChanged: () => void;
}) {
  const [confirmChecked, setConfirmChecked] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function act(path: string, body: Record<string, unknown> = {}) {
    setBusy(true);
    setError(null);
    try {
      await client.request(path, { method: "POST", body: JSON.stringify(body) });
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="card">
      <p>
        <strong>
          Patient/{request.source_patient_id} → Patient/{request.survivor_patient_id}
        </strong>{" "}
        <StatusBadge tone={requestTone(request.status)}>{request.status}</StatusBadge>
      </p>
      <p className="hint">
        Proposed by {request.proposed_by}
        {request.approved_by ? ` · approved by ${request.approved_by}` : ""}
        {request.executed_by ? ` · executed by ${request.executed_by}` : ""}
        {request.note ? ` · note: ${request.note}` : ""}
      </p>
      {request.status === "proposed" && (
        <p>
          <button type="button" disabled={busy} onClick={() => void act(`/empi/requests/${request.id}/approve`)}>
            Approve (second steward)
          </button>{" "}
          <button type="button" disabled={busy} onClick={() => void act(`/empi/requests/${request.id}/reject`)}>
            Reject (second steward)
          </button>
          <span className="hint">
            {" "}
            The proposer cannot approve or reject their own request — the server refuses with 403.
          </span>
        </p>
      )}
      {request.status === "approved" && (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void act(`/empi/requests/${request.id}/execute`, { confirm: true });
          }}
        >
          <label>
            <input
              type="checkbox"
              checked={confirmChecked}
              onChange={(e) => setConfirmChecked(e.target.checked)}
            />{" "}
            I confirm: merge Patient/{request.source_patient_id} into Patient/
            {request.survivor_patient_id}. The source becomes inactive and links to the survivor;
            both records are snapshotted first so the merge can be reversed.
          </label>
          <button type="submit" disabled={busy || !confirmChecked}>
            Execute merge
          </button>
        </form>
      )}
      {request.status === "executed" && (
        <p>
          <button type="button" disabled={busy} onClick={() => void act(`/empi/requests/${request.id}/unmerge`)}>
            Unmerge — restore both records
          </button>
          <span className="hint">
            {" "}
            Restores both patients byte-for-byte from the snapshots in the merge cross-reference
            (source active again, link removed); the cross-reference itself is kept as history.
          </span>
        </p>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}
