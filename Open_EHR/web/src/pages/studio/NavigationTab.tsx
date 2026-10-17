import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import { RolesInput, RowControls, moveItem } from "./fields";
import {
  emptyNavigation,
  parseNavigation,
  serializeNavigation,
  type NavItemModel,
  type NavigationModel,
} from "./models";

// Navigation tab — menu rows of {module, label, roles[]}. Modules ONLY:
// pages and forms live inside modules and can never be menu entries —
// the API rejects such payloads with HTTP 400 and the shell surfaces
// that error verbatim above the editor. Row order is the stored order.

const KNOWN_MODULES = [
  "patients", "inbox", "scheduling", "lab", "pophealth", "billing",
  "pharmacy", "ed", "radiology", "surgery", "studio", "portal",
];

function NavigationEditor({
  model,
  setModel,
}: {
  model: NavigationModel;
  setModel: (m: NavigationModel) => void;
}) {
  const setItem = (index: number, patch: Partial<NavItemModel>) => {
    setModel({
      ...model,
      items: model.items.map((it, i) => (i === index ? { ...it, ...patch } : it)),
    });
  };

  return (
    <div className="studio-editor">
      <p className="hint">
        Menu entries may only reference modules. Pages and forms live inside modules and
        cannot be menu entries — the server rejects them (HTTP 400) and the error appears
        above the editor.
      </p>
      <datalist id="studio-modules">
        {KNOWN_MODULES.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>
      {model.items.map((item, i) => (
        <p key={i} className="studio-row">
          <label>
            Module{" "}
            <input
              aria-label={`Nav item ${i + 1} module`}
              list="studio-modules"
              value={item.module}
              onChange={(e) => setItem(i, { module: e.target.value })}
            />
          </label>{" "}
          <label>
            Label{" "}
            <input
              aria-label={`Nav item ${i + 1} label`}
              value={item.label}
              onChange={(e) => setItem(i, { label: e.target.value })}
            />
          </label>{" "}
          <RolesInput
            label={`Nav item ${i + 1} roles`}
            roles={item.roles}
            onCommit={(roles) => setItem(i, { roles })}
          />{" "}
          <label>
            <input
              type="checkbox"
              aria-label={`Nav item ${i + 1} visible`}
              checked={item.visible}
              onChange={(e) => setItem(i, { visible: e.target.checked })}
            />{" "}
            Visible
          </label>{" "}
          <RowControls
            index={i}
            count={model.items.length}
            label={`nav item ${i + 1}`}
            onMove={(idx, delta) =>
              setModel({ ...model, items: moveItem(model.items, idx, delta) })
            }
            onRemove={(idx) =>
              setModel({ ...model, items: model.items.filter((_, j) => j !== idx) })
            }
          />
        </p>
      ))}
      <p>
        <button
          type="button"
          onClick={() =>
            setModel({
              ...model,
              items: [...model.items, { module: "", label: "", roles: [], visible: true, rest: {} }],
            })
          }
        >
          Add menu item
        </button>
      </p>
    </div>
  );
}

export function NavigationTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<NavigationModel>
      client={client}
      kind="navigation"
      newKeyHint="e.g. main"
      parse={parseNavigation}
      serialize={serializeNavigation}
      empty={emptyNavigation}
    >
      {(model, setModel) => <NavigationEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
