import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  UR_CRITERIA_FIXTURE_LABEL,
  advisorReviewUrCase,
  createUrCase,
  getUrCase,
  listUrCases,
  reviewUrCase,
  urCriteria,
  urDenialRisk,
  type URCase,
} from "../api/accessServices";
import type { Role } from "../api/types";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 15 (BD-21, backend/app/access_services.py): utilisation
// review worklist + case detail. Review states run pending ->
// in-review -> approved | denial-risk | escalated; a denial-risk
// case can only move via a physician-advisor second review, and the
// advisor must differ from the first reviewer (the server 409s the
// same person — surfaced verbatim below). Criteria are governed
// config (terminology/ur-criteria) whose seeded concepts are
// SITE-FIXTURE examples; that label travels with the criteria
// wherever they are shown.

const UR_STATUSES = ["pending", "in-review", "approved", "denial-risk", "escalated"];

/** First-review transitions the server allows from each state
 *  (denial-risk and approved are terminal for the first reviewer). */
const REVIEW_TRANSITIONS: Record<string, string[]> = {
  pending: ["in-review"],
  "in-review": ["approved", "denial-risk", "escalated"],
  escalated: ["in-review"],
  "denial-risk": [],
  approved: [],
};

const ADVISOR_DECISIONS = ["approved", "denial-risk", "escalated"];

function toneFor(status: string): StatusTone {
  if (status === "approved") return "ok";
  if (status === "denial-risk") return "bad";
  if (status === "escalated") return "warn";
  if (status === "in-review") return "info";
  return "neutral";
}

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

export function URPanel({ client }: Props) {
  const [tick, setTick] = useState(0);
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const hasRequest = typeof client.request === "function";
  const casesState = useAsync(
    async () =>
      hasRequest
        ? listUrCases(client, statusFilter || undefined)
        : { items: [], total: 0 },
    [client, statusFilter, tick],
  );
  const feedState = useAsync(
    async () => (hasRequest ? urDenialRisk(client) : null),
    [client, tick],
  );
  const criteriaState = useAsync(() => urCriteria(client), [client]);
  const detailState = useAsync(
    () =>
      selectedId && hasRequest
        ? getUrCase(client, selectedId)
        : Promise.resolve(null),
    [client, selectedId, tick],
  );

  const cases: URCase[] = casesState.data?.items ?? [];
  const feed = feedState.data;
  const concepts = (criteriaState.data?.concepts ?? []).filter(
    (c) => c.active !== false,
  );
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

  return (
    <div>
      <h3>Utilisation review</h3>
      <p className="hint">
        UR cases opened for admissions, reviewed against the published
        Studio criteria config (<code>terminology/ur-criteria</code>).{" "}
        {UR_CRITERIA_FIXTURE_LABEL}. A first reviewer who marks a case{" "}
        <strong>denial-risk</strong> triggers a required physician-advisor
        second review by a <em>different</em> reviewer.
      </p>
      {notice && <p className="state state-empty" role="status">{notice}</p>}
      {actionError && <div className="state state-error" role="alert">{actionError}</div>}

      {/* Denial-risk JOIN FEED — informational only. Denial workqueue
          state (assignee, notes, outcomes) lives in the existing
          Denials workqueue on the Claims & billing tab; this strip
          feeds that queue, it is not a second queue. */}
      <div className="card" aria-label="Denial-risk feed">
        <h4>Denial-risk feed — for the billing denials queue</h4>
        {feedState.loading && <Loading label="Loading denial-risk feed…" />}
        {feedState.error != null && <ErrorView error={feedState.error} />}
        {feed && (
          <>
            <p className="hint">
              {feed.note} Joins with: {feed.joins_with} (join keys:{" "}
              {feed.join_keys.join(", ")}). To work these denials, open the{" "}
              <strong>Claims &amp; billing</strong> tab and use the existing{" "}
              <strong>Denials workqueue</strong> there — this strip is a feed
              into that queue, not a separate queue.
            </p>
            {feed.items.length === 0 ? (
              <Empty label="No cases currently in denial-risk." />
            ) : (
              <ul className="result-list">
                {feed.items.map((c) => (
                  <li key={c.case_id} className="result-row">
                    <span>{c.patient_ref}</span>
                    <span className="result-meta">
                      {c.claim_ref ?? "no claim"} · first reviewer{" "}
                      {c.first_reviewer ?? "—"}
                    </span>
                    <StatusBadge tone="bad">denial-risk</StatusBadge>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </div>

      <CreateCaseForm
        client={client}
        concepts={concepts}
        busy={busy}
        perform={perform}
        onCreated={(id) => setSelectedId(id)}
      />

      <div className="toolbar">
        <label>
          Status{" "}
          <select
            aria-label="UR status filter"
            value={statusFilter}
            onChange={(e) => setStatusFilter(e.target.value)}
          >
            <option value="">All statuses</option>
            {UR_STATUSES.map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
      </div>

      <h4>UR worklist</h4>
      {casesState.loading && <Loading label="Loading UR cases…" />}
      {casesState.error != null && <ErrorView error={casesState.error} />}
      {!casesState.loading && !casesState.error && cases.length === 0 && (
        <Empty label="No UR cases in this view." />
      )}
      {cases.length > 0 && (
        <table className="table">
          <thead>
            <tr>
              <th>Patient</th>
              <th>Encounter</th>
              <th>Claim</th>
              <th>Status</th>
              <th>First reviewer</th>
              <th>Advisor</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.case_id}>
                <td>{c.patient_ref}</td>
                <td>{c.encounter_ref ?? "—"}</td>
                <td>{c.claim_ref ?? "—"}</td>
                <td><StatusBadge tone={toneFor(c.status)}>{c.status}</StatusBadge></td>
                <td>{c.first_reviewer ?? "—"}</td>
                <td>{c.advisor_reviewer ?? "—"}</td>
                <td>
                  <button type="button" onClick={() => setSelectedId(c.case_id)}>
                    View case
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {selectedId && (
        <>
          <h4>Case detail</h4>
          {detailState.loading && <Loading label="Loading UR case…" />}
          {detailState.error != null && <ErrorView error={detailState.error} />}
          {detail && (
            <CaseDetail
              key={`${detail.case_id}-${detail.status}-${detail.updated_at ?? ""}`}
              client={client}
              detail={detail}
              concepts={concepts}
              busy={busy}
              perform={perform}
            />
          )}
        </>
      )}
    </div>
  );
}

function CreateCaseForm({
  client,
  concepts,
  busy,
  perform,
  onCreated,
}: {
  client: ApiClient;
  concepts: Array<{ code: string; display: string }>;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
  onCreated: (id: string) => void;
}) {
  const [patientId, setPatientId] = useState("");
  const [encounterId, setEncounterId] = useState("");
  const [claimId, setClaimId] = useState("");
  const [criteriaCode, setCriteriaCode] = useState("");

  return (
    <form
      className="toolbar"
      onSubmit={(e) => {
        e.preventDefault();
        const raw = patientId.trim();
        if (!raw) return;
        void perform("Open UR case", async () => {
          const created = await createUrCase(client, {
            patient: raw.includes("/") ? raw : `Patient/${raw}`,
            ...(encounterId.trim() ? { encounter_id: encounterId.trim() } : {}),
            ...(claimId.trim() ? { claim_id: claimId.trim() } : {}),
            ...(criteriaCode ? { criteria_code: criteriaCode } : {}),
          });
          onCreated(created.case_id);
        });
      }}
    >
      <label>
        Patient id{" "}
        <input
          aria-label="UR patient id"
          value={patientId}
          onChange={(e) => setPatientId(e.target.value)}
          placeholder="pat-1"
        />
      </label>
      <label>
        Encounter id{" "}
        <input
          aria-label="UR encounter id"
          value={encounterId}
          onChange={(e) => setEncounterId(e.target.value)}
          placeholder="enc-1"
        />
      </label>
      <label>
        Claim id{" "}
        <input
          aria-label="UR claim id"
          value={claimId}
          onChange={(e) => setClaimId(e.target.value)}
          placeholder="claim-1"
        />
      </label>
      <label>
        Criteria{" "}
        <select
          aria-label="UR criteria for new case"
          value={criteriaCode}
          onChange={(e) => setCriteriaCode(e.target.value)}
        >
          <option value="">No criteria yet</option>
          {concepts.map((c) => (
            <option key={c.code} value={c.code}>{c.display}</option>
          ))}
        </select>
      </label>
      <button type="submit" disabled={busy || !patientId.trim()}>
        Open UR case
      </button>
      <span className="hint">{UR_CRITERIA_FIXTURE_LABEL}.</span>
    </form>
  );
}

function CaseDetail({
  client,
  detail,
  concepts,
  busy,
  perform,
}: {
  client: ApiClient;
  detail: URCase;
  concepts: Array<{
    code: string;
    display: string;
    description?: string;
    requiredEvidence?: string[];
  }>;
  busy: boolean;
  perform: (label: string, fn: () => Promise<unknown>) => Promise<void>;
}) {
  const transitions = REVIEW_TRANSITIONS[detail.status] ?? [];
  const [decision, setDecision] = useState(transitions[0] ?? "");
  const [criteriaCode, setCriteriaCode] = useState(detail.criteria_code ?? "");
  const [evidence, setEvidence] = useState<string[]>(detail.evidence_checked ?? []);
  const [advisorDecision, setAdvisorDecision] = useState("approved");

  useEffect(() => {
    if (decision && !transitions.includes(decision)) {
      setDecision(transitions[0] ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.status]);

  const concept = concepts.find((c) => c.code === criteriaCode);
  const requiredEvidence = concept?.requiredEvidence ?? [];
  const deciding = ["approved", "denial-risk", "escalated"].includes(decision);

  function toggleEvidence(field: string, checked: boolean) {
    setEvidence((prev) =>
      checked ? [...prev, field] : prev.filter((f) => f !== field),
    );
  }

  return (
    <div>
      <p>
        <strong>{detail.patient_ref}</strong> · status{" "}
        <StatusBadge tone={toneFor(detail.status)}>{detail.status}</StatusBadge>
        {detail.encounter_ref ? ` · encounter ${detail.encounter_ref}` : ""}
        {detail.claim_ref ? ` · claim ${detail.claim_ref}` : ""}
        {detail.first_reviewer ? ` · first reviewer ${detail.first_reviewer}` : ""}
        {detail.advisor_reviewer
          ? ` · advisor ${detail.advisor_reviewer} (${detail.advisor_decision ?? ""})`
          : ""}
      </p>
      {detail.history.length > 0 && (
        <p className="hint">
          History:{" "}
          {detail.history
            .map(
              (h) =>
                `${h.from ?? "∅"} → ${h.to} (${h.by}${h.advisor ? ", advisor" : ""})`,
            )
            .join(" · ")}
        </p>
      )}

      <p className="hint">
        Criteria: {UR_CRITERIA_FIXTURE_LABEL}.
        {concept?.description ? ` ${concept.description}` : ""}
      </p>
      <div className="toolbar">
        <label>
          Criteria set{" "}
          <select
            aria-label="Criteria set"
            value={criteriaCode}
            onChange={(e) => {
              setCriteriaCode(e.target.value);
              setEvidence([]);
            }}
          >
            <option value="">Select criteria…</option>
            {concepts.map((c) => (
              <option key={c.code} value={c.code}>{c.display}</option>
            ))}
          </select>
        </label>
      </div>
      {requiredEvidence.length > 0 && (
        <fieldset>
          <legend>Evidence checked</legend>
          {requiredEvidence.map((field) => (
            <label key={field}>
              <input
                type="checkbox"
                aria-label={`Evidence ${field}`}
                checked={evidence.includes(field)}
                onChange={(e) => toggleEvidence(field, e.target.checked)}
              />{" "}
              {field}
            </label>
          ))}
        </fieldset>
      )}

      {transitions.length > 0 ? (
        <div className="toolbar">
          <label>
            Review decision{" "}
            <select
              aria-label="Review decision"
              value={decision}
              onChange={(e) => setDecision(e.target.value)}
            >
              {transitions.map((t) => (
                <option key={t} value={t}>{t}</option>
              ))}
            </select>
          </label>
          <button
            type="button"
            disabled={busy || !decision || (deciding && !criteriaCode)}
            onClick={() =>
              void perform("Record review", () =>
                reviewUrCase(client, detail.case_id, {
                  to_status: decision,
                  ...(criteriaCode ? { criteria_code: criteriaCode } : {}),
                  ...(evidence.length > 0 ? { evidence_checked: evidence } : {}),
                }),
              )
            }
          >
            Record review
          </button>
        </div>
      ) : (
        <p className="hint">
          {detail.status === "denial-risk"
            ? "This case is in denial-risk: only a physician-advisor second review (below) can move it."
            : "No first-review actions remain for this case."}
        </p>
      )}

      {detail.status === "denial-risk" && (
        <div className="card" aria-label="Physician advisor review">
          <h5>Physician advisor review (required)</h5>
          <p className="hint">
            The advisor must differ from the first reviewer
            {detail.first_reviewer ? ` (${detail.first_reviewer})` : ""}; the
            server rejects a same-person advisor review with a 409.
          </p>
          <div className="toolbar">
            <label>
              Advisor decision{" "}
              <select
                aria-label="Advisor decision"
                value={advisorDecision}
                onChange={(e) => setAdvisorDecision(e.target.value)}
              >
                {ADVISOR_DECISIONS.map((d) => (
                  <option key={d} value={d}>{d}</option>
                ))}
              </select>
            </label>
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                void perform("Advisor review", () =>
                  advisorReviewUrCase(client, detail.case_id, {
                    decision: advisorDecision,
                    ...(criteriaCode ? { criteria_code: criteriaCode } : {}),
                    ...(evidence.length > 0 ? { evidence_checked: evidence } : {}),
                  }),
                )
              }
            >
              Submit advisor review
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
