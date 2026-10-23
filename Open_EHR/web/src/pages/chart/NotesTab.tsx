import { useEffect, useState } from "react";
import type { ApiClient } from "../../api/client";
import type { FormDefinition, QuestionnaireAnswerItem } from "../../api/types";
import { FormRenderer } from "../../components/FormRenderer";
import { RichTextEditor } from "../../components/RichTextEditor";
import { RichTextView } from "../../components/RichTextView";
import { htmlToText } from "../../components/richtext";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

interface Props {
  client: ApiClient;
  patientId: string;
  /** notes.write in the runtime permissions matrix for the role. */
  canWrite: boolean;
}

/** Notes tab: author from the note type's published form template
 *  (rendered by the shared FormRenderer), sign, and amend. Amending
 *  creates a new linked DocumentReference server-side — the original
 *  stays untouched, and both appear in the list. */
export function NotesTab({ client, patientId, canWrite }: Props) {
  const { data, loading, error, reload } = useAsync(async () => {
    const [types, list] = await Promise.all([
      client.noteTypes(),
      client.notes(patientId),
    ]);
    return {
      types: types.note_types.concepts.filter((c) => c.active !== false),
      notes: list.items,
    };
  }, [client, patientId]);

  const [noteType, setNoteType] = useState("progress");
  const [form, setForm] = useState<FormDefinition | null>(null);
  const [text, setText] = useState("");
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [amendId, setAmendId] = useState<string | null>(null);
  const [amendText, setAmendText] = useState("");

  const templateKey = data?.types.find((t) => t.code === noteType)?.templateForm;

  useEffect(() => {
    let cancelled = false;
    if (!templateKey) {
      setForm(null);
      return;
    }
    client
      .effectiveConfig("form", templateKey)
      .then((cfg) => {
        if (!cancelled) setForm(cfg.payload as unknown as FormDefinition);
      })
      .catch(() => {
        if (!cancelled) setForm(null);
      });
    return () => {
      cancelled = true;
    };
  }, [client, templateKey]);

  if (loading) return <Loading label="Loading notes…" />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No note data." />;

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setActionError(null);
    try {
      await fn();
      reload();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "Note action failed");
    } finally {
      setBusy(false);
    }
  };

  const createFromAnswers = async (answers: QuestionnaireAnswerItem[]) => {
    await client.createNote({
      patient: patientId,
      noteType,
      ...(answers.length > 0 ? { answers } : {}),
      // Free text is rich HTML now; "present" means it has text, not
      // just markup (the server sanitizes it again on write).
      ...(htmlToText(text).trim() ? { text } : {}),
    });
    setText("");
    reload();
  };

  return (
    <section aria-label="Notes">
      {canWrite && (
        <div className="note-author">
          <h4>New note</h4>
          <div className="form-field">
            <label htmlFor="note-type">Note type</label>
            <select
              id="note-type"
              value={noteType}
              onChange={(e) => setNoteType(e.target.value)}
            >
              {data.types.map((t) => (
                <option key={t.code} value={t.code}>
                  {t.display ?? t.code}
                </option>
              ))}
            </select>
          </div>
          <div className="form-field">
            <label htmlFor="note-text">Free text (optional)</label>
            <RichTextEditor
              id="note-text"
              value={text}
              ariaLabel="Free text (optional)"
              onChange={setText}
            />
          </div>
          {form ? (
            <FormRenderer
              key={noteType}
              form={form}
              submitLabel="Create note"
              onSubmit={createFromAnswers}
            />
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await client.createNote({
                    patient: patientId,
                    noteType,
                    ...(htmlToText(text).trim() ? { text } : {}),
                  });
                  setText("");
                })
              }
            >
              Create note
            </button>
          )}
        </div>
      )}

      {actionError && (
        <p role="alert" className="form-error">
          {actionError}
        </p>
      )}

      <h4>Notes</h4>
      {data.notes.length === 0 ? (
        <Empty label="No notes yet." />
      ) : (
        <ul className="note-list">
          {data.notes.map((note) => (
            <li key={note.id}>
              <span>
                {(note.type?.text ?? note.noteType ?? "Note")}{" "}
                <span className="badge">{note.docStatus ?? note.status}</span>
                {note.date ? ` · ${note.date.slice(0, 10)}` : ""}
              </span>
              {note.noteContent?.text ? (
                <RichTextView html={note.noteContent.text} />
              ) : null}
              {canWrite && note.id && note.docStatus === "preliminary" && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => run(() => client.signNote(note.id!))}
                >
                  Sign
                </button>
              )}
              {canWrite &&
                note.id &&
                (note.docStatus === "final" || note.docStatus === "amended") && (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      setAmendId(note.id!);
                      setAmendText("");
                    }}
                  >
                    Amend
                  </button>
                )}
            </li>
          ))}
        </ul>
      )}

      {amendId && (
        <div className="note-amend">
          <h4>Amend note</h4>
          <p>
            Amending creates a new version linked to the original; the
            original is not changed.
          </p>
          <div className="form-field">
            <label htmlFor="amend-text">Amended text</label>
            <RichTextEditor
              id="amend-text"
              value={amendText}
              ariaLabel="Amended text"
              onChange={setAmendText}
            />
          </div>
          <button
            type="button"
            disabled={busy || !htmlToText(amendText).trim()}
            onClick={() =>
              run(async () => {
                await client.amendNote(amendId, { text: amendText });
                setAmendId(null);
              })
            }
          >
            Save amendment
          </button>
          <button type="button" onClick={() => setAmendId(null)}>
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}
