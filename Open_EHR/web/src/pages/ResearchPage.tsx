import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { ModuleHeader } from "../components/ModuleHeader";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { Tabs } from "../components/Tabs";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 16 (WS-F2 + WS-F1 / K2) — research & R&D operations: studies,
// CTMS coverage determinations + billing grid (BD-42), protocol
// approval by Part 11 e-signature over a snapshot hash (BD-46), and
// the genomics surface (reported variants + PGx check) that rides the
// study workspace. Activation is blocked — in plain words — until
// every procedure carries a coverage determination.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

/** The activation gate returns a structured 422 detail; surface the
 *  undetermined procedure codes plainly instead of "[object Object]". */
function undeterminedFromError(err: unknown): string[] | null {
  if (!(err instanceof ApiError)) return null;
  const body = err.body as { detail?: { undetermined_procedures?: Array<{ procedure_code: string }> } } | undefined;
  const list = body?.detail?.undetermined_procedures;
  return list && list.length ? list.map((p) => p.procedure_code) : null;
}

interface Determination {
  id: number;
  determination: "research" | "clinical" | "mixed";
  rationale: string;
  determined_by: string;
  signature_id: number | null;
}

interface Procedure {
  id: number;
  study_id: number;
  procedure_code: string;
  description: string;
  determination: Determination | null;
  snapshot_hash: string;
}

interface Study {
  id: number;
  protocol_id: string;
  title: string;
  phase: string;
  status: string;
  pi_username: string;
  approved: boolean;
  approval_signature_id: number | null;
  snapshot_hash: string;
  procedures?: Procedure[];
}

interface Coverage {
  items: Procedure[];
  undetermined: Array<{ procedure_id: number; procedure_code: string }>;
  coverage_complete: boolean;
}

interface GridLine {
  id: number;
  charge_code: string;
  procedure_code: string | null;
  procedure_id: number | null;
  amount: number;
  billed_to: "research" | "standard";
}

interface Grid {
  lines: GridLine[];
  research_total: number;
  standard_total: number;
  grand_total: number;
}

interface Variant {
  observation_id: string;
  gene: string | null;
  hgvs_c: string | null;
  hgvs_p: string | null;
  zygosity: string | null;
  interpretation: string | null;
  source_lab: string | null;
}

interface PgxFinding {
  gene: string | null;
  variant: string | null;
  drug: string;
  action: "avoid" | "reduce" | "flag";
  message: string;
}

function studyTone(status: string): StatusTone {
  if (status === "active") return "ok";
  if (status === "closed") return "neutral";
  return "warn";
}

function actionTone(action: string): StatusTone {
  if (action === "avoid") return "bad";
  if (action === "reduce") return "warn";
  return "info";
}

const money = (n: number) => `$${Number(n).toFixed(2)}`;

export function ResearchPage({ client }: Props) {
  const studies = useAsync<{ items: Study[]; total: number }>(
    () => client.request("/research/studies"),
    [client],
  );
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [createForm, setCreateForm] = useState({ protocolId: "", title: "", phase: "", piUsername: "" });
  const [createError, setCreateError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const study = useAsync<Study | null>(
    () =>
      selectedId != null
        ? client.request(`/research/studies/${selectedId}`)
        : Promise.resolve(null),
    [client, selectedId],
  );

  async function createStudy() {
    setBusy(true);
    setCreateError(null);
    try {
      const created = await client.request<Study>("/research/studies", {
        method: "POST",
        body: JSON.stringify(createForm),
      });
      setCreateForm({ protocolId: "", title: "", phase: "", piUsername: "" });
      studies.reload();
      setSelectedId(created.id);
    } catch (err) {
      setCreateError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <ModuleHeader
        icon="microscope"
        group="Data Platform"
        title="Research"
        purpose="Studies, coverage analysis, research billing, approvals and genomics — with signed protocol approval."
      />
      <p className="hint">
        Studies, coverage analysis and research billing. A protocol can only be approved with a
        21 CFR Part 11 e-signature over the exact record snapshot, and a study cannot activate
        until every procedure has a coverage determination.
      </p>
      <div className="card">
        <h3>Studies</h3>
        {studies.loading && <Loading label="Loading studies…" />}
        {studies.error != null && <ErrorView error={studies.error} />}
        {studies.data && studies.data.items.length === 0 && (
          <Empty label="No studies yet. Create the first protocol below." />
        )}
        <ul className="result-list">
          {studies.data?.items.map((s) => (
            <li key={s.id} className="result-row">
              <button type="button" onClick={() => setSelectedId(s.id)}>
                {s.protocol_id} — {s.title}
              </button>
              <StatusBadge tone={studyTone(s.status)}>{s.status}</StatusBadge>
              <StatusBadge tone={s.approved ? "ok" : "warn"}>
                {s.approved ? "protocol approved (signed)" : "not approved"}
              </StatusBadge>
            </li>
          ))}
        </ul>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void createStudy();
          }}
        >
          <h4>New study</h4>
          <label>
            Protocol ID
            <input
              value={createForm.protocolId}
              onChange={(e) => setCreateForm({ ...createForm, protocolId: e.target.value })}
              required
            />
          </label>
          <label>
            Title
            <input
              value={createForm.title}
              onChange={(e) => setCreateForm({ ...createForm, title: e.target.value })}
              required
            />
          </label>
          <label>
            Phase
            <input
              value={createForm.phase}
              onChange={(e) => setCreateForm({ ...createForm, phase: e.target.value })}
            />
          </label>
          <label>
            Principal investigator (username)
            <input
              value={createForm.piUsername}
              onChange={(e) => setCreateForm({ ...createForm, piUsername: e.target.value })}
            />
          </label>
          <button type="submit" disabled={busy || !createForm.protocolId || !createForm.title}>
            Create study
          </button>
          {createError && (
            <p className="form-error" role="alert">
              {createError}
            </p>
          )}
        </form>
      </div>

      {selectedId != null && study.loading && <Loading label="Loading study…" />}
      {study.error != null && <ErrorView error={study.error} />}
      {study.data && (
        <StudyDetail
          key={study.data.id}
          client={client}
          study={study.data}
          onChanged={() => {
            study.reload();
            studies.reload();
          }}
        />
      )}
    </section>
  );
}

function StudyDetail({
  client,
  study,
  onChanged,
}: {
  client: ApiClient;
  study: Study;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState("coverage");
  return (
    <div className="card">
      <h3>
        {study.protocol_id} — {study.title}{" "}
        <StatusBadge tone={studyTone(study.status)}>{study.status}</StatusBadge>
      </h3>
      <p className="hint">
        PI: {study.pi_username} · phase: {study.phase || "—"} · record snapshot hash:{" "}
        <code>{study.snapshot_hash}</code>
      </p>
      <Tabs
        ariaLabel={`Study ${study.protocol_id} sections`}
        tabs={[
          { key: "coverage", label: "Procedures & Coverage" },
          { key: "billing", label: "Billing Grid" },
          { key: "approvals", label: "Approvals" },
          { key: "genomics", label: "Genomics" },
        ]}
        activeKey={tab}
        onChange={setTab}
        panels={{
          coverage: <CoverageTab client={client} study={study} onChanged={onChanged} />,
          billing: <BillingTab client={client} study={study} />,
          approvals: <ApprovalsTab client={client} study={study} onChanged={onChanged} />,
          genomics: <GenomicsTab client={client} />,
        }}
      />
    </div>
  );
}

function CoverageTab({
  client,
  study,
  onChanged,
}: {
  client: ApiClient;
  study: Study;
  onChanged: () => void;
}) {
  const coverage = useAsync<Coverage>(
    () => client.request(`/research/studies/${study.id}/coverage`),
    [client, study.id],
  );
  const [procForm, setProcForm] = useState({ procedureCode: "", description: "" });
  const [detForms, setDetForms] = useState<Record<number, { determination: string; rationale: string }>>({});
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function addProcedure() {
    setBusy(true);
    setError(null);
    try {
      await client.request(`/research/studies/${study.id}/procedures`, {
        method: "POST",
        body: JSON.stringify(procForm),
      });
      setProcForm({ procedureCode: "", description: "" });
      coverage.reload();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function determine(procedureId: number) {
    const form = detForms[procedureId] ?? { determination: "research", rationale: "" };
    setBusy(true);
    setError(null);
    try {
      await client.request(`/research/procedures/${procedureId}/determination`, {
        method: "POST",
        body: JSON.stringify(form),
      });
      coverage.reload();
      onChanged();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {coverage.loading && <Loading label="Loading coverage analysis…" />}
      {coverage.error != null && <ErrorView error={coverage.error} />}
      {coverage.data && !coverage.data.coverage_complete && (
        <div className="state state-error" role="alert">
          <strong>Activation is blocked.</strong> These procedures have no coverage determination
          yet: {coverage.data.undetermined.map((u) => u.procedure_code).join(", ") || "—"}. Every
          procedure on an active study must be determined research, clinical or mixed first
          (BD-42).
        </div>
      )}
      {coverage.data?.items.map((proc) => (
        <div key={proc.id} className="card">
          <h4>
            {proc.procedure_code} — {proc.description || "no description"}
          </h4>
          {proc.determination ? (
            <p role="status">
              Determination:{" "}
              <StatusBadge tone={proc.determination.determination === "research" ? "info" : proc.determination.determination === "mixed" ? "warn" : "ok"}>
                {proc.determination.determination}
              </StatusBadge>{" "}
              by {proc.determination.determined_by}
              {proc.determination.signature_id ? ` · e-signed (signature ${proc.determination.signature_id})` : ""}
              <br />
              Rationale: {proc.determination.rationale || "—"}
            </p>
          ) : (
            <form
              noValidate
              onSubmit={(e) => {
                e.preventDefault();
                void determine(proc.id);
              }}
            >
              <p>
                <StatusBadge tone="bad">undetermined — blocks activation</StatusBadge>
              </p>
              <label>
                Determination
                <select
                  value={(detForms[proc.id]?.determination ?? "research")}
                  onChange={(e) =>
                    setDetForms({
                      ...detForms,
                      [proc.id]: { determination: e.target.value, rationale: detForms[proc.id]?.rationale ?? "" },
                    })
                  }
                >
                  <option value="research">research</option>
                  <option value="clinical">clinical</option>
                  <option value="mixed">mixed</option>
                </select>
              </label>
              <label>
                Rationale
                <input
                  value={detForms[proc.id]?.rationale ?? ""}
                  onChange={(e) =>
                    setDetForms({
                      ...detForms,
                      [proc.id]: { determination: detForms[proc.id]?.determination ?? "research", rationale: e.target.value },
                    })
                  }
                />
              </label>
              <button type="submit" disabled={busy}>
                Record determination
              </button>
            </form>
          )}
        </div>
      ))}
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void addProcedure();
        }}
      >
        <h4>Add procedure</h4>
        <label>
          Procedure code
          <input
            value={procForm.procedureCode}
            onChange={(e) => setProcForm({ ...procForm, procedureCode: e.target.value })}
            required
          />
        </label>
        <label>
          Description
          <input
            value={procForm.description}
            onChange={(e) => setProcForm({ ...procForm, description: e.target.value })}
          />
        </label>
        <button type="submit" disabled={busy || !procForm.procedureCode}>
          Add procedure
        </button>
      </form>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function BillingTab({ client, study }: { client: ApiClient; study: Study }) {
  const grid = useAsync<Grid>(
    () => client.request(`/research/studies/${study.id}/billing-grid`),
    [client, study.id],
  );
  const [lineForm, setLineForm] = useState({ procedureId: "", chargeCode: "", amount: "", billedTo: "research" });
  const [error, setError] = useState<string | null>(null);
  const [releaseNote, setReleaseNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const procedures = study.procedures ?? [];

  async function addLine() {
    setBusy(true);
    setError(null);
    try {
      await client.request(`/research/studies/${study.id}/grid-lines`, {
        method: "POST",
        body: JSON.stringify({
          procedureId: Number(lineForm.procedureId),
          chargeCode: lineForm.chargeCode,
          amount: Number(lineForm.amount),
          billedTo: lineForm.billedTo,
        }),
      });
      setLineForm({ procedureId: "", chargeCode: "", amount: "", billedTo: "research" });
      grid.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function release() {
    setBusy(true);
    setError(null);
    try {
      const out = await client.request<{ research_total: number; standard_total: number; grand_total: number; line_count: number }>(
        `/research/studies/${study.id}/billing-grid/release`,
        { method: "POST" },
      );
      setReleaseNote(
        `Grid released to the revenue cycle as a tagged export: ${out.line_count} line(s), research ${money(out.research_total)}, standard of care ${money(out.standard_total)}, grand total ${money(out.grand_total)}.`,
      );
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      {grid.loading && <Loading label="Loading billing grid…" />}
      {grid.error != null && <ErrorView error={grid.error} />}
      {grid.data && grid.data.lines.length === 0 && (
        <Empty label="No billing-grid lines yet. Add a charge line per procedure below." />
      )}
      {grid.data && grid.data.lines.length > 0 && (
        <>
          <table className="data-table">
            <thead>
              <tr>
                <th>Charge code</th>
                <th>Procedure</th>
                <th>Amount</th>
                <th>Billed to</th>
              </tr>
            </thead>
            <tbody>
              {grid.data.lines.map((line) => (
                <tr key={line.id}>
                  <td>{line.charge_code}</td>
                  <td>{line.procedure_code ?? "—"}</td>
                  <td className="num">{money(line.amount)}</td>
                  <td>
                    <StatusBadge tone={line.billed_to === "research" ? "info" : "ok"}>
                      {line.billed_to === "research" ? "research" : "standard of care"}
                    </StatusBadge>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p role="status">
            Research total: <strong>{money(grid.data.research_total)}</strong> · Standard-of-care
            total: <strong>{money(grid.data.standard_total)}</strong> · Grand total:{" "}
            <strong>{money(grid.data.grand_total)}</strong>
          </p>
          <button type="button" disabled={busy} onClick={() => void release()}>
            Release billing grid
          </button>
        </>
      )}
      {releaseNote && <p role="status">{releaseNote}</p>}
      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void addLine();
        }}
      >
        <h4>Add grid line</h4>
        <label>
          Procedure
          <select
            value={lineForm.procedureId}
            onChange={(e) => setLineForm({ ...lineForm, procedureId: e.target.value })}
            disabled={procedures.length === 0}
          >
            <option value="">Select a procedure…</option>
            {procedures.map((p) => (
              <option key={p.id} value={p.id}>
                {p.procedure_code}
              </option>
            ))}
          </select>
          {procedures.length === 0 && (
            <span className="field-error">No procedures on this study yet — add one under Procedures &amp; Coverage first.</span>
          )}
        </label>
        <label>
          Charge code
          <input
            value={lineForm.chargeCode}
            onChange={(e) => setLineForm({ ...lineForm, chargeCode: e.target.value })}
            required
          />
        </label>
        <label>
          Amount
          <input
            value={lineForm.amount}
            onChange={(e) => setLineForm({ ...lineForm, amount: e.target.value })}
            inputMode="decimal"
            required
          />
        </label>
        <label>
          Billed to
          <select
            value={lineForm.billedTo}
            onChange={(e) => setLineForm({ ...lineForm, billedTo: e.target.value })}
          >
            <option value="research">research</option>
            <option value="standard">standard of care</option>
          </select>
        </label>
        <button type="submit" disabled={busy || !lineForm.procedureId || !lineForm.chargeCode || !lineForm.amount}>
          Add line
        </button>
      </form>
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function ApprovalsTab({
  client,
  study,
  onChanged,
}: {
  client: ApiClient;
  study: Study;
  onChanged: () => void;
}) {
  const snapshot = useAsync<{ snapshot_hash: string; snapshot: Record<string, unknown> }>(
    () => client.request(`/esign/snapshot/ResearchStudy/${study.id}`),
    [client, study.id],
  );
  const manifest = useAsync<{ manifestation: { signer: string; meaning: string; timestamp: string; binding: string } } | null>(
    async () => {
      if (!study.approved) return null;
      try {
        return await client.request<{ manifestation: { signer: string; meaning: string; timestamp: string; binding: string } }>(
          `/esign/manifest/ResearchStudy/${study.id}`,
        );
      } catch {
        return null;
      }
    },
    [client, study.id, study.approved],
  );
  const [meaning, setMeaning] = useState("I approve this protocol");
  const [error, setError] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);

  async function signAndApprove() {
    if (!snapshot.data) return;
    setBusy(true);
    setError(null);
    try {
      const signature = await client.request<{ id: number }>("/esign/sign", {
        method: "POST",
        body: JSON.stringify({
          recordType: "ResearchStudy",
          recordId: String(study.id),
          meaning,
          snapshotHash: snapshot.data.snapshot_hash,
        }),
      });
      await client.request(`/research/studies/${study.id}/approve`, {
        method: "POST",
        body: JSON.stringify({ signatureId: signature.id }),
      });
      onChanged();
    } catch (err) {
      // A 409 from /esign/sign means the record changed after the
      // snapshot was taken — surface it inline, never silently re-sign.
      setError(errorText(err));
      snapshot.reload();
    } finally {
      setBusy(false);
    }
  }

  async function activate() {
    setBusy(true);
    setError(null);
    setBlocked(null);
    try {
      await client.request(`/research/studies/${study.id}/activate`, { method: "POST" });
      onChanged();
    } catch (err) {
      const undetermined = undeterminedFromError(err);
      if (undetermined) setBlocked(undetermined);
      else setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p role="status">
        Protocol approval:{" "}
        {study.approved ? (
          <StatusBadge tone="ok">approved — e-signature {study.approval_signature_id} on file</StatusBadge>
        ) : (
          <StatusBadge tone="warn">not approved — an e-signature is required</StatusBadge>
        )}
      </p>
      {manifest.data && (
        <p className="hint">
          Signature manifestation (21 CFR Part 11 §11.50): signed by{" "}
          {manifest.data.manifestation.signer} · meaning “{manifest.data.manifestation.meaning}” ·{" "}
          {manifest.data.manifestation.timestamp} · binding hash{" "}
          <code>{manifest.data.manifestation.binding}</code>
        </p>
      )}
      {!study.approved && (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void signAndApprove();
          }}
        >
          <p className="hint">
            You are signing the record exactly as snapshotted. Snapshot hash:{" "}
            <code>{snapshot.data?.snapshot_hash ?? study.snapshot_hash}</code>. If the study is
            edited after this snapshot, signing is refused (409) and a fresh snapshot is taken.
          </p>
          {snapshot.error != null && <ErrorView error={snapshot.error} />}
          <label>
            Signature meaning
            <input value={meaning} onChange={(e) => setMeaning(e.target.value)} required />
          </label>
          <button type="submit" disabled={busy || !snapshot.data || !meaning}>
            Sign &amp; approve protocol
          </button>
        </form>
      )}
      {study.status === "draft" && (
        <p>
          <button type="button" disabled={busy} onClick={() => void activate()}>
            Activate study
          </button>
        </p>
      )}
      {blocked && (
        <div className="state state-error" role="alert">
          <strong>Activation is blocked.</strong> These procedures still have no coverage
          determination: {blocked.join(", ")}. Record determinations under Procedures &amp;
          Coverage, then activate.
        </div>
      )}
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
    </div>
  );
}

function GenomicsTab({ client }: { client: ApiClient }) {
  const [patientId, setPatientId] = useState("");
  const variants = useAsync<{ variants: Variant[]; total: number } | null>(
    () =>
      patientId
        ? client.request(`/genomics/patients/${encodeURIComponent(patientId)}/variants`)
        : Promise.resolve(null),
    [client, patientId],
  );
  const [form, setForm] = useState({
    gene: "",
    hgvsC: "",
    hgvsP: "",
    zygosity: "het",
    interpretation: "pathogenic",
    sourceLab: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [codes, setCodes] = useState("");
  const [findings, setFindings] = useState<PgxFinding[] | null>(null);
  const [pgxError, setPgxError] = useState<string | null>(null);

  async function recordVariant() {
    setBusy(true);
    setError(null);
    try {
      await client.request("/genomics/variants", {
        method: "POST",
        body: JSON.stringify({ patientRef: `Patient/${patientId}`, ...form }),
      });
      setForm({ gene: "", hgvsC: "", hgvsP: "", zygosity: "het", interpretation: "pathogenic", sourceLab: "" });
      variants.reload();
    } catch (err) {
      setError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  async function pgxCheck() {
    setBusy(true);
    setPgxError(null);
    try {
      const out = await client.request<{ findings: PgxFinding[] }>("/genomics/pgx-check", {
        method: "POST",
        body: JSON.stringify({
          patientRef: `Patient/${patientId}`,
          medicationCodes: codes.split(",").map((c) => c.trim()).filter(Boolean),
        }),
      });
      setFindings(out.findings);
    } catch (err) {
      setPgxError(errorText(err));
      setFindings(null);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <p className="hint">
        Reported variant results only — there is no variant-calling pipeline; variants arrive as
        results from a source laboratory and are recorded as FHIR Observations.
      </p>
      <label>
        Patient ID
        <input value={patientId} onChange={(e) => setPatientId(e.target.value)} />
      </label>
      {patientId && variants.loading && <Loading label="Loading reported variants…" />}
      {variants.error != null && <ErrorView error={variants.error} />}
      {variants.data && variants.data.variants.length === 0 && (
        <Empty label="No reported variants recorded for this patient." />
      )}
      <ul className="result-list">
        {variants.data?.variants.map((v) => (
          <li key={v.observation_id} className="result-row">
            <span className="result-type">
              {v.gene ?? "?"} {v.hgvs_c ?? ""}
            </span>
            <span className="result-meta">
              {v.hgvs_p ?? ""} · {v.zygosity ?? ""} · {v.interpretation ?? ""}
              {v.source_lab ? ` · reported by ${v.source_lab}` : ""}
            </span>
          </li>
        ))}
      </ul>

      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void recordVariant();
        }}
      >
        <h4>Record a reported variant</h4>
        <label>
          Gene
          <input value={form.gene} onChange={(e) => setForm({ ...form, gene: e.target.value })} required />
        </label>
        <label>
          HGVS c.
          <input value={form.hgvsC} onChange={(e) => setForm({ ...form, hgvsC: e.target.value })} required />
        </label>
        <label>
          HGVS p. (optional)
          <input value={form.hgvsP} onChange={(e) => setForm({ ...form, hgvsP: e.target.value })} />
        </label>
        <label>
          Zygosity
          <select value={form.zygosity} onChange={(e) => setForm({ ...form, zygosity: e.target.value })}>
            <option value="het">het</option>
            <option value="hom">hom</option>
            <option value="hemi">hemi</option>
          </select>
        </label>
        <label>
          Interpretation
          <select
            value={form.interpretation}
            onChange={(e) => setForm({ ...form, interpretation: e.target.value })}
          >
            <option value="pathogenic">pathogenic</option>
            <option value="likely-pathogenic">likely-pathogenic</option>
            <option value="vus">vus</option>
            <option value="likely-benign">likely-benign</option>
            <option value="benign">benign</option>
          </select>
        </label>
        <label>
          Source laboratory
          <input value={form.sourceLab} onChange={(e) => setForm({ ...form, sourceLab: e.target.value })} />
        </label>
        <button type="submit" disabled={busy || !patientId || !form.gene || !form.hgvsC}>
          Record variant
        </button>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
      </form>

      <form
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          void pgxCheck();
        }}
      >
        <h4>Pharmacogenomic (PGx) check</h4>
        <label>
          Medication codes (comma-separated RxNorm)
          <input value={codes} onChange={(e) => setCodes(e.target.value)} />
        </label>
        <button type="submit" disabled={busy || !patientId || !codes.trim()}>
          Run PGx check
        </button>
        {pgxError && (
          <p className="form-error" role="alert">
            {pgxError}
          </p>
        )}
      </form>
      {findings != null &&
        (findings.length === 0 ? (
          <Empty label="No PGx findings: none of this patient's reported variants trigger a governed rule for those medications." />
        ) : (
          <ul className="result-list">
            {findings.map((f, i) => (
              <li key={i} className="result-row">
                <span className="result-type">
                  {f.gene} {f.variant ?? ""} × drug {f.drug}
                </span>
                <span className="result-meta">{f.message}</span>
                <StatusBadge tone={actionTone(f.action)}>{f.action}</StatusBadge>
              </li>
            ))}
          </ul>
        ))}
    </div>
  );
}
