import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import { InterfacesPanel } from "./InterfacesPanel";
import {
  emptyIntegration,
  parseIntegration,
  serializeIntegration,
  type IntegrationModel,
} from "./models";

// Integration tab — external-system connection data as key/value rows.
// Paths are dotted (section.key): the seeded example is the radiology
// DICOM viewer (integration/dicom-viewer: viewer.baseUrl, viewer.name)
// that the radiology board builds its external viewer link from.

function IntegrationEditor({
  model,
  setModel,
}: {
  model: IntegrationModel;
  setModel: (m: IntegrationModel) => void;
}) {
  return (
    <div className="studio-editor">
      <p className="hint">
        One row per setting, as section.key — e.g. <code>viewer.baseUrl</code> for the
        external DICOM viewer's base URL. Non-scalar values in the saved config are
        preserved untouched.
      </p>
      {model.rows.map((row, i) => (
        <p key={i} className="studio-row">
          <label>
            Key{" "}
            <input
              aria-label={`Setting ${i + 1} key`}
              value={row.path}
              onChange={(e) =>
                setModel({
                  ...model,
                  rows: model.rows.map((r, j) => (j === i ? { ...r, path: e.target.value } : r)),
                })
              }
              placeholder="viewer.baseUrl"
            />
          </label>{" "}
          <label>
            Value{" "}
            <input
              aria-label={`Setting ${i + 1} value`}
              value={row.value}
              size={36}
              onChange={(e) =>
                setModel({
                  ...model,
                  rows: model.rows.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)),
                })
              }
            />
          </label>{" "}
          <button
            type="button"
            aria-label={`Remove setting ${i + 1}`}
            onClick={() => setModel({ ...model, rows: model.rows.filter((_, j) => j !== i) })}
          >
            ✕
          </button>
        </p>
      ))}
      <p>
        <button
          type="button"
          onClick={() => setModel({ ...model, rows: [...model.rows, { path: "", value: "" }] })}
        >
          Add setting
        </button>
      </p>
    </div>
  );
}

export function IntegrationTab({ client }: { client: ApiClient }) {
  return (
    <div>
      <ConfigEditorShell<IntegrationModel>
        client={client}
        kind="integration"
        newKeyHint="e.g. dicom-viewer"
        parse={parseIntegration}
        serialize={serializeIntegration}
        empty={emptyIntegration}
      >
        {(model, setModel) => <IntegrationEditor model={model} setModel={setModel} />}
      </ConfigEditorShell>
      <InterfacesPanel client={client} />
    </div>
  );
}
