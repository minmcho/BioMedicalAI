import { useEffect, useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type {
  CaiDisclosure,
  CaiIncident,
  CaiKillSwitch,
  CaiStatement,
  CaiTierRequirements,
  CaiWorklistItem,
} from "../../api/types";
import { StatusBadge, type StatusTone } from "../../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

// Agent validation tab (Phase 16, WS-K1) — the AI model validation
// harness (backend/app/agent_validation.py, BD-41). Governed
// evaluation datasets are run against published agents (recorded mode
// scores stored outputs — harness mechanics, no model; live mode runs
// the real runtime and surfaces its honest 503 verbatim, recording
// nothing). A completed run mints the ValidationRecord the agent
// enablement gate reads. Change control follows the PCCP pattern;
// published agents without a covering record are listed as exceptions.

export interface ValidationConfig {
  requireValidationForEnable: boolean;
  validationValidityDays: number;
  sliceDisparityThreshold: number;
  driftRejectRateThreshold: number;
  driftOutputLengthChangeThreshold: number;
  notes?: string;
}

export interface EvaluationDataset {
  id: number;
  key: string;
  name: string;
  description: string;
  caseCount: number;
  casesHash: string;
  createdBy: string;
  createdAt: string | null;
  cases?: unknown[];
}

export interface EvaluationRun {
  id: number;
  runKey: string;
  datasetKey: string;
  agentKey: string;
  agentConfigVersion: number;
  modelRoute: string;
  mode: string;
  totalCases: number;
  passedCases: number;
  passRate: number;
  disparityFlags: Array<{ detail?: string } | string>;
  actor: string;
  createdAt: string | null;
}

export interface ValidationRecordOut {
  id: number;
  recordKey: string;
  agentKey: string;
  agentConfigVersion: number;
  modelRoute: string;
  datasetKey: string;
  passRate: number;
  issuedAt: string | null;
  expiresAt: string | null;
  issuedBy: string;
}

export interface AgentValidationStatus {
  agentKey: string;
  status: "current" | "expired" | "missing";
  required: boolean;
  publishedVersion: number | null;
  modelRoute: string | null;
  record: ValidationRecordOut | null;
  reason: string;
}

export interface DriftReport {
  agentKey: string;
  status: string;
  baseline: Record<string, unknown> | null;
  recent: Record<string, unknown>;
  flags: Array<{ metric?: string; detail?: string }>;
  reason?: string;
}

export interface ChangeControlOut {
  id: number;
  recordKey: string;
  agentKey: string;
  fromVersion: number;
  toVersion: number;
  intendedChange: string;
  validationPlan: string;
  resultRef: string;
  status: string;
  approver: string;
  createdBy: string;
  createdAt: string | null;
  approvedAt: string | null;
}

export interface ValidationException {
  agentKey: string;
  version: number;
  modelRoute: string;
  reason: string;
}

interface AgentRosterEntry {
  key: string;
  version?: number;
  enabled?: boolean;
}

const CASES_TEMPLATE = `[
  {
    "id": "case-1",
    "prompt": "Summarise this synthetic lab panel for the patient.",
    "recordedOutput": "Synthetic summary mentioning potassium.",
    "checks": [{ "type": "contains", "value": "potassium" }],
    "slices": ["baseline"]
  }
]`;

export function validationStatusTone(status: string): StatusTone {
  switch (status) {
    case "current":
      return "ok";
    case "expired":
      return "warn";
    case "missing":
      return "bad";
    default:
      return "neutral";
  }
}

function errText(err: unknown): string {
  return err instanceof ApiError ? err.message : String(err);
}

function pct(rate: number): string {
  return `${Math.round(rate * 1000) / 10}%`;
}

// ---------------------------------------------------------------------
// C-AI clinical AI governance (/ai-governance) — risk tiers, the
// human-authored validation statement register, the expiry worklist,
// the kill switch, per-agent HTI-1 source-attribute disclosure and
// agent incidents. Every control calls a real endpoint; server
// refusals (422 completeness, 403 four-eyes) surface verbatim.
// ---------------------------------------------------------------------

export const ZERO_TOLERANCE_LABELS: Record<number, string> = {
  1: "Fabricated clinical fact",
  2: "Cross-patient contamination",
  3: "Prohibited finalisation",
  4: "Unperformed-check claims",
  5: "Prohibited-use compliance",
};

/** The five §4.5 prohibited uses seeded on every statement. The code
 *  is locked; the text stays editable, and more rows can be added. */
export const PROHIBITED_USE_SEEDS: Array<{ code: string; text: string }> = [
  {
    code: "no-autonomous-diagnosis",
    text: "No autonomous diagnosis or final reporting",
  },
  {
    code: "no-unreviewed-order",
    text: "No unreviewed order, prescription, or referral",
  },
  {
    code: "no-unreviewed-patient-communication",
    text: "No patient-facing communication sent without a human disposition",
  },
  {
    code: "no-excluded-population",
    text: "No use on a population excluded in the population & data scope",
  },
  {
    code: "no-unlisted-route-tool-modality",
    text: "No use with a model route, tool, or modality not named in this statement",
  },
];

export interface ClaimRow {
  id: string;
  text: string;
}
export interface EvidenceRow {
  datasetKey: string;
  runKey: string;
  mode: string;
}
export interface ProhibitedRow {
  code: string;
  text: string;
  locked: boolean;
}

function asText(v: unknown): string {
  if (typeof v === "string") return v;
  if (v === null || v === undefined) return "";
  return String(v);
}

function currencyTone(status: string): StatusTone {
  switch (status) {
    case "current":
      return "ok";
    case "expired":
    case "missing":
      return "bad";
    case "suspended":
    case "stale":
      return "warn";
    default:
      return "neutral";
  }
}

/** Per-agent source-attribute disclosure (HTI-1 pattern, AI-G7).
 *  Shared: ValidationTab's Disclosure section and AdkTab's
 *  "Source attributes" action render this exact view, honest gaps
 *  ("not recorded") included, exactly as the server returns them. */
export function AgentDisclosure({
  client,
  agentKey,
}: {
  client: ApiClient;
  agentKey: string;
}) {
  const disclosure = useAsync<CaiDisclosure>(
    () =>
      client.request(
        `/ai-governance/agents/${encodeURIComponent(agentKey)}/disclosure`,
      ),
    [client, agentKey],
  );
  if (disclosure.loading) return <Loading label="Loading source attributes…" />;
  if (disclosure.error) return <ErrorView error={disclosure.error} />;
  const data = disclosure.data;
  if (!data || data.attributes.length === 0) {
    return <Empty label={`No source attributes recorded for ${agentKey}.`} />;
  }
  return (
    <table className="studio-table">
      <thead>
        <tr>
          <th>Attribute</th>
          <th>Value</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody>
        {data.attributes.map((a) => (
          <tr key={a.attribute}>
            <td>{a.attribute}</td>
            <td>{a.value === null || a.value === undefined || a.value === "" ? "not recorded" : String(a.value)}</td>
            <td>{a.source}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const PENDING_STATUSES = [
  "pending",
  "pending-approval",
  "pending_approval",
  "submitted",
  "in-review",
  "in_review",
  "awaiting-approval",
];

function StatementRowActions({
  client,
  statement,
  onChanged,
}: {
  client: ApiClient;
  statement: CaiStatement;
  onChanged: () => void;
}) {
  const [kind, setKind] = useState("clinical_reviewer");
  const [conditions, setConditions] = useState("");
  const [suspendReason, setSuspendReason] = useState("");
  const [retireNote, setRetireNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const status = statement.status.toLowerCase();
  const isPending = PENDING_STATUSES.includes(status);

  async function post(path: string, body: Record<string, unknown>) {
    setError(null);
    try {
      await client.request(path, {
        method: "POST",
        body: JSON.stringify(body),
      });
      onChanged();
    } catch (err) {
      // 403 four-eyes refusals and every other server refusal are
      // shown verbatim — never paraphrased, never swallowed.
      setError(errText(err));
    }
  }

  const base = `/ai-governance/statements/${encodeURIComponent(statement.statementId)}`;
  return (
    <div>
      {isPending && (
        <p>
          <label>
            Approval kind{" "}
            <select
              aria-label={`Approval kind for ${statement.statementId}`}
              value={kind}
              onChange={(e) => setKind(e.target.value)}
            >
              <option value="clinical_reviewer">clinical_reviewer</option>
              <option value="governance_approver">governance_approver</option>
              <option value="clinical_owner">clinical_owner</option>
            </select>
          </label>{" "}
          <label>
            Conditions{" "}
            <input
              aria-label={`Approval conditions for ${statement.statementId}`}
              value={conditions}
              onChange={(e) => setConditions(e.target.value)}
              size={24}
            />
          </label>{" "}
          <button
            type="button"
            onClick={() =>
              void post(`${base}/approve`, {
                kind,
                ...(conditions.trim() ? { conditions: conditions.trim() } : {}),
              })
            }
          >
            Approve
          </button>
        </p>
      )}
      {status === "approved" && (
        <p>
          <label>
            Suspend reason{" "}
            <input
              aria-label={`Suspend reason for ${statement.statementId}`}
              value={suspendReason}
              onChange={(e) => setSuspendReason(e.target.value)}
              size={24}
            />
          </label>{" "}
          <button
            type="button"
            disabled={!suspendReason.trim()}
            onClick={() =>
              void post(`${base}/suspend`, { reason: suspendReason.trim() })
            }
          >
            Suspend
          </button>
        </p>
      )}
      {status === "suspended" && (
        <p>
          <button
            type="button"
            onClick={() => void post(`${base}/reinstate`, {})}
          >
            Reinstate
          </button>
        </p>
      )}
      {status !== "retired" && (
        <p>
          <label>
            Retirement note{" "}
            <input
              aria-label={`Retirement note for ${statement.statementId}`}
              value={retireNote}
              onChange={(e) => setRetireNote(e.target.value)}
              size={24}
            />
          </label>{" "}
          <button
            type="button"
            onClick={() => void post(`${base}/retire`, { note: retireNote.trim() })}
          >
            Retire
          </button>
        </p>
      )}
      {error && (
        <p className="error-note" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

export function ValidationTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const config = useAsync<ValidationConfig>(
    () => client.request("/agent-validation/config"),
    [client],
  );
  const datasets = useAsync<{ items: EvaluationDataset[]; total: number }>(
    () => client.request("/agent-validation/datasets"),
    [client, reloadKey],
  );
  const evalRuns = useAsync<{ items: EvaluationRun[]; total: number }>(
    () => client.request("/agent-validation/runs"),
    [client, reloadKey],
  );
  const records = useAsync<{ items: ValidationRecordOut[]; total: number }>(
    () => client.request("/agent-validation/validations"),
    [client, reloadKey],
  );
  const changeControl = useAsync<{ items: ChangeControlOut[]; total: number }>(
    () => client.request("/agent-validation/change-control"),
    [client, reloadKey],
  );
  const exceptions = useAsync<{ items: ValidationException[]; total: number }>(
    () => client.request("/agent-validation/exceptions"),
    [client, reloadKey],
  );
  const roster = useAsync<{ agents: AgentRosterEntry[] }>(
    () => client.request("/agents"),
    [client, reloadKey],
  );
  const statuses = useAsync<AgentValidationStatus[]>(
    async () => {
      const agents = (await client.request<{ agents: AgentRosterEntry[] }>("/agents")).agents ?? [];
      return Promise.all(
        agents.map((a) =>
          client.request<AgentValidationStatus>(
            `/agent-validation/agents/${encodeURIComponent(a.key)}/status`,
          ),
        ),
      );
    },
    [client, reloadKey],
  );

  const [notice, setNotice] = useState<string | null>(null);

  // Dataset create form
  const [dsKey, setDsKey] = useState("");
  const [dsName, setDsName] = useState("");
  const [dsDescription, setDsDescription] = useState("");
  const [dsCases, setDsCases] = useState(CASES_TEMPLATE);
  const [dsError, setDsError] = useState<string | null>(null);

  // Dataset run form
  const [runDatasetId, setRunDatasetId] = useState("");
  const [runAgent, setRunAgent] = useState("");
  const [runMode, setRunMode] = useState<"recorded" | "live">("recorded");
  const [runError, setRunError] = useState<string | null>(null);
  const [runResult, setRunResult] = useState<string | null>(null);

  // Drift
  const [driftAgent, setDriftAgent] = useState("");
  const [drift, setDrift] = useState<DriftReport | null>(null);
  const [driftError, setDriftError] = useState<string | null>(null);

  // Change control propose form
  const [ccAgent, setCcAgent] = useState("");
  const [ccFrom, setCcFrom] = useState("1");
  const [ccTo, setCcTo] = useState("2");
  const [ccChange, setCcChange] = useState("");
  const [ccPlan, setCcPlan] = useState("");
  const [ccError, setCcError] = useState<string | null>(null);

  // ---- C-AI governance state ----
  const tierRequirements = useAsync<CaiTierRequirements>(
    () => client.request("/ai-governance/tier-requirements"),
    [client, reloadKey],
  );
  const worklist = useAsync<{ items: CaiWorklistItem[]; total: number }>(
    () => client.request("/ai-governance/worklist"),
    [client, reloadKey],
  );
  const killSwitch = useAsync<CaiKillSwitch>(
    () => client.request("/ai-governance/kill-switch"),
    [client, reloadKey],
  );
  const incidents = useAsync<{ items: CaiIncident[]; total: number }>(
    () => client.request("/ai-governance/incidents"),
    [client, reloadKey],
  );
  const [govAgent, setGovAgent] = useState("");
  const statements = useAsync<{ items: CaiStatement[]; total: number }>(
    async () => {
      const key =
        govAgent ||
        ((await client.request<{ agents: AgentRosterEntry[] }>("/agents"))
          .agents ?? [])[0]?.key ||
        "";
      if (!key) return { items: [], total: 0 };
      return client.request(
        `/ai-governance/statements?agent=${encodeURIComponent(key)}`,
      );
    },
    [client, reloadKey, govAgent],
  );

  // Statement draft editor (§4.1 fields)
  const [editingId, setEditingId] = useState<string | null>(null);
  const [stOwner, setStOwner] = useState("");
  const [stTier, setStTier] = useState("T2");
  const [stTierRationale, setStTierRationale] = useState("");
  const [stIntendedUse, setStIntendedUse] = useState("");
  const [stClinicalDecision, setStClinicalDecision] = useState("");
  const [stPopulationScope, setStPopulationScope] = useState("");
  const [stClaims, setStClaims] = useState<ClaimRow[]>([{ id: "C1", text: "" }]);
  const [stEvidence, setStEvidence] = useState<EvidenceRow[]>([
    { datasetKey: "", runKey: "", mode: "recorded" },
  ]);
  const [stMinOverall, setStMinOverall] = useState("");
  const [stMinSlice, setStMinSlice] = useState("");
  const [stMaxDisparity, setStMaxDisparity] = useState("");
  const [stZeroTolerance, setStZeroTolerance] = useState<number[]>([1, 2]);
  const [stFailureModes, setStFailureModes] = useState("");
  const [stKnownLimitations, setStKnownLimitations] = useState("");
  const [stProhibited, setStProhibited] = useState<ProhibitedRow[]>(
    PROHIBITED_USE_SEEDS.map((s) => ({ ...s, locked: true })),
  );
  const [stDispositionRule, setStDispositionRule] = useState("");
  const [stToolsDataAccess, setStToolsDataAccess] = useState("");
  const [stMonitoringPlan, setStMonitoringPlan] = useState("");
  const [stChangeControlPlan, setStChangeControlPlan] = useState("");
  const [stIncidentSuspension, setStIncidentSuspension] = useState("");
  const [stReviewBy, setStReviewBy] = useState("");
  const [stError, setStError] = useState<string | null>(null);
  const [stNotice, setStNotice] = useState<string | null>(null);

  // Prefill the threshold inputs from the selected tier's governed
  // defaults as soon as they load (a statement may tighten them).
  useEffect(() => {
    const req = tierRequirements.data?.tiers?.[stTier];
    if (!req) return;
    setStMinOverall((v) => (v === "" ? String(req.minOverallPass) : v));
    setStMinSlice((v) => (v === "" ? String(req.minSliceFloor) : v));
    setStMaxDisparity((v) => (v === "" ? String(req.maxSliceDisparity) : v));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tierRequirements.data, stTier]);

  // Kill switch panel state
  const [ksReason, setKsReason] = useState("");
  const [ksConfirming, setKsConfirming] = useState(false);
  const [ksError, setKsError] = useState<string | null>(null);

  if (datasets.loading) return <Loading label="Loading validation harness…" />;
  if (datasets.error) return <ErrorView error={datasets.error} />;

  const datasetList = datasets.data?.items ?? [];
  const agentKeys = (roster.data?.agents ?? []).map((a) => a.key);
  const selectedDataset = runDatasetId || (datasetList[0] ? String(datasetList[0].id) : "");
  const selectedRunAgent = runAgent || agentKeys[0] || "";
  const selectedDriftAgent = driftAgent || agentKeys[0] || "";
  const selectedCcAgent = ccAgent || agentKeys[0] || "";
  const govAgentKey = govAgent || agentKeys[0] || "";

  function applyTierDefaults(tier: string) {
    const req = tierRequirements.data?.tiers?.[tier];
    if (!req) return;
    setStMinOverall(String(req.minOverallPass));
    setStMinSlice(String(req.minSliceFloor));
    setStMaxDisparity(String(req.maxSliceDisparity));
  }

  function loadStatementIntoEditor(s: CaiStatement) {
    const f = s.fields ?? {};
    setEditingId(s.statementId);
    setStError(null);
    setStNotice(null);
    setStOwner(s.owner || asText(f.owner));
    const tier = s.riskTier || asText(f.riskTier) || "T2";
    setStTier(tier);
    setStTierRationale(
      asText(f.tierRationale) || asText(f.riskTierRationale) || asText(f.rationale),
    );
    setStIntendedUse(asText(f.intendedUse));
    setStClinicalDecision(asText(f.clinicalDecision));
    setStPopulationScope(asText(f.populationScope));
    const claims = Array.isArray(f.claims) ? f.claims : [];
    setStClaims(
      claims.length === 0
        ? [{ id: "C1", text: "" }]
        : claims.map((c, i) =>
            typeof c === "string"
              ? { id: `C${i + 1}`, text: c }
              : {
                  id: asText((c as Record<string, unknown>).id) || `C${i + 1}`,
                  text:
                    asText((c as Record<string, unknown>).text) ||
                    asText((c as Record<string, unknown>).claim),
                },
          ),
    );
    const evidence = Array.isArray(f.evidence) ? f.evidence : [];
    setStEvidence(
      evidence.length === 0
        ? [{ datasetKey: "", runKey: "", mode: "recorded" }]
        : evidence.map((e) => {
            const row = e as Record<string, unknown>;
            return {
              datasetKey: asText(row.datasetKey),
              runKey: asText(row.runKey),
              mode: asText(row.mode) || "recorded",
            };
          }),
    );
    const thresholds = (f.thresholds ?? {}) as Record<string, unknown>;
    setStMinOverall(asText(thresholds.minOverallPass));
    setStMinSlice(asText(thresholds.minSliceFloor));
    setStMaxDisparity(asText(thresholds.maxSliceDisparity));
    const zt = Array.isArray(f.zeroToleranceClasses)
      ? f.zeroToleranceClasses.map((n) => Number(n)).filter((n) => n >= 1 && n <= 5)
      : [];
    setStZeroTolerance([...new Set([1, 2, ...zt])].sort());
    setStFailureModes(
      Array.isArray(f.failureModes)
        ? f.failureModes.map(asText).join("\n")
        : asText(f.failureModes),
    );
    setStKnownLimitations(asText(f.knownLimitations));
    const prohibited = Array.isArray(f.prohibitedUses) ? f.prohibitedUses : [];
    setStProhibited(
      prohibited.length === 0
        ? PROHIBITED_USE_SEEDS.map((seed) => ({ ...seed, locked: true }))
        : prohibited.map((p) => {
            if (typeof p === "string") return { code: "", text: p, locked: false };
            const row = p as Record<string, unknown>;
            const code = asText(row.code);
            return {
              code,
              text: asText(row.text) || asText(row.description),
              locked: PROHIBITED_USE_SEEDS.some((seed) => seed.code === code),
            };
          }),
    );
    setStDispositionRule(asText(f.dispositionRule));
    setStToolsDataAccess(asText(f.toolsDataAccess));
    setStMonitoringPlan(asText(f.monitoringPlan));
    setStChangeControlPlan(asText(f.changeControlPlan));
    setStIncidentSuspension(asText(f.incidentSuspension));
    setStReviewBy(asText(f.reviewByRequested) || s.reviewBy || "");
  }

  function statementFieldsPayload(): Record<string, unknown> {
    return {
      intendedUse: stIntendedUse.trim(),
      clinicalDecision: stClinicalDecision.trim(),
      populationScope: stPopulationScope.trim(),
      tierRationale: stTierRationale.trim(),
      riskTierRationale: stTierRationale.trim(),
      rationale: stTierRationale.trim(),
      claims: stClaims
        .filter((c) => c.text.trim())
        .map((c) => ({ id: c.id.trim(), text: c.text.trim() })),
      evidence: stEvidence
        .filter((e) => e.datasetKey.trim() || e.runKey.trim())
        .map((e) => ({
          datasetKey: e.datasetKey.trim(),
          runKey: e.runKey.trim(),
          mode: e.mode,
        })),
      thresholds: {
        minOverallPass: Number(stMinOverall),
        minSliceFloor: Number(stMinSlice),
        maxSliceDisparity: Number(stMaxDisparity),
      },
      zeroToleranceClasses: [...new Set([1, 2, ...stZeroTolerance])].sort(),
      failureModes: stFailureModes.trim(),
      knownLimitations: stKnownLimitations.trim(),
      prohibitedUses: stProhibited
        .filter((p) => p.text.trim() || p.code.trim())
        .map((p) => ({ code: p.code.trim(), text: p.text.trim() })),
      dispositionRule: stDispositionRule.trim(),
      toolsDataAccess: stToolsDataAccess.trim(),
      monitoringPlan: stMonitoringPlan.trim(),
      changeControlPlan: stChangeControlPlan.trim(),
      incidentSuspension: stIncidentSuspension.trim(),
      reviewByRequested: stReviewBy,
    };
  }

  async function saveDraft(): Promise<CaiStatement | null> {
    setStError(null);
    if (!govAgentKey) {
      setStError("No agent is selected — there is no roster agent to state for.");
      return null;
    }
    const body = {
      agentKey: govAgentKey,
      owner: stOwner.trim(),
      riskTier: stTier,
      fields: statementFieldsPayload(),
    };
    try {
      const saved = editingId
        ? await client.request<CaiStatement>(
            `/ai-governance/statements/${encodeURIComponent(editingId)}`,
            { method: "PUT", body: JSON.stringify(body) },
          )
        : await client.request<CaiStatement>("/ai-governance/statements", {
            method: "POST",
            body: JSON.stringify(body),
          });
      setEditingId(saved.statementId);
      setStNotice(`Draft statement ${saved.statementId} saved (v${saved.version}).`);
      setReloadKey((k) => k + 1);
      return saved;
    } catch (err) {
      setStError(errText(err));
      return null;
    }
  }

  async function submitDraft() {
    setStError(null);
    const saved = await saveDraft();
    if (!saved) return;
    try {
      const submitted = await client.request<CaiStatement>(
        `/ai-governance/statements/${encodeURIComponent(saved.statementId)}/submit`,
        { method: "POST", body: JSON.stringify({}) },
      );
      setStNotice(
        `Statement ${submitted.statementId} submitted for approval (status: ${submitted.status}).`,
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      // A 422 names the missing §4.1 fields — shown verbatim.
      setStError(errText(err));
    }
  }

  async function engageKillSwitch() {
    setKsError(null);
    try {
      await client.request("/ai-governance/kill-switch", {
        method: "POST",
        body: JSON.stringify({ reason: ksReason.trim() }),
      });
      setKsConfirming(false);
      setNotice("Kill switch engaged — all agents are disabled.");
      setReloadKey((k) => k + 1);
    } catch (err) {
      setKsError(errText(err));
    }
  }

  async function releaseKillSwitch() {
    setKsError(null);
    try {
      await client.request("/ai-governance/kill-switch/release", {
        method: "POST",
        body: JSON.stringify({}),
      });
      setNotice(
        "Kill switch released. No agent was re-enabled — re-enablement is per agent through the normal gate.",
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      setKsError(errText(err));
    }
  }

  async function act(fn: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setNotice(errText(err));
    }
  }

  async function createDataset() {
    setDsError(null);
    let cases: unknown;
    try {
      cases = JSON.parse(dsCases);
    } catch (err) {
      setDsError(`Cases are not valid JSON: ${err instanceof Error ? err.message : String(err)}`);
      return;
    }
    if (!Array.isArray(cases)) {
      setDsError("Cases must be a JSON array of case objects.");
      return;
    }
    try {
      const created = await client.request<EvaluationDataset>(
        "/agent-validation/datasets",
        {
          method: "POST",
          body: JSON.stringify({
            key: dsKey.trim() || undefined,
            name: dsName.trim(),
            description: dsDescription.trim(),
            cases,
          }),
        },
      );
      setNotice(`Dataset '${created.key}' created (${created.caseCount} cases, hash ${created.casesHash.slice(0, 12)}…).`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      // Server-side case validation failures land here verbatim —
      // the dataset is refused, never partially stored.
      setDsError(errText(err));
    }
  }

  async function runDataset() {
    setRunError(null);
    setRunResult(null);
    try {
      const out = await client.request<EvaluationRun & { validationRecord?: ValidationRecordOut }>(
        `/agent-validation/datasets/${encodeURIComponent(selectedDataset)}/run`,
        {
          method: "POST",
          body: JSON.stringify({ agentKey: selectedRunAgent, mode: runMode }),
        },
      );
      setRunResult(
        `Run complete: ${out.passedCases}/${out.totalCases} cases passed (${pct(out.passRate)}) for ${out.agentKey} v${out.agentConfigVersion}.` +
          (out.validationRecord
            ? ` Validation record minted, expires ${out.validationRecord.expiresAt ?? "—"}.`
            : ""),
      );
      setReloadKey((k) => k + 1);
    } catch (err) {
      // Live mode with no model / unreachable runtime: the server's
      // 503 verbatim, and nothing was recorded.
      setRunError(errText(err));
    }
  }

  async function loadDrift() {
    setDriftError(null);
    setDrift(null);
    try {
      setDrift(
        await client.request<DriftReport>(
          `/agent-validation/agents/${encodeURIComponent(selectedDriftAgent)}/drift`,
        ),
      );
    } catch (err) {
      setDriftError(errText(err));
    }
  }

  async function proposeChangeControl() {
    setCcError(null);
    const fromVersion = Number(ccFrom);
    const toVersion = Number(ccTo);
    if (!Number.isInteger(fromVersion) || !Number.isInteger(toVersion)) {
      setCcError("From/To versions must be integers.");
      return;
    }
    try {
      const created = await client.request<ChangeControlOut>(
        "/agent-validation/change-control",
        {
          method: "POST",
          body: JSON.stringify({
            agentKey: selectedCcAgent,
            fromVersion,
            toVersion,
            intendedChange: ccChange.trim(),
            validationPlanId: ccPlan.trim(),
          }),
        },
      );
      setNotice(`Change-control record proposed for ${created.agentKey} v${created.fromVersion} → v${created.toVersion}.`);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setCcError(errText(err));
    }
  }

  return (
    <div>
      <p className="hint">
        Every agent version must carry a current validation record
        before it can be enabled — the record is minted by running a
        governed evaluation dataset and expires after the configured
        validity window. Recorded mode scores stored synthetic outputs
        (harness mechanics only, not model evidence); live mode runs
        the real runtime and fails honestly when no model answers.
      </p>
      {config.data && (
        <p className="hint" role="status">
          Governance: validation required for enable ={" "}
          {config.data.requireValidationForEnable ? "yes" : "no"} ·
          validity {config.data.validationValidityDays} days · slice
          disparity threshold {config.data.sliceDisparityThreshold} ·
          drift reject-rate threshold {config.data.driftRejectRateThreshold}
          {config.data.notes ? ` · ${config.data.notes}` : ""}
        </p>
      )}
      {notice && <p className="hint" role="status">{notice}</p>}

      <h3>Risk tiers</h3>
      {tierRequirements.loading ? (
        <Loading label="Loading tier requirements…" />
      ) : tierRequirements.error ? (
        <p className="error-note" role="alert">
          {errText(tierRequirements.error)}
        </p>
      ) : (
        <div>
          <table className="studio-table">
            <thead>
              <tr>
                <th>Tier</th>
                <th>Min overall pass</th>
                <th>Min slice floor</th>
                <th>Max slice disparity</th>
                <th>Statement validity</th>
                <th>Required approvals</th>
                <th>Monitoring cadence</th>
              </tr>
            </thead>
            <tbody>
              {(["T1", "T2", "T3"] as const).map((tier) => {
                const req = tierRequirements.data?.tiers?.[tier];
                if (!req) return null;
                return (
                  <tr key={tier}>
                    <td><strong>{tier}</strong></td>
                    <td>{req.minOverallPass}</td>
                    <td>{req.minSliceFloor}</td>
                    <td>{req.maxSliceDisparity}</td>
                    <td>{req.statementValidityMonths} months</td>
                    <td>{req.approvals.join(", ")}</td>
                    <td>{req.monitoringCadence}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {tierRequirements.data?.source ? (
            <p className="hint">Source: {tierRequirements.data.source}</p>
          ) : null}
        </div>
      )}

      <h3>Validation status per agent</h3>
      {statuses.loading ? (
        <Loading label="Loading agent validation status…" />
      ) : statuses.error ? (
        <ErrorView error={statuses.error} />
      ) : (statuses.data ?? []).length === 0 ? (
        <Empty label="No agents are known to the roster yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Published</th>
              <th>Model route</th>
              <th>Status</th>
              <th>Reason</th>
            </tr>
          </thead>
          <tbody>
            {(statuses.data ?? []).map((s) => (
              <tr key={s.agentKey}>
                <td><strong>{s.agentKey}</strong></td>
                <td>{s.publishedVersion ? `v${s.publishedVersion}` : "—"}</td>
                <td>{s.modelRoute ? <code>{s.modelRoute}</code> : "(none)"}</td>
                <td>
                  <StatusBadge tone={validationStatusTone(s.status)}>
                    {s.status}
                  </StatusBadge>
                  {s.required ? "" : " (not required)"}
                </td>
                <td>
                  {s.reason}
                  {s.record ? (
                    <span className="hint">
                      {" "}· record expires {s.record.expiresAt ?? "—"}, pass rate {pct(s.record.passRate)}
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Evaluation datasets</h3>
      {datasetList.length === 0 ? (
        <Empty label="No evaluation datasets yet — create one below." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Cases</th>
              <th>Cases hash</th>
              <th>Created by</th>
            </tr>
          </thead>
          <tbody>
            {datasetList.map((d) => (
              <tr key={d.id}>
                <td>
                  <strong>{d.key}</strong>
                  {d.name ? (
                    <>
                      <br />
                      <span className="hint">{d.name}</span>
                    </>
                  ) : null}
                </td>
                <td>{d.caseCount}</td>
                <td><code>{d.casesHash.slice(0, 12)}…</code></td>
                <td>{d.createdBy}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Create a dataset</h4>
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void createDataset();
        }}
      >
        <p>
          <label>
            Key{" "}
            <input
              aria-label="Dataset key"
              value={dsKey}
              onChange={(e) => setDsKey(e.target.value)}
              placeholder="optional — derived from the name when blank"
              size={32}
            />
          </label>{" "}
          <label>
            Name{" "}
            <input
              aria-label="Dataset name"
              value={dsName}
              onChange={(e) => setDsName(e.target.value)}
              size={32}
            />
          </label>{" "}
          <label>
            Description{" "}
            <input
              aria-label="Dataset description"
              value={dsDescription}
              onChange={(e) => setDsDescription(e.target.value)}
              size={40}
            />
          </label>
        </p>
        <p>
          <textarea
            aria-label="Dataset cases JSON"
            value={dsCases}
            onChange={(e) => setDsCases(e.target.value)}
            rows={10}
            cols={84}
          />
        </p>
        {dsError && (
          <p className="error-note" role="alert">
            {dsError}
          </p>
        )}
        <p>
          <button type="submit">Create dataset</button>{" "}
          <span className="hint">
            Cases are validated and sha256-hashed by the server; malformed
            cases are refused with the reason shown above.
          </span>
        </p>
      </form>

      <h4>Run a dataset</h4>
      {datasetList.length === 0 || agentKeys.length === 0 ? (
        <p className="hint">
          Running is unavailable:{" "}
          {datasetList.length === 0 ? "no datasets exist" : ""}
          {datasetList.length === 0 && agentKeys.length === 0 ? " and " : ""}
          {agentKeys.length === 0 ? "no agents are on the roster" : ""}.
        </p>
      ) : (
        <div>
          <p>
            <label>
              Dataset{" "}
              <select
                aria-label="Dataset to run"
                value={selectedDataset}
                onChange={(e) => setRunDatasetId(e.target.value)}
              >
                {datasetList.map((d) => (
                  <option key={d.id} value={String(d.id)}>
                    {d.key}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              Agent{" "}
              <select
                aria-label="Agent to validate"
                value={selectedRunAgent}
                onChange={(e) => setRunAgent(e.target.value)}
              >
                {agentKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              Mode{" "}
              <select
                aria-label="Run mode"
                value={runMode}
                onChange={(e) => setRunMode(e.target.value as "recorded" | "live")}
              >
                <option value="recorded">recorded</option>
                <option value="live">live</option>
              </select>
            </label>{" "}
            <button type="button" onClick={() => void runDataset()}>
              Run dataset
            </button>
          </p>
          {runError && (
            <p className="error-note" role="alert">
              {runError}
            </p>
          )}
          {runResult && (
            <p className="hint" role="status">
              {runResult}
            </p>
          )}
        </div>
      )}

      <h3>Evaluation runs</h3>
      {(evalRuns.data?.items ?? []).length === 0 ? (
        <Empty label="No evaluation runs yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Dataset</th>
              <th>Agent</th>
              <th>Mode</th>
              <th>Pass rate</th>
              <th>Disparity flags</th>
            </tr>
          </thead>
          <tbody>
            {(evalRuns.data?.items ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.datasetKey}</td>
                <td>
                  {r.agentKey} v{r.agentConfigVersion}
                </td>
                <td>{r.mode}</td>
                <td>
                  {pct(r.passRate)} ({r.passedCases}/{r.totalCases})
                </td>
                <td>
                  {r.disparityFlags.length === 0
                    ? "none"
                    : r.disparityFlags
                        .map((f) => (typeof f === "string" ? f : (f.detail ?? "flag")))
                        .join("; ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Validation records</h3>
      {(records.data?.items ?? []).length === 0 ? (
        <Empty label="No validation records minted yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Dataset</th>
              <th>Pass rate</th>
              <th>Issued</th>
              <th>Expires</th>
            </tr>
          </thead>
          <tbody>
            {(records.data?.items ?? []).map((r) => (
              <tr key={r.id}>
                <td>
                  {r.agentKey} v{r.agentConfigVersion}
                </td>
                <td>{r.datasetKey}</td>
                <td>{pct(r.passRate)}</td>
                <td>{r.issuedAt ?? "—"}</td>
                <td>{r.expiresAt ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Drift</h3>
      {agentKeys.length === 0 ? (
        <p className="hint">Drift is unavailable: no agents are on the roster.</p>
      ) : (
        <div>
          <p>
            <label>
              Agent{" "}
              <select
                aria-label="Agent for drift"
                value={selectedDriftAgent}
                onChange={(e) => setDriftAgent(e.target.value)}
              >
                {agentKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>{" "}
            <button type="button" onClick={() => void loadDrift()}>
              Check drift
            </button>
          </p>
          {driftError && (
            <p className="error-note" role="alert">
              {driftError}
            </p>
          )}
          {drift && (
            <div>
              <p>
                Status:{" "}
                <StatusBadge tone={drift.status === "within-threshold" ? "ok" : drift.status === "drift-flagged" ? "bad" : "neutral"}>
                  {drift.status}
                </StatusBadge>
                {drift.reason ? <span className="hint"> {drift.reason}</span> : null}
              </p>
              {drift.flags.length > 0 ? (
                <ul>
                  {drift.flags.map((f, i) => (
                    <li key={i}>{f.detail ?? f.metric ?? "flag"}</li>
                  ))}
                </ul>
              ) : (
                <p className="hint">No drift flags against the validation baseline.</p>
              )}
            </div>
          )}
        </div>
      )}

      <h3>Change control (PCCP)</h3>
      {(changeControl.data?.items ?? []).length === 0 ? (
        <Empty label="No change-control records yet." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Change</th>
              <th>Intended change</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(changeControl.data?.items ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.agentKey}</td>
                <td>
                  v{r.fromVersion} → v{r.toVersion}
                </td>
                <td>{r.intendedChange}</td>
                <td>
                  <StatusBadge tone={r.status === "approved" ? "ok" : "warn"}>
                    {r.status}
                  </StatusBadge>
                  {r.approver ? <span className="hint"> by {r.approver}</span> : null}
                </td>
                <td>
                  {r.status === "proposed" && (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.request(
                              `/agent-validation/change-control/${r.id}/approve`,
                              { method: "POST", body: JSON.stringify({}) },
                            ),
                          `Change-control record ${r.id} approved.`,
                        )
                      }
                    >
                      Approve
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h4>Propose a change</h4>
      {agentKeys.length === 0 ? (
        <p className="hint">Proposing is unavailable: no agents are on the roster.</p>
      ) : (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void proposeChangeControl();
          }}
        >
          <p>
            <label>
              Agent{" "}
              <select
                aria-label="Change-control agent"
                value={selectedCcAgent}
                onChange={(e) => setCcAgent(e.target.value)}
              >
                {agentKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              From version{" "}
              <input
                aria-label="From version"
                value={ccFrom}
                onChange={(e) => setCcFrom(e.target.value)}
                size={4}
                inputMode="numeric"
              />
            </label>{" "}
            <label>
              To version{" "}
              <input
                aria-label="To version"
                value={ccTo}
                onChange={(e) => setCcTo(e.target.value)}
                size={4}
                inputMode="numeric"
              />
            </label>{" "}
            <label>
              Validation plan (dataset key){" "}
              <input
                aria-label="Validation plan"
                value={ccPlan}
                onChange={(e) => setCcPlan(e.target.value)}
                size={24}
              />
            </label>
          </p>
          <p>
            <label>
              Intended change{" "}
              <input
                aria-label="Intended change"
                value={ccChange}
                onChange={(e) => setCcChange(e.target.value)}
                size={64}
              />
            </label>
          </p>
          {ccError && (
            <p className="error-note" role="alert">
              {ccError}
            </p>
          )}
          <p>
            <button type="submit" disabled={!ccChange.trim()}>
              Propose change
            </button>
          </p>
        </form>
      )}

      <h3>Change-control exceptions</h3>
      {exceptions.loading ? (
        <Loading label="Loading exceptions…" />
      ) : (exceptions.data?.items ?? []).length === 0 ? (
        <p className="hint" role="status">
          No exceptions — every published agent version is covered by a
          change-control record.
        </p>
      ) : (
        <ul>
          {(exceptions.data?.items ?? []).map((x) => (
            <li key={`${x.agentKey}-${x.version}`}>
              <StatusBadge tone="bad">exception</StatusBadge> {x.reason}
            </li>
          ))}
        </ul>
      )}

      <h3>Validation statements</h3>
      {agentKeys.length === 0 ? (
        <p className="hint">Statements are unavailable: no agents are on the roster.</p>
      ) : (
        <div>
          <p>
            <label>
              Agent{" "}
              <select
                aria-label="Governance agent"
                value={govAgentKey}
                onChange={(e) => {
                  setGovAgent(e.target.value);
                  setEditingId(null);
                }}
              >
                {agentKeys.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
          </p>
          {statements.loading ? (
            <Loading label="Loading validation statements…" />
          ) : statements.error ? (
            <p className="error-note" role="alert">
              {errText(statements.error)}
            </p>
          ) : (statements.data?.items ?? []).length === 0 ? (
            <Empty label={`No validation statements for ${govAgentKey} yet — draft one below.`} />
          ) : (
            <table className="studio-table">
              <thead>
                <tr>
                  <th>Statement</th>
                  <th>Tier</th>
                  <th>Owner</th>
                  <th>Status</th>
                  <th>Currency</th>
                  <th>Review by</th>
                  <th>Approvals</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {(statements.data?.items ?? []).map((s) => (
                  <tr key={s.statementId}>
                    <td>
                      <strong>{s.statementId}</strong>
                      <span className="hint"> v{s.version}</span>
                    </td>
                    <td>{s.riskTier}</td>
                    <td>{s.owner}</td>
                    <td>
                      <StatusBadge tone={s.status === "approved" ? "ok" : "neutral"}>
                        {s.status}
                      </StatusBadge>
                    </td>
                    <td>
                      <StatusBadge tone={currencyTone(s.currency?.status ?? "missing")}>
                        {s.currency?.status ?? "missing"}
                      </StatusBadge>
                      {s.currency?.reason ? (
                        <span className="hint"> {s.currency.reason}</span>
                      ) : null}
                    </td>
                    <td>{s.reviewBy ?? "—"}</td>
                    <td>
                      {(s.approvals ?? []).length === 0
                        ? "none recorded"
                        : (s.approvals ?? []).map((a, i) => (
                            <div key={i}>
                              {a.kind}: {a.approver}
                              {a.conditions ? ` — conditions: ${a.conditions}` : ""}
                              {a.decidedAt ? ` (${a.decidedAt})` : ""}
                            </div>
                          ))}
                    </td>
                    <td>
                      {s.status === "draft" && (
                        <button
                          type="button"
                          onClick={() => loadStatementIntoEditor(s)}
                        >
                          Edit draft
                        </button>
                      )}
                      <StatementRowActions
                        client={client}
                        statement={s}
                        onChanged={() => setReloadKey((k) => k + 1)}
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <h4>{editingId ? `Edit draft statement ${editingId}` : "Draft a validation statement"}</h4>
          <fieldset>
            <legend>Use &amp; scope</legend>
            <p>
              <label>
                Owner{" "}
                <input
                  aria-label="Statement owner"
                  value={stOwner}
                  onChange={(e) => setStOwner(e.target.value)}
                  size={32}
                />
              </label>{" "}
              <label>
                Risk tier{" "}
                <select
                  aria-label="Statement risk tier"
                  value={stTier}
                  onChange={(e) => {
                    setStTier(e.target.value);
                    applyTierDefaults(e.target.value);
                  }}
                >
                  <option value="T1">T1</option>
                  <option value="T2">T2</option>
                  <option value="T3">T3</option>
                </select>
              </label>
            </p>
            <p>
              <label>
                Tier rationale{" "}
                <input
                  aria-label="Tier rationale"
                  value={stTierRationale}
                  onChange={(e) => setStTierRationale(e.target.value)}
                  size={64}
                />
              </label>
            </p>
            <p>
              <label>
                Intended use{" "}
                <textarea
                  aria-label="Intended use"
                  value={stIntendedUse}
                  onChange={(e) => setStIntendedUse(e.target.value)}
                  rows={3}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Clinical decision touched{" "}
                <textarea
                  aria-label="Clinical decision"
                  value={stClinicalDecision}
                  onChange={(e) => setStClinicalDecision(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Population &amp; data scope{" "}
                <textarea
                  aria-label="Population scope"
                  value={stPopulationScope}
                  onChange={(e) => setStPopulationScope(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
          </fieldset>

          <fieldset>
            <legend>Claims &amp; evidence</legend>
            {stClaims.map((c, i) => (
              <p key={i}>
                <label>
                  Claim ID{" "}
                  <input
                    aria-label={`Claim ${i + 1} ID`}
                    value={c.id}
                    onChange={(e) =>
                      setStClaims((rows) =>
                        rows.map((r, j) => (j === i ? { ...r, id: e.target.value } : r)),
                      )
                    }
                    size={6}
                  />
                </label>{" "}
                <label>
                  Claim text{" "}
                  <input
                    aria-label={`Claim ${i + 1} text`}
                    value={c.text}
                    onChange={(e) =>
                      setStClaims((rows) =>
                        rows.map((r, j) => (j === i ? { ...r, text: e.target.value } : r)),
                      )
                    }
                    size={56}
                  />
                </label>{" "}
                <button
                  type="button"
                  onClick={() =>
                    setStClaims((rows) => rows.filter((_, j) => j !== i))
                  }
                >
                  Remove claim
                </button>
              </p>
            ))}
            <p>
              <button
                type="button"
                onClick={() =>
                  setStClaims((rows) => [
                    ...rows,
                    { id: `C${rows.length + 1}`, text: "" },
                  ])
                }
              >
                Add claim
              </button>
            </p>
            {stEvidence.map((ev, i) => (
              <p key={i}>
                <label>
                  Dataset key{" "}
                  <input
                    aria-label={`Evidence ${i + 1} dataset key`}
                    value={ev.datasetKey}
                    onChange={(e) =>
                      setStEvidence((rows) =>
                        rows.map((r, j) =>
                          j === i ? { ...r, datasetKey: e.target.value } : r,
                        ),
                      )
                    }
                    size={24}
                  />
                </label>{" "}
                <label>
                  Run key{" "}
                  <input
                    aria-label={`Evidence ${i + 1} run key`}
                    value={ev.runKey}
                    onChange={(e) =>
                      setStEvidence((rows) =>
                        rows.map((r, j) =>
                          j === i ? { ...r, runKey: e.target.value } : r,
                        ),
                      )
                    }
                    size={24}
                  />
                </label>{" "}
                <label>
                  Mode{" "}
                  <select
                    aria-label={`Evidence ${i + 1} mode`}
                    value={ev.mode}
                    onChange={(e) =>
                      setStEvidence((rows) =>
                        rows.map((r, j) =>
                          j === i ? { ...r, mode: e.target.value } : r,
                        ),
                      )
                    }
                  >
                    <option value="recorded">recorded</option>
                    <option value="live">live</option>
                  </select>
                </label>{" "}
                <button
                  type="button"
                  onClick={() =>
                    setStEvidence((rows) => rows.filter((_, j) => j !== i))
                  }
                >
                  Remove evidence
                </button>
              </p>
            ))}
            <p>
              <button
                type="button"
                onClick={() =>
                  setStEvidence((rows) => [
                    ...rows,
                    { datasetKey: "", runKey: "", mode: "recorded" },
                  ])
                }
              >
                Add evidence
              </button>
            </p>
            <p>
              <label>
                Min overall pass{" "}
                <input
                  aria-label="Min overall pass"
                  type="number"
                  step="0.01"
                  min="0"
                  max="1"
                  value={stMinOverall}
                  onChange={(e) => setStMinOverall(e.target.value)}
                />
              </label>{" "}
              <label>
                Min slice floor{" "}
                <input
                  aria-label="Min slice floor"
                  type="number"
                  step="0.01"
                  min="0"
                  max="1"
                  value={stMinSlice}
                  onChange={(e) => setStMinSlice(e.target.value)}
                />
              </label>{" "}
              <label>
                Max slice disparity{" "}
                <input
                  aria-label="Max slice disparity"
                  type="number"
                  step="0.01"
                  min="0"
                  max="1"
                  value={stMaxDisparity}
                  onChange={(e) => setStMaxDisparity(e.target.value)}
                />
              </label>
            </p>
            <p>
              Zero-tolerance classes:{" "}
              {[1, 2, 3, 4, 5].map((n) => (
                <label key={n}>
                  <input
                    type="checkbox"
                    aria-label={`Zero-tolerance class ${n}`}
                    checked={n === 1 || n === 2 || stZeroTolerance.includes(n)}
                    disabled={n === 1 || n === 2}
                    onChange={(e) =>
                      setStZeroTolerance((rows) =>
                        e.target.checked
                          ? [...rows, n]
                          : rows.filter((x) => x !== n),
                      )
                    }
                  />{" "}
                  {n}. {ZERO_TOLERANCE_LABELS[n]}{" "}
                </label>
              ))}
              <span className="hint">Classes 1 and 2 are always on.</span>
            </p>
            <p>
              <label>
                Failure modes found{" "}
                <textarea
                  aria-label="Failure modes"
                  value={stFailureModes}
                  onChange={(e) => setStFailureModes(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Known limitations{" "}
                <textarea
                  aria-label="Known limitations"
                  value={stKnownLimitations}
                  onChange={(e) => setStKnownLimitations(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
          </fieldset>

          <fieldset>
            <legend>Controls</legend>
            {stProhibited.map((p, i) => (
              <p key={i}>
                <label>
                  Prohibited use code{" "}
                  <input
                    aria-label={`Prohibited use ${i + 1} code`}
                    value={p.code}
                    disabled={p.locked}
                    onChange={(e) =>
                      setStProhibited((rows) =>
                        rows.map((r, j) =>
                          j === i ? { ...r, code: e.target.value } : r,
                        ),
                      )
                    }
                    size={32}
                  />
                </label>{" "}
                <label>
                  Text{" "}
                  <input
                    aria-label={`Prohibited use ${i + 1} text`}
                    value={p.text}
                    onChange={(e) =>
                      setStProhibited((rows) =>
                        rows.map((r, j) =>
                          j === i ? { ...r, text: e.target.value } : r,
                        ),
                      )
                    }
                    size={48}
                  />
                </label>{" "}
                {!p.locked && (
                  <button
                    type="button"
                    onClick={() =>
                      setStProhibited((rows) => rows.filter((_, j) => j !== i))
                    }
                  >
                    Remove prohibited use
                  </button>
                )}
              </p>
            ))}
            <p>
              <button
                type="button"
                onClick={() =>
                  setStProhibited((rows) => [
                    ...rows,
                    { code: "", text: "", locked: false },
                  ])
                }
              >
                Add prohibited use
              </button>
            </p>
            <p>
              <label>
                Human-disposition rule{" "}
                <textarea
                  aria-label="Disposition rule"
                  value={stDispositionRule}
                  onChange={(e) => setStDispositionRule(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Tools &amp; data access{" "}
                <textarea
                  aria-label="Tools and data access"
                  value={stToolsDataAccess}
                  onChange={(e) => setStToolsDataAccess(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Monitoring plan{" "}
                <textarea
                  aria-label="Monitoring plan"
                  value={stMonitoringPlan}
                  onChange={(e) => setStMonitoringPlan(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Change-control plan{" "}
                <textarea
                  aria-label="Change control plan"
                  value={stChangeControlPlan}
                  onChange={(e) => setStChangeControlPlan(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Incident &amp; suspension{" "}
                <textarea
                  aria-label="Incident and suspension"
                  value={stIncidentSuspension}
                  onChange={(e) => setStIncidentSuspension(e.target.value)}
                  rows={2}
                  cols={84}
                />
              </label>
            </p>
            <p>
              <label>
                Review-by requested{" "}
                <input
                  aria-label="Review by requested"
                  type="date"
                  value={stReviewBy}
                  onChange={(e) => setStReviewBy(e.target.value)}
                />
              </label>
            </p>
          </fieldset>
          {stError && (
            <p className="error-note" role="alert">
              {stError}
            </p>
          )}
          {stNotice && (
            <p className="hint" role="status">
              {stNotice}
            </p>
          )}
          <p>
            <button type="button" onClick={() => void saveDraft()}>
              Save draft
            </button>{" "}
            <button type="button" onClick={() => void submitDraft()}>
              Submit for approval
            </button>{" "}
            <span className="hint">
              Submit saves the draft first, then asks the server to check
              §4.1 completeness — a refusal names the missing fields above.
            </span>
          </p>
        </div>
      )}

      <h3>Expiry worklist</h3>
      {worklist.loading ? (
        <Loading label="Loading expiry worklist…" />
      ) : worklist.error ? (
        <p className="error-note" role="alert">
          {errText(worklist.error)}
        </p>
      ) : (worklist.data?.items ?? []).length === 0 ? (
        <Empty label="No agents on the governance worklist." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Owner</th>
              <th>Tier</th>
              <th>Statement</th>
              <th>Record</th>
              <th>Due state</th>
            </tr>
          </thead>
          <tbody>
            {(worklist.data?.items ?? []).map((w) => {
              const overdue =
                w.dueState.toLowerCase().includes("overdue") ||
                (w.statementDaysToExpiry !== null && w.statementDaysToExpiry < 0) ||
                (w.recordDaysToExpiry !== null && w.recordDaysToExpiry < 0);
              return (
                <tr
                  key={w.agentKey}
                  style={overdue ? { background: "#fdecea" } : undefined}
                >
                  <td><strong>{w.agentKey}</strong></td>
                  <td>{w.owner}</td>
                  <td>{w.tier}</td>
                  <td>
                    {w.statementStatus}
                    {w.statementDaysToExpiry !== null
                      ? ` (${w.statementDaysToExpiry} days)`
                      : ""}
                  </td>
                  <td>
                    {w.recordStatus}
                    {w.recordDaysToExpiry !== null
                      ? ` (${w.recordDaysToExpiry} days)`
                      : ""}
                  </td>
                  <td>
                    <StatusBadge tone={overdue ? "bad" : "neutral"}>
                      {w.dueState}
                    </StatusBadge>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}

      <h3>Kill switch</h3>
      {killSwitch.loading ? (
        <Loading label="Loading kill switch state…" />
      ) : killSwitch.error ? (
        <p className="error-note" role="alert">
          {errText(killSwitch.error)}
        </p>
      ) : killSwitch.data?.active ? (
        <div>
          <p role="status">
            <StatusBadge tone="bad">active</StatusBadge> Kill switch is
            ACTIVE — all agents are disabled.
            {killSwitch.data.reason ? <> Reason: {killSwitch.data.reason}.</> : null}
            {killSwitch.data.engagedBy ? <> Engaged by {killSwitch.data.engagedBy}</> : null}
            {killSwitch.data.engagedAt ? <> at {killSwitch.data.engagedAt}</> : null}
            .
          </p>
          <p className="hint">
            Releasing the kill switch does not re-enable any agent — each
            agent must be re-enabled individually through its normal gate.
          </p>
          <p>
            <button type="button" onClick={() => void releaseKillSwitch()}>
              Release kill switch
            </button>
          </p>
        </div>
      ) : (
        <div>
          <p role="status">
            <StatusBadge tone="ok">inactive</StatusBadge> Kill switch is
            inactive.
          </p>
          <p className="hint">
            Engaging the kill switch disables ALL agents immediately.
            Releasing it afterwards does not re-enable any agent — each
            agent must be re-enabled individually through its normal gate.
          </p>
          <p>
            <label>
              Reason{" "}
              <input
                aria-label="Kill switch reason"
                value={ksReason}
                onChange={(e) => setKsReason(e.target.value)}
                size={48}
              />
            </label>{" "}
            {!ksConfirming ? (
              <button
                type="button"
                disabled={!ksReason.trim()}
                onClick={() => setKsConfirming(true)}
              >
                Engage kill switch
              </button>
            ) : (
              <>
                <button type="button" onClick={() => void engageKillSwitch()}>
                  Confirm engage — disable all agents
                </button>{" "}
                <button type="button" onClick={() => setKsConfirming(false)}>
                  Cancel
                </button>
              </>
            )}
          </p>
          {!ksReason.trim() && (
            <p className="hint">A reason is required before the kill switch can be engaged.</p>
          )}
        </div>
      )}
      {ksError && (
        <p className="error-note" role="alert">
          {ksError}
        </p>
      )}

      <h3>Disclosure</h3>
      {agentKeys.length === 0 ? (
        <p className="hint">Disclosure is unavailable: no agents are on the roster.</p>
      ) : (
        <div>
          <p className="hint">
            Source attributes for {govAgentKey}, generated from its
            statement, config and validation record. Gaps are shown as
            recorded — nothing is filled in.
          </p>
          <AgentDisclosure client={client} agentKey={govAgentKey} />
        </div>
      )}

      <h3>Agent incidents</h3>
      {incidents.loading ? (
        <Loading label="Loading agent incidents…" />
      ) : incidents.error ? (
        <p className="error-note" role="alert">
          {errText(incidents.error)}
        </p>
      ) : (incidents.data?.items ?? []).length === 0 ? (
        <Empty label="No agent incidents reported." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Event</th>
              <th>Category</th>
              <th>Harm</th>
              <th>Status</th>
              <th>When</th>
              <th>Description</th>
            </tr>
          </thead>
          <tbody>
            {(incidents.data?.items ?? []).map((ev) => (
              <tr key={ev.eventId}>
                <td><code>{ev.eventId}</code></td>
                <td>{ev.category}</td>
                <td>{ev.harmLevel}</td>
                <td>{ev.status}</td>
                <td>{ev.createdAt ?? "—"}</td>
                <td>
                  {ev.description.length > 120
                    ? `${ev.description.slice(0, 120)}…`
                    : ev.description}
                  <span className="hint"> — reported by {ev.reporter}</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
