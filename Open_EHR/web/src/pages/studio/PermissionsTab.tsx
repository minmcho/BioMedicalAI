import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import {
  ALL_ROLES,
  CAPABILITY_DEFAULTS,
  emptyPermissions,
  parsePermissions,
  serializePermissions,
  type PermissionMatrix,
} from "./models";

// Permissions tab — the role/capability matrix (kind=permissions,
// key=role-matrix). The grid starts from the built-in defaults merged
// with the saved config; saving writes the full matrix as a new draft.
// Enforcement is server-side: the API merges the published matrix over
// its defaults in require_capability — this tab edits the configuration
// record, it is not itself the gate.

function PermissionsEditor({
  model,
  setModel,
}: {
  model: PermissionMatrix;
  setModel: (m: PermissionMatrix) => void;
}) {
  const capabilities = Object.keys(CAPABILITY_DEFAULTS);
  return (
    <div className="studio-editor">
      <p className="hint">
        Permission matrix configuration. The API enforces the published matrix server-side,
        merged over the built-in defaults, and the web shell reads the same published
        matrix at sign-in to gate its pages and actions — so UI and server agree.
        Toggling a cell here changes the saved configuration once the draft is published.
        New capability names can be defined in the Advanced (JSON) editor; only
        enforcement points are code.
      </p>
      <table className="studio-table studio-matrix">
        <thead>
          <tr>
            <th>Capability</th>
            {ALL_ROLES.map((role) => (
              <th key={role}>{role}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {capabilities.map((capability) => (
            <tr key={capability}>
              <td>{capability}</td>
              {ALL_ROLES.map((role) => (
                <td key={role}>
                  <input
                    type="checkbox"
                    aria-label={`${capability} for ${role}`}
                    checked={model[capability]?.[role] === true}
                    onChange={(e) =>
                      setModel({
                        ...model,
                        [capability]: { ...model[capability], [role]: e.target.checked },
                      })
                    }
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function PermissionsTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<PermissionMatrix>
      client={client}
      kind="permissions"
      fixedKey="role-matrix"
      parse={parsePermissions}
      serialize={serializePermissions}
      empty={emptyPermissions}
    >
      {(model, setModel) => <PermissionsEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
