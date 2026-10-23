import { useState } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type {
  ComplianceEvidence,
  ComplianceRequirement,
  ReviewQueueResponse,
  StudioConfigObject,
} from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

/** Compliance tab — the regulatory framework as data (Phase 10,
 *  Track J). The requirements register links each obligation to the
 *  control as implemented and to an EVIDENCE SOURCE resolved live
 *  against the hash-chained audit log — an auditor checks the
 *  evidence, not a claim. Regulatory packs are governed config whose
 *  parameters drive real behaviour (the published HIPAA pack sets the
 *  break-the-glass clocks below). The privacy review queue lists
 *  EVERY unreviewed break-the-glass opening: 100% review, never
 *  sampled. */
export function ComplianceTab({ client }: { client: ApiClient }) {
  const [reloadKey, setReloadKey] = useState(0);
  const register = useAsync<{ items: ComplianceRequirement[]; total: number }>(
    () => client.request("/compliance/requirements"),
    [client, reloadKey],
  );
  const queue = useAsync<ReviewQueueResponse>(
    () => client.request<ReviewQueueResponse>("/privacy/review-queue"),
    [client, reloadKey],
  );
  const packs = useAsync<StudioConfigObject[]>(
    () => client.listConfigs("regulatory_pack"),
    [client, reloadKey],
  );
  const [notice, setNotice] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<ComplianceEvidence | null>(null);
  const [form, setForm] = useState({
    body: "", citation: "", title: "", control: "",
    implementation_ref: "", evidence_source: "",
  });

  if (register.loading || queue.loading || packs.loading) {
    return <Loading label="Loading compliance…" />;
  }
  if (register.error) return <ErrorView error={register.error} />;
  if (queue.error) return <ErrorView error={queue.error} />;
  if (packs.error) return <ErrorView error={packs.error} />;

  const policy = queue.data?.policy;

  async function act(fn: () => Promise<unknown>, ok: string) {
    setNotice(null);
    try {
      await fn();
      setNotice(ok);
      setReloadKey((k) => k + 1);
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function showEvidence(req: ComplianceRequirement) {
    setEvidence(null);
    try {
      const ev = await client.request<ComplianceEvidence>(
        `/compliance/requirements/${req.id}/evidence`,
      );
      setEvidence(ev);
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function createRequirement() {
    await act(
      () =>
        client.request("/compliance/requirements", {
          method: "POST",
          body: JSON.stringify(form),
        }),
      "Requirement added to the register.",
    );
    setForm({ body: "", citation: "", title: "", control: "",
              implementation_ref: "", evidence_source: "" });
  }

  return (
    <div>
      <p className="hint">
        Regulation is configuration: obligations live in this register as
        data, each tied to its implementation and to evidence the system
        itself produces (audit-chain entries, published config). Regulatory
        packs parameterise the workflows — publishing the HIPAA pack, for
        example, sets the break-the-glass clocks shown below.
      </p>
      {notice && <p className="hint" role="status">{notice}</p>}

      {policy && (
        <section>
          <h3>Break-the-glass policy</h3>
          <p>
            Emergency access lasts <strong>{policy.duration_minutes} minutes</strong>;
            the opener must justify it within{" "}
            <strong>{policy.justification_window_hours} hours</strong>. Review
            expectation: <strong>{policy.review_expectation}</strong> Source:{" "}
            {policy.source}.
          </p>
        </section>
      )}

      <h3>Privacy review queue — break-the-glass</h3>
      {(queue.data?.items ?? []).length === 0 ? (
        <Empty label="No unreviewed break-the-glass openings. Every opening lands here until a reviewer dispositions it." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Patient</th>
              <th>Opened by</th>
              <th>Reason</th>
              <th>Justification due</th>
              <th>Review</th>
            </tr>
          </thead>
          <tbody>
            {(queue.data?.items ?? []).map((item) => (
              <tr key={item.access_id}>
                <td>{item.patient_ref}</td>
                <td>{item.actor}</td>
                <td>{item.reason}</td>
                <td>
                  {item.justification_due_at
                    ? new Date(item.justification_due_at).toLocaleString()
                    : "—"}
                  {item.overdue ? " — OVERDUE" : ""}
                </td>
                <td>
                  <button
                    type="button"
                    onClick={() =>
                      void act(
                        () =>
                          client.request(
                            `/privacy/review-queue/${item.access_id}/review`,
                            {
                              method: "POST",
                              body: JSON.stringify({ outcome: "justified" }),
                            },
                          ),
                        `Opening ${item.access_id.slice(0, 8)} reviewed: justified.`,
                      )
                    }
                  >
                    Justified
                  </button>{" "}
                  <button
                    type="button"
                    onClick={() =>
                      void act(
                        () =>
                          client.request(
                            `/privacy/review-queue/${item.access_id}/review`,
                            {
                              method: "POST",
                              body: JSON.stringify({ outcome: "unjustified" }),
                            },
                          ),
                        `Opening ${item.access_id.slice(0, 8)} reviewed: unjustified.`,
                      )
                    }
                  >
                    Unjustified
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Regulatory packs</h3>
      {(packs.data ?? []).length === 0 ? (
        <Empty label="No regulatory packs." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Pack</th>
              <th>Version</th>
              <th>Status</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {(packs.data ?? []).map((p) => (
              <tr key={`${p.key}-${p.version}`}>
                <td>{p.key}</td>
                <td>v{p.version}</td>
                <td>{p.status}</td>
                <td>
                  {p.status === "draft" && (
                    <button
                      type="button"
                      onClick={() =>
                        void act(
                          () =>
                            client.publishConfig(
                              "regulatory_pack", p.key, p.version),
                          `Published ${p.key} v${p.version}.`,
                        )
                      }
                    >
                      Publish
                    </button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3>Requirements register</h3>
      {(register.data?.items ?? []).length === 0 ? (
        <Empty label="The register is empty." />
      ) : (
        <table className="studio-table">
          <thead>
            <tr>
              <th>Body</th>
              <th>Citation</th>
              <th>Title</th>
              <th>Control</th>
              <th>Evidence source</th>
              <th>Evidence</th>
            </tr>
          </thead>
          <tbody>
            {(register.data?.items ?? []).map((req) => (
              <tr key={req.id}>
                <td>{req.body}</td>
                <td>{req.citation}</td>
                <td>{req.title}</td>
                <td>{req.control}</td>
                <td>
                  <code>{req.evidence_source || "—"}</code>
                </td>
                <td>
                  <button type="button" onClick={() => void showEvidence(req)}>
                    View evidence
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {evidence && (
        <div>
          <h4>
            Evidence for requirement #{evidence.requirement_id} (
            {evidence.source || "no source declared"})
          </h4>
          {evidence.kind === "audit" && (
            <>
              <p className="hint">
                {evidence.total} matching audit-chain entries, newest first:
              </p>
              <ul>
                {(evidence.entries ?? []).map((e, i) => (
                  <li key={i}>
                    <code>{String(e.action)}</code> — {String(e.entity)} by{" "}
                    {String(e.actor)} at {String(e.created_at)}
                  </li>
                ))}
              </ul>
            </>
          )}
          {evidence.kind === "config" && (
            <p>
              Published config: {evidence.config?.kind}/{evidence.config?.key}{" "}
              v{evidence.config?.version} ({evidence.config?.status}).
            </p>
          )}
          {evidence.kind === "reference" && <p>{evidence.note}</p>}
          {evidence.kind === "none" && (
            <p className="error-note">
              No evidence source is declared for this requirement — that gap is
              itself the finding.
            </p>
          )}
        </div>
      )}

      <h3>Add a requirement</h3>
      <p>
        {(
          [
            ["body", "Body (e.g. HIPAA)"],
            ["citation", "Citation"],
            ["title", "Title"],
            ["control", "Control description"],
            ["implementation_ref", "Implementation reference"],
            ["evidence_source", "Evidence source (audit:<prefix> / config:<kind>/<key> / file:<path>)"],
          ] as const
        ).map(([field, label]) => (
          <label key={field} style={{ display: "block" }}>
            {label}{" "}
            <input
              aria-label={label}
              value={form[field]}
              onChange={(e) => setForm({ ...form, [field]: e.target.value })}
            />
          </label>
        ))}
      </p>
      <p>
        <button
          type="button"
          disabled={!form.body.trim() || !form.citation.trim() || !form.control.trim()}
          onClick={() => void createRequirement()}
        >
          Add requirement
        </button>
      </p>
    </div>
  );
}
