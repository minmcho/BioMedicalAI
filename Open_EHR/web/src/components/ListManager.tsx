import { useEffect, useState } from "react";
import { ApiError, type ApiClient } from "../api/client";
import {
  getManagedList,
  putListDraft,
  type ListConcept,
  type ManagedList,
} from "../api/lists";
import { StatusBadge } from "./StatusBadge";

function errorText(err: unknown): string {
  if (err instanceof ApiError) return err.detail;
  return err instanceof Error ? err.message : String(err);
}

function cloneConcepts(concepts: ListConcept[]): ListConcept[] {
  return concepts.map((c) => ({ ...c }));
}

/** Governed dropdown-list manager (Phase 15, workstream F).
 *
 *  Backend: backend/app/listmgmt.py. The affordance is INVISIBLE to
 *  unauthorized roles — when the payload's caller_can_manage is
 *  false this component renders null (a hard requirement, not a
 *  disabled button). When true it renders a "Manage {label}" button
 *  opening an inline editor over the list's concepts:
 *
 *  - edits land on the governed DRAFT only (PUT .../draft); a human
 *    publishes in System Studio — the success notice is the
 *    backend's own notice text from the payload;
 *  - DELETE is deactivation: a published code is never hard-deleted
 *    (the UI offers deactivate/reactivate only for published codes);
 *    a code that exists only in the draft may be removed outright;
 *  - server errors surface the server detail verbatim (notably the
 *    422 that refuses to drop a published code).
 *
 *  Prop note: the list key travels as `listKey`, not `key` — React
 *  reserves `key` as a special prop and strips it from JSX props
 *  (verified empirically on this repo's React 19; coordinator
 *  ratified the rename in the Phase 15 integration pass).
 */
export function ListManager({
  client,
  kind,
  listKey,
  label,
}: {
  client: ApiClient;
  kind: string;
  listKey: string;
  label: string;
}) {
  const [data, setData] = useState<ManagedList | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState(false);
  const [concepts, setConcepts] = useState<ListConcept[]>([]);
  const [newCode, setNewCode] = useState("");
  const [newDisplay, setNewDisplay] = useState("");
  const [addError, setAddError] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveNotice, setSaveNotice] = useState<string | null>(null);
  const [savedVersion, setSavedVersion] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    getManagedList(client, kind, listKey)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        const source = result.draft
          ? result.draft.concepts
          : result.published.concepts;
        setConcepts(cloneConcepts(source));
        setLoading(false);
      })
      .catch((err) => {
        if (cancelled) return;
        setLoadError(errorText(err));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [client, kind, listKey]);

  // Hard requirement: invisible to unauthorized roles. Also renders
  // nothing while loading (the manage basis is not known yet) and
  // when the list cannot be loaded at all (e.g. the caller lacks
  // studio.read, so no manage basis can be established) — the
  // affordance simply does not appear. Save errors, by contrast,
  // surface the server detail in the editor below.
  if (loading) return null;
  if (loadError) return null;
  if (!data || !data.caller_can_manage) return null;

  const publishedCodes = new Set(
    data.published.concepts.map((c) => c.code),
  );
  const notice = data.notice;

  function patchConcept(code: string, patch: Partial<ListConcept>) {
    setConcepts((prev) =>
      prev.map((c) => (c.code === code ? { ...c, ...patch } : c)),
    );
    setSaveNotice(null);
    setSavedVersion(null);
  }

  function addConcept() {
    const code = newCode.trim();
    const display = newDisplay.trim();
    if (!code) {
      setAddError("A code is required for a new entry.");
      return;
    }
    if (concepts.some((c) => c.code === code)) {
      setAddError(`Code "${code}" is already in the list.`);
      return;
    }
    setAddError(null);
    setConcepts((prev) => [
      ...prev,
      { code, display: display || code, active: true },
    ]);
    setNewCode("");
    setNewDisplay("");
    setSaveNotice(null);
    setSavedVersion(null);
  }

  function removeDraftConcept(code: string) {
    setConcepts((prev) => prev.filter((c) => c.code !== code));
    setSaveNotice(null);
    setSavedVersion(null);
  }

  async function save() {
    setSaving(true);
    setSaveError(null);
    setSaveNotice(null);
    setSavedVersion(null);
    try {
      const result = await putListDraft(client, kind, listKey, concepts);
      setConcepts(cloneConcepts(result.concepts));
      // The backend's draft-only notice, verbatim from the payload:
      // changes take effect only when published in Studio.
      setSaveNotice(result.notice);
      setSavedVersion(result.draft_version);
    } catch (err) {
      // Surface the server detail verbatim (e.g. the 422 refusing
      // to hard-delete a published code).
      setSaveError(errorText(err));
    } finally {
      setSaving(false);
    }
  }

  if (!open) {
    return (
      <button type="button" onClick={() => setOpen(true)}>
        Manage {label}
      </button>
    );
  }

  return (
    <section aria-label={`Manage ${label}`} className="card">
      <h3>Manage {label}</h3>
      <p className="hint">{notice}</p>
      <p className="hint">
        Published v{data.published.version}
        {data.draft
          ? ` · Draft v${data.draft.version} pending — editing the draft`
          : " · No draft yet — editing starts a new draft"}
        {data.manage_basis ? ` · Basis: ${data.manage_basis}` : ""}
      </p>

      <ul className="patient-list">
        {concepts.map((concept) => {
          const isPublished = publishedCodes.has(concept.code);
          const isActive = concept.active !== false;
          return (
            <li key={concept.code}>
              <div className="patient-row-line">
                <span className="patient-name">{concept.code}</span>
                <label className="field">
                  Display for {concept.code}
                  <input
                    aria-label={`Display for ${concept.code}`}
                    value={concept.display ?? ""}
                    onChange={(e) =>
                      patchConcept(concept.code, {
                        display: e.target.value,
                      })
                    }
                  />
                </label>
                {isActive ? (
                  <StatusBadge tone="ok">Active</StatusBadge>
                ) : (
                  <StatusBadge tone="warn">Inactive</StatusBadge>
                )}
                {isPublished ? (
                  <button
                    type="button"
                    className="linklike"
                    onClick={() =>
                      patchConcept(concept.code, { active: !isActive })
                    }
                  >
                    {isActive
                      ? `Deactivate ${concept.code}`
                      : `Reactivate ${concept.code}`}
                  </button>
                ) : (
                  <>
                    <button
                      type="button"
                      className="linklike"
                      onClick={() =>
                        patchConcept(concept.code, { active: !isActive })
                      }
                    >
                      {isActive
                        ? `Deactivate ${concept.code}`
                        : `Reactivate ${concept.code}`}
                    </button>
                    <button
                      type="button"
                      className="linklike"
                      onClick={() => removeDraftConcept(concept.code)}
                    >
                      Remove {concept.code}
                    </button>
                  </>
                )}
              </div>
            </li>
          );
        })}
      </ul>
      {concepts.length === 0 && (
        <p className="state state-empty" role="status">
          No concepts in this list yet.
        </p>
      )}

      <div className="inline-form">
        <label className="field">
          New code
          <input
            aria-label="New code"
            value={newCode}
            onChange={(e) => setNewCode(e.target.value)}
            placeholder="e.g. new-code"
          />
        </label>
        <label className="field">
          New display
          <input
            aria-label="New display"
            value={newDisplay}
            onChange={(e) => setNewDisplay(e.target.value)}
            placeholder="e.g. New entry"
          />
        </label>
        <button type="button" onClick={addConcept}>
          Add concept
        </button>
      </div>
      {addError && (
        <p className="error-note" role="alert">
          {addError}
        </p>
      )}

      <div className="row-actions">
        <button type="button" onClick={() => void save()} disabled={saving}>
          Save draft
        </button>
        <button
          type="button"
          className="linklike"
          onClick={() => setOpen(false)}
        >
          Close editor
        </button>
      </div>
      {saveNotice && (
        <p className="state state-empty" role="status">
          Draft saved{savedVersion !== null ? ` (draft v${savedVersion})` : ""}
          . {saveNotice}
        </p>
      )}
      {saveError && (
        <p className="error-note" role="alert">
          {saveError}
        </p>
      )}
    </section>
  );
}
