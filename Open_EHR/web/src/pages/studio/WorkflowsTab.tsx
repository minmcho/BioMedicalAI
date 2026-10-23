import { useEffect, useState } from "react";
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
// validates (configstore.validate_workflow: `states` is a list of plain
// state-name strings; each transition is {from, to, action?, roles?}
// with from/to drawn from the declared states) and the ADT/lab/board
// machines run from.
//
// Studio parity row 7: a List/Canvas toggle. The canvas renders the
// SAME WorkflowModel draft the list editor edits — states as boxes,
// transitions as labelled SVG edges — with no forked model. Clicking a
// state in the canvas selects it (shared selection state) and returns
// to the list with that state's row highlighted and scrolled into
// view. The canvas never mutates structure.

const BOX_W = 150;
const BOX_H = 44;
const GAP = 36;
const PAD = 16;
const BOX_Y = 16;

function WorkflowCanvas({
  model,
  selectedIndex,
  onSelect,
}: {
  model: WorkflowModel;
  selectedIndex: number | null;
  onSelect: (index: number) => void;
}) {
  const states = model.states;
  const width = Math.max(320, PAD * 2 + states.length * BOX_W + Math.max(0, states.length - 1) * GAP);
  const height = 190;
  const xOf = (i: number) => PAD + i * (BOX_W + GAP);
  const indexOf = (name: string) => states.findIndex((s) => s.name === name);

  return (
    <div style={{ overflowX: "auto" }}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width={width}
        height={height}
        role="img"
        aria-label="Workflow canvas"
      >
        <defs>
          <marker
            id="wf-arrow"
            markerWidth="8"
            markerHeight="8"
            refX="7"
            refY="3"
            orient="auto"
          >
            <path d="M0,0 L7,3 L0,6 Z" fill="#5b6b76" />
          </marker>
        </defs>
        {model.transitions.map((t, ti) => {
          const from = indexOf(t.from);
          const to = indexOf(t.to);
          if (from < 0 || to < 0) return null;
          const x1 = xOf(from) + BOX_W / 2;
          const x2 = xOf(to) + BOX_W / 2;
          const depth = BOX_Y + BOX_H + 34 + (ti % 3) * 22;
          const label = t.action || `${t.from} → ${t.to}`;
          return (
            <g key={ti}>
              <path
                d={`M ${x1} ${BOX_Y + BOX_H} C ${x1} ${depth}, ${x2} ${depth}, ${x2} ${BOX_Y + BOX_H + 2}`}
                fill="none"
                stroke="#5b6b76"
                strokeWidth="1.5"
                markerEnd="url(#wf-arrow)"
              />
              <text
                x={(x1 + x2) / 2}
                y={depth + 14}
                fontSize="11"
                fill="#33424e"
                textAnchor="middle"
              >
                {label}
              </text>
            </g>
          );
        })}
        {states.map((state, i) => {
          const selected = i === selectedIndex;
          return (
            <g
              key={i}
              role="button"
              aria-label={`Select state ${state.name || `state ${i + 1}`}`}
              aria-pressed={selected}
              data-selected={selected ? "true" : undefined}
              onClick={() => onSelect(i)}
              style={{ cursor: "pointer" }}
            >
              <rect
                x={xOf(i)}
                y={BOX_Y}
                width={BOX_W}
                height={BOX_H}
                rx="6"
                fill={selected ? "#0b6e4f" : "#ffffff"}
                stroke={selected ? "#0b6e4f" : "#5b6b76"}
                strokeWidth={selected ? 2.5 : 1.5}
              />
              <text
                x={xOf(i) + BOX_W / 2}
                y={BOX_Y + 20}
                fontSize="12"
                fill={selected ? "#ffffff" : "#1c2b36"}
                textAnchor="middle"
              >
                {(state.name || `state ${i + 1}`).slice(0, 20)}
              </text>
              <text
                x={xOf(i) + BOX_W / 2}
                y={BOX_Y + 36}
                fontSize="9"
                fill={selected ? "#d9efe6" : "#5b6b76"}
                textAnchor="middle"
              >
                {state.initial ? "initial" : state.terminal ? "terminal" : " "}
              </text>
            </g>
          );
        })}
      </svg>
      {states.length === 0 ? <p className="hint">No states yet — add them in the list.</p> : null}
    </div>
  );
}

function WorkflowEditor({
  model,
  setModel,
}: {
  model: WorkflowModel;
  setModel: (m: WorkflowModel) => void;
}) {
  const stateNames = model.states.map((s) => s.name).filter(Boolean);
  const [view, setView] = useState<"list" | "canvas">("list");
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

  // After a canvas click returns us to the list, bring the selected
  // state's row into view (guarded: not every DOM implements it).
  useEffect(() => {
    if (view !== "list" || selectedIndex === null) return;
    const el = document.getElementById(`workflow-state-row-${selectedIndex}`);
    if (el && typeof el.scrollIntoView === "function") {
      try {
        el.scrollIntoView({ block: "nearest" });
      } catch {
        /* scrolling is a nicety, never a failure */
      }
    }
  }, [view, selectedIndex]);

  const selectFromCanvas = (index: number) => {
    setSelectedIndex(index);
    setView("list");
  };

  return (
    <div className="studio-editor">
      <p className="studio-view-toggle">
        <button
          type="button"
          aria-pressed={view === "list"}
          onClick={() => setView("list")}
        >
          List view
        </button>{" "}
        <button
          type="button"
          aria-pressed={view === "canvas"}
          onClick={() => setView("canvas")}
        >
          Canvas view
        </button>
      </p>

      {view === "canvas" ? (
        <>
          <p className="hint">
            Canvas is a view of the same draft — edit stages in the list. Click a
            state to select it in the list editor.
          </p>
          <WorkflowCanvas
            model={model}
            selectedIndex={selectedIndex}
            onSelect={selectFromCanvas}
          />
        </>
      ) : (
        <>
      <h4>States</h4>
      {model.states.map((state, i) => (
        <p
          key={i}
          id={`workflow-state-row-${i}`}
          className="studio-row"
          data-selected={i === selectedIndex ? "true" : undefined}
          style={
            i === selectedIndex
              ? { background: "#e6f4ea", outline: "2px solid #0b6e4f" }
              : undefined
          }
        >
          <input
            aria-label={`State ${i + 1} name`}
            value={state.name}
            onFocus={() => setSelectedIndex(i)}
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
        </>
      )}
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
