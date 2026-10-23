import { useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import type { Role } from "../api/types";
import { RichTextEditor } from "../components/RichTextEditor";
import { htmlToText } from "../components/richtext";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";
import { ModuleHeader } from "../components/ModuleHeader";
import { Empty, ErrorView, Loading } from "../components/StatusViews";
import { useAsync } from "../hooks";

interface Props {
  client: ApiClient;
  role?: Role;
}

// Phase 16 (WS-G2 / K2) — document intake worklist (inbound documents /
// fax pattern). Documents are received, text-extracted, and the
// governed classifier agent SUGGESTS a patient + type; a human
// confirms before anything is filed into a chart. Confirming against a
// different patient than suggested requires a reason (the misfile
// safeguard — the server 422s without one, and the form checks first).
// Unfiling marks the filed copy entered-in-error; nothing is deleted.

function errorText(err: unknown): string {
  if (err instanceof ApiError) return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}

interface IntakeDoc {
  id: string;
  filename: string;
  mediaType: string;
  status: string;
  extraction_error: string | null;
  classification_note: string | null;
  suggestedPatientRef: string | null;
  suggestedType: string | null;
  classifierRunId: string | null;
  confirmedPatientRef: string | null;
  filedDocumentId: string | null;
  filedBy: string | null;
}

interface DocTypeConcept {
  code: string;
  display?: string;
  active?: boolean;
}

function docTone(status: string): StatusTone {
  if (status === "filed") return "ok";
  if (status === "extraction-failed") return "bad";
  if (status === "classified") return "info";
  return "warn";
}

function toBase64(text: string): string {
  return btoa(unescape(encodeURIComponent(text)));
}

export function DocumentsPage({ client }: Props) {
  const docs = useAsync<{ items: IntakeDoc[]; total: number }>(
    () => client.request("/intake/documents"),
    [client],
  );
  const docTypes = useAsync<DocTypeConcept[]>(
    async () => {
      try {
        const cfg = await client.request<{ payload: { concepts?: DocTypeConcept[] } }>(
          "/studio/config/terminology/effective/intake-doc-types",
        );
        return (cfg.payload.concepts ?? []).filter((c) => c.active !== false);
      } catch {
        return [];
      }
    },
    [client],
  );
  const [upload, setUpload] = useState({ filename: "", mediaType: "text/plain", content: "" });
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function receive() {
    setBusy(true);
    setUploadError(null);
    try {
      await client.request("/intake/documents", {
        method: "POST",
        body: JSON.stringify({
          filename: upload.filename,
          mediaType: upload.mediaType,
          contentBase64: toBase64(upload.content),
        }),
      });
      setUpload({ filename: "", mediaType: "text/plain", content: "" });
      docs.reload();
    } catch (err) {
      setUploadError(errorText(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section>
      <ModuleHeader icon="folder" group="Data Platform" title="Documents" purpose="Document intake and processing worklist" />
      <p className="hint">
        Inbound documents land here first. The classifier agent&apos;s patient/type is a{" "}
        <strong>suggestion only</strong> — a human confirms before a document is filed into a
        chart, and filing against a different patient than suggested requires a reason.
      </p>
      <div className="card">
        <h3>Receive a document</h3>
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void receive();
          }}
        >
          <label>
            Filename
            <input
              value={upload.filename}
              onChange={(e) => setUpload({ ...upload, filename: e.target.value })}
              required
            />
          </label>
          <label>
            Media type
            <select
              value={upload.mediaType}
              onChange={(e) => setUpload({ ...upload, mediaType: e.target.value })}
            >
              <option value="text/plain">text/plain</option>
              <option value="application/pdf">application/pdf</option>
            </select>
          </label>
          <label>
            Document text (the received bytes, pasted for this synthetic intake)
            <RichTextEditor
              ariaLabel="Document text"
              value={upload.content}
              onChange={(html) => setUpload({ ...upload, content: html })}
              minRows={4}
            />
          </label>
          <button type="submit" disabled={busy || !upload.filename || !htmlToText(upload.content).trim()}>
            Receive document
          </button>
          {uploadError && (
            <p className="form-error" role="alert">
              {uploadError}
            </p>
          )}
        </form>
      </div>

      <div className="card">
        <h3>Worklist</h3>
        {docs.loading && <Loading label="Loading intake worklist…" />}
        {docs.error != null && <ErrorView error={docs.error} />}
        {docs.data && docs.data.items.length === 0 && (
          <Empty label="The intake queue is empty. Received documents appear here for classification and confirmation." />
        )}
        {docs.data?.items.map((doc) => (
          <DocRow
            key={doc.id}
            client={client}
            doc={doc}
            docTypes={docTypes.data ?? []}
            onChanged={() => docs.reload()}
          />
        ))}
      </div>
    </section>
  );
}

function DocRow({
  client,
  doc,
  docTypes,
  onChanged,
}: {
  client: ApiClient;
  doc: IntakeDoc;
  docTypes: DocTypeConcept[];
  onChanged: () => void;
}) {
  const [patientRef, setPatientRef] = useState(doc.suggestedPatientRef ?? "");
  const [docType, setDocType] = useState(doc.suggestedType ?? docTypes[0]?.code ?? "");
  // The governed type list can arrive after this row mounts; while
  // the local state is still unset, the effective type is the
  // list's first code — the same value the select displays — so
  // the classify/confirm actions never sit disabled behind a
  // select that looks populated (latent dead control).
  const effectiveDocType = docType || docTypes[0]?.code || "";
  const [reason, setReason] = useState("");
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const differsFromSuggestion =
    doc.suggestedPatientRef != null && patientRef !== "" && patientRef !== doc.suggestedPatientRef;

  async function post(path: string, body: Record<string, unknown>) {
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

  function confirm() {
    setFieldError(null);
    if (differsFromSuggestion && !reason.trim()) {
      setFieldError(
        `You are filing to ${patientRef}, but the classifier suggested ${doc.suggestedPatientRef}. A reason is required to file against a different patient.`,
      );
      return;
    }
    void post(`/intake/documents/${encodeURIComponent(doc.id)}/confirm`, {
      patientRef,
      docType: effectiveDocType,
      ...(reason.trim() ? { reason: reason.trim() } : {}),
    });
  }

  return (
    <div className="card">
      <h4>
        {doc.filename} <StatusBadge tone={docTone(doc.status)}>{doc.status}</StatusBadge>
      </h4>
      <p className="hint">
        {doc.mediaType}
        {doc.classifierRunId ? ` · classifier run ${doc.classifierRunId}` : ""}
        {doc.classification_note ? ` · ${doc.classification_note}` : ""}
      </p>
      {doc.status === "extraction-failed" && (
        <p className="form-error" role="alert">
          Text extraction failed: {doc.extraction_error ?? "no reason recorded"}. The document is
          parked here — it cannot be classified or filed until extraction succeeds.
        </p>
      )}
      {(doc.suggestedPatientRef || doc.suggestedType) && doc.status !== "filed" && (
        <p role="status">
          Classifier suggestion (not filed): patient{" "}
          <strong>{doc.suggestedPatientRef ?? "—"}</strong> · type{" "}
          <strong>{doc.suggestedType ?? "—"}</strong>
        </p>
      )}
      {doc.status === "filed" && (
        <p role="status">
          Filed as DocumentReference/{doc.filedDocumentId} into {doc.confirmedPatientRef}
          {doc.filedBy ? ` by ${doc.filedBy}` : ""}.
        </p>
      )}

      {(doc.status === "awaiting-classification" || doc.status === "extracted" || doc.status === "received") && (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            void post(`/intake/documents/${encodeURIComponent(doc.id)}/classify`, {
              patientRef,
              docType: effectiveDocType,
            });
          }}
        >
          <h5>Classify manually</h5>
          <DocFields
            patientRef={patientRef}
            setPatientRef={setPatientRef}
            docType={effectiveDocType}
            setDocType={setDocType}
            docTypes={docTypes}
          />
          <button type="submit" disabled={busy || !patientRef || !effectiveDocType}>
            Record classification
          </button>
        </form>
      )}

      {doc.status === "classified" && (
        <form
          noValidate
          onSubmit={(e) => {
            e.preventDefault();
            confirm();
          }}
        >
          <h5>Confirm &amp; file into the chart</h5>
          <DocFields
            patientRef={patientRef}
            setPatientRef={setPatientRef}
            docType={effectiveDocType}
            setDocType={setDocType}
            docTypes={docTypes}
          />
          <label>
            Reason (required when the patient differs from the suggestion)
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          {fieldError && (
            <span className="field-error" role="alert">
              {fieldError}
            </span>
          )}
          <button type="submit" disabled={busy || !patientRef || !effectiveDocType}>
            Confirm &amp; file
          </button>
        </form>
      )}

      {doc.status === "filed" && (
        <div>
          <label>
            Unfile reason
            <input value={reason} onChange={(e) => setReason(e.target.value)} />
          </label>
          <button
            type="button"
            disabled={busy}
            onClick={() =>
              void post(`/intake/documents/${encodeURIComponent(doc.id)}/unfile`, {
                ...(reason.trim() ? { reason: reason.trim() } : {}),
              })
            }
          >
            Unfile — mark entered-in-error
          </button>
          <p className="hint">
            Unfiling never deletes: the filed copy is marked entered-in-error and stays readable;
            this document returns to the worklist ready to re-file.
          </p>
        </div>
      )}
      {!["awaiting-classification", "extracted", "received", "classified", "filed",
        "extraction-failed"].includes(doc.status) && (
        <p className="hint">
          No actions are available for a document in status “{doc.status}”.
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

function DocFields({
  patientRef,
  setPatientRef,
  docType,
  setDocType,
  docTypes,
}: {
  patientRef: string;
  setPatientRef: (v: string) => void;
  docType: string;
  setDocType: (v: string) => void;
  docTypes: DocTypeConcept[];
}) {
  return (
    <>
      <label>
        Patient reference
        <input
          value={patientRef}
          onChange={(e) => setPatientRef(e.target.value)}
          placeholder="Patient/<id>"
          required
        />
      </label>
      <label>
        Document type (governed terminology: intake-doc-types)
        <select value={docType} onChange={(e) => setDocType(e.target.value)} disabled={docTypes.length === 0}>
          {docTypes.length === 0 && <option value="">list unavailable</option>}
          {docTypes.map((t) => (
            <option key={t.code} value={t.code}>
              {t.display ?? t.code}
            </option>
          ))}
        </select>
        {docTypes.length === 0 && (
          <span className="field-error">
            The governed intake-doc-types list could not be loaded, so no type can be chosen.
          </span>
        )}
      </label>
    </>
  );
}
