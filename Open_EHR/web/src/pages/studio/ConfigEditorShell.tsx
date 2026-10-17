import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { ApiClient } from "../../api/client";
import { ApiError } from "../../api/client";
import type { ConfigKind, StudioConfigObject } from "../../api/types";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";
import { configActions } from "./helpers";

// Shared governance shell for every visual Studio editor tab.
//
// The lifecycle is the engine's, unchanged: everything saves as a DRAFT
// version through the /studio/config endpoints; publishing is a separate
// action; published versions are never edited in place — opening one
// loads its payload as the base for a NEW draft. Version history with
// publish / disable / rollback sits under every editor.

type WorkingBase =
  | { mode: "new" }
  | { mode: "draft"; version: number }
  | { mode: "from"; version: number; status: string };

export interface ConfigEditorShellProps<M> {
  client: ApiClient;
  kind: ConfigKind;
  /** When set, the object key is fixed (e.g. permissions/role-matrix). */
  fixedKey?: string;
  /** Placeholder suggestion for a brand-new key. */
  newKeyHint?: string;
  parse: (payload: Record<string, unknown>) => M;
  serialize: (model: M) => Record<string, unknown>;
  empty: () => M;
  /** Local validation run before save; return an error message or null. */
  validate?: (model: M) => string | null;
  children: (model: M, setModel: (m: M) => void) => ReactNode;
}

export function ConfigEditorShell<M>(props: ConfigEditorShellProps<M>) {
  const { client, kind, fixedKey, newKeyHint, parse, serialize, empty, validate } = props;
  const [reloadTick, setReloadTick] = useState(0);
  const [keyChoice, setKeyChoice] = useState<string | null>(null);
  const [newKeyText, setNewKeyText] = useState("");
  const [model, setModel] = useState<M | null>(null);
  const [base, setBase] = useState<WorkingBase>({ mode: "new" });
  const [basePayload, setBasePayload] = useState<string>("");
  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [initToken, setInitToken] = useState("");

  const { data, loading, error } = useAsync<StudioConfigObject[]>(
    () => client.listConfigs(kind),
    [client, kind, reloadTick],
  );

  const keys = useMemo(() => {
    if (!data) return [];
    return [...new Set(data.map((c) => c.key))].sort();
  }, [data]);

  const creating = keyChoice === "__new__";
  const effectiveKey = fixedKey ?? (creating ? newKeyText.trim() : (keyChoice ?? keys[0] ?? ""));

  const versions = useMemo(() => {
    if (!data || !effectiveKey) return [];
    return data
      .filter((c) => c.kind === kind && c.key === effectiveKey)
      .sort((a, b) => a.version - b.version);
  }, [data, effectiveKey]);

  // (Re)initialise the editor whenever the selected object or its version
  // set changes: work on the open draft if one exists, else on the
  // published version as the base for a new draft, else an empty model.
  const versionToken = versions.map((v) => `${v.id}:${v.status}`).join(",");
  useEffect(() => {
    if (loading || !data) return;
    const token = `${effectiveKey}|${versionToken}`;
    if (token === initToken) return;
    setInitToken(token);
    const draft = [...versions].reverse().find((v) => v.status === "draft");
    const published = versions.find((v) => v.status === "published");
    const latest = versions[versions.length - 1];
    const source = draft ?? published ?? latest;
    if (source) {
      setModel(parse(source.payload));
      setBasePayload(JSON.stringify(source.payload));
      setBase(
        source.status === "draft"
          ? { mode: "draft", version: source.version }
          : { mode: "from", version: source.version, status: source.status },
      );
    } else {
      setModel(empty());
      setBasePayload("");
      setBase({ mode: "new" });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effectiveKey, versionToken, loading, data]);

  const dirty =
    model !== null && JSON.stringify(serialize(model)) !== (basePayload || JSON.stringify(serialize(empty())));

  async function run(key: string, fn: () => Promise<unknown>, okNotice?: string) {
    setBusy(key);
    setActionError(null);
    setNotice(null);
    try {
      await fn();
      if (okNotice) setNotice(okNotice);
      setReloadTick((t) => t + 1);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : "Action failed.");
    } finally {
      setBusy(null);
    }
  }

  async function saveDraft(): Promise<StudioConfigObject | null> {
    if (!model || !effectiveKey) return null;
    const problem = validate?.(model) ?? null;
    if (problem) {
      setActionError(problem);
      return null;
    }
    const payload = serialize(model);
    if (base.mode === "draft") {
      return client.updateConfigDraft(kind, effectiveKey, base.version, payload);
    }
    return client.createConfigDraft(kind, effectiveKey, payload);
  }

  async function onSaveDraft() {
    setBusy("save");
    setActionError(null);
    setNotice(null);
    try {
      const saved = await saveDraft();
      if (saved) {
        setNotice(`Draft v${saved.version} saved. It is designer-only until published.`);
        setReloadTick((t) => t + 1);
      }
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : "Save failed.");
    } finally {
      setBusy(null);
    }
  }

  async function onPublish() {
    setBusy("publish");
    setActionError(null);
    setNotice(null);
    try {
      let version: number | null = null;
      if (base.mode === "draft" && !dirty) {
        version = base.version;
      } else {
        const saved = await saveDraft();
        if (!saved) return; // validation error already surfaced
        version = saved.version;
      }
      const published = await client.publishConfig(kind, effectiveKey, version);
      setNotice(`Published v${published.version} — now the effective ${kind}/${effectiveKey}.`);
      setReloadTick((t) => t + 1);
    } catch (err) {
      setActionError(err instanceof ApiError ? err.detail : "Publish failed.");
    } finally {
      setBusy(null);
    }
  }

  function loadVersion(v: StudioConfigObject) {
    setModel(parse(v.payload));
    setBasePayload(JSON.stringify(v.payload));
    setBase(
      v.status === "draft"
        ? { mode: "draft", version: v.version }
        : { mode: "from", version: v.version, status: v.status },
    );
    setInitToken(`${effectiveKey}|loaded-${v.id}`);
    setNotice(
      v.status === "draft"
        ? `Editing draft v${v.version}.`
        : `Loaded v${v.version} (${v.status}) — saving creates a new draft.`,
    );
    setActionError(null);
  }

  if (loading && !data) return <Loading label={`Loading ${kind} configuration…`} />;
  if (error) return <ErrorView error={error} />;
  if (!data) return <Empty label="No configuration data." />;

  const published = versions.find((v) => v.status === "published");

  return (
    <div>
      {!fixedKey ? (
        <p className="studio-keybar">
          <label>
            {kind} key{" "}
            <select
              aria-label={`${kind} config key`}
              value={creating ? "__new__" : effectiveKey}
              onChange={(e) => {
                setKeyChoice(e.target.value);
                setInitToken("");
              }}
            >
              {keys.map((k) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
              <option value="__new__">＋ New key…</option>
            </select>
          </label>{" "}
          {creating ? (
            <input
              aria-label="New config key"
              value={newKeyText}
              onChange={(e) => setNewKeyText(e.target.value)}
              placeholder={newKeyHint ?? "e.g. intake"}
            />
          ) : null}{" "}
          <span className="badge">
            {published ? `effective: v${published.version}` : "nothing published"}
          </span>{" "}
          <span className="hint">
            {base.mode === "draft"
              ? `editing draft v${base.version}`
              : base.mode === "from"
                ? `based on v${base.version} (${base.status}) — saving creates a new draft`
                : "new object — saving creates draft v1"}
            {dirty ? " · unsaved changes" : ""}
          </span>
        </p>
      ) : (
        <p className="studio-keybar">
          <span className="badge">
            {kind}/{fixedKey} — {published ? `effective: v${published.version}` : "nothing published"}
          </span>{" "}
          <span className="hint">
            {base.mode === "draft"
              ? `editing draft v${base.version}`
              : base.mode === "from"
                ? `based on v${base.version} (${base.status}) — saving creates a new draft`
                : "no saved matrix yet — the grid shows the built-in defaults"}
            {dirty ? " · unsaved changes" : ""}
          </span>
        </p>
      )}

      {actionError ? (
        <div className="state state-error" role="alert">
          <strong>Action failed.</strong> {actionError}
        </div>
      ) : null}
      {notice ? (
        <div className="state state-ok" role="status">
          {notice}
        </div>
      ) : null}

      {model !== null ? props.children(model, (m) => setModel(m)) : <Loading label="Preparing editor…" />}

      <p className="studio-actions">
        <button
          type="button"
          disabled={busy !== null || !effectiveKey || model === null}
          onClick={onSaveDraft}
        >
          Save draft
        </button>{" "}
        <button
          type="button"
          disabled={busy !== null || !effectiveKey || model === null}
          onClick={onPublish}
        >
          Publish
        </button>{" "}
        <span className="hint">
          Drafts are designer-only. Publishing supersedes the previous published version;
          published versions are immutable.
        </span>
      </p>

      {versions.length > 0 ? (
        <>
          <h4>Version history — {kind}/{effectiveKey}</h4>
          <ul className="config-versions">
            {versions.map((v) => (
              <li key={v.id}>
                <span>
                  v{v.version} — {v.status} (by {v.updated_by || v.created_by})
                </span>{" "}
                <button type="button" onClick={() => loadVersion(v)}>
                  Load
                </button>{" "}
                {configActions(v.status).includes("publish") ? (
                  <button
                    type="button"
                    disabled={busy === `pub-${v.id}`}
                    onClick={() =>
                      run(`pub-${v.id}`, () => client.publishConfig(kind, effectiveKey, v.version),
                        `Published v${v.version}.`)
                    }
                  >
                    Publish
                  </button>
                ) : null}{" "}
                {configActions(v.status).includes("disable") ? (
                  <button
                    type="button"
                    disabled={busy === `dis-${v.id}`}
                    onClick={() =>
                      run(`dis-${v.id}`, () => client.disableConfig(kind, effectiveKey, v.version),
                        `Disabled v${v.version}.`)
                    }
                  >
                    Disable
                  </button>
                ) : null}{" "}
                {configActions(v.status).includes("rollback") ? (
                  <button
                    type="button"
                    disabled={busy === `rb-${v.id}`}
                    onClick={() =>
                      run(`rb-${v.id}`, () => client.rollbackConfig(kind, effectiveKey, v.version),
                        `Rolled back: v${v.version} republished as the new effective version.`)
                    }
                  >
                    Roll back to v{v.version}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </div>
  );
}
