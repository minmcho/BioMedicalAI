import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import { RolesInput, RowControls, moveItem } from "./fields";
import {
  emptyWorkflow,
  parseWorkflow,
  serializeWorkflow,
  type WorkflowModel,
} from "./models";

// Workflows tab — state machines as structured rows: a states list (one
// initial, any number of terminal states) and transitions with from/to,
// an action label and the roles allowed to fire them. Serialized to the
// {states, transitions, initial?, terminal?} payload the backend
// validates and the ADT/lab/board machines run from.

function WorkflowEditor({
  model,
  setModel,
}: {
  model: WorkflowModel;
  setModel: (m: WorkflowModel) => void;
}) {
  const stateNames = model.states.map((s) => s.name).filter(Boolean);

  return (
    <div className="studio-editor">
      <h4>States</h4>
      {model.states.map((state, i) => (
        <p key={i} className="studio-row">
          <input
            aria-label={`State ${i + 1} name`}
            value={state.name}
            onChange={(e) =>
              setModel({
                ...model,
                states: model.states.map((s, j) => (j === i ? { ...s, name: e.target.value } : s)),
              })
            }
          />{" "}
          <label>
            <input
              type="radio"
              name="workflow-initial"
              aria-label={`State ${i + 1} initial`}
              checked={state.initial}
              onChange={() =>
                setModel({
                  ...model,
                  states: model.states.map((s, j) => ({ ...s, initial: j === i })),
                })
              }
            />{" "}
            Initial
          </label>{" "}
          <label>
            <input
              type="checkbox"
              aria-label={`State ${i + 1} terminal`}
              checked={state.terminal}
              onChange={(e) =>
                setModel({
                  ...model,
                  states: model.states.map((s, j) =>
                    j === i ? { ...s, terminal: e.target.checked } : s,
                  ),
                })
              }
            />{" "}
            Terminal
          </label>{" "}
          <RowControls
            index={i}
            count={model.states.length}
            label={`state ${i + 1}`}
            onMove={(idx, delta) =>
              setModel({ ...model, states: moveItem(model.states, idx, delta) })
            }
            onRemove={(idx) =>
              setModel({ ...model, states: model.states.filter((_, j) => j !== idx) })
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
              states: [...model.states, { name: "", initial: false, terminal: false }],
            })
          }
        >
          Add state
        </button>
      </p>

      <h4>Transitions</h4>
      {model.transitions.map((t, i) => (
        <p key={i} className="studio-row">
          <label>
            From{" "}
            <select
              aria-label={`Transition ${i + 1} from`}
              value={t.from}
              onChange={(e) =>
                setModel({
                  ...model,
                  transitions: model.transitions.map((x, j) =>
                    j === i ? { ...x, from: e.target.value } : x,
                  ),
                })
              }
            >
              <option value="">—</option>
              {stateNames.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>{" "}
          <label>
            To{" "}
            <select
              aria-label={`Transition ${i + 1} to`}
              value={t.to}
              onChange={(e) =>
                setModel({
                  ...model,
                  transitions: model.transitions.map((x, j) =>
                    j === i ? { ...x, to: e.target.value } : x,
                  ),
                })
              }
            >
              <option value="">—</option>
              {stateNames.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>{" "}
          <label>
            Action / label{" "}
            <input
              aria-label={`Transition ${i + 1} action`}
              value={t.action}
              onChange={(e) =>
                setModel({
                  ...model,
                  transitions: model.transitions.map((x, j) =>
                    j === i ? { ...x, action: e.target.value } : x,
                  ),
                })
              }
            />
          </label>{" "}
          <RolesInput
            label={`Transition ${i + 1} roles`}
            roles={t.roles}
            onCommit={(roles) =>
              setModel({
                ...model,
                transitions: model.transitions.map((x, j) => (j === i ? { ...x, roles } : x)),
              })
            }
          />{" "}
          <button
            type="button"
            aria-label={`Remove transition ${i + 1}`}
            onClick={() =>
              setModel({ ...model, transitions: model.transitions.filter((_, j) => j !== i) })
            }
          >
            ✕
          </button>
        </p>
      ))}
      <p>
        <button
          type="button"
          onClick={() =>
            setModel({
              ...model,
              transitions: [
                ...model.transitions,
                { from: stateNames[0] ?? "", to: "", action: "", roles: [], rest: {} },
              ],
            })
          }
        >
          Add transition
        </button>
      </p>
    </div>
  );
}

export function WorkflowsTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<WorkflowModel>
      client={client}
      kind="workflow"
      newKeyHint="e.g. appointment-adt"
      parse={parseWorkflow}
      serialize={serializeWorkflow}
      empty={emptyWorkflow}
    >
      {(model, setModel) => <WorkflowEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
