import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import {
  emptyTerminology,
  parseTerminology,
  serializeTerminology,
  terminologyError,
  type ConceptModel,
  type TerminologyModel,
} from "./models";

// Terminology tab — concept-list editor in the lab catalog's shape
// (backend/app/lab.py): code, display, unit, and a reference range with
// low/high plus critical low/high. Extra concept data the editor does
// not model (a panel's `components`, `panel` flag) is preserved through
// the round-trip by the model layer.

function TerminologyEditor({
  model,
  setModel,
}: {
  model: TerminologyModel;
  setModel: (m: TerminologyModel) => void;
}) {
  const setConcept = (index: number, patch: Partial<ConceptModel>) => {
    setModel({
      ...model,
      concepts: model.concepts.map((c, i) => (i === index ? { ...c, ...patch } : c)),
    });
  };

  const numField = (
    index: number,
    field: "low" | "high" | "criticalLow" | "criticalHigh",
    label: string,
  ) => (
    <input
      aria-label={`Concept ${index + 1} ${label}`}
      value={model.concepts[index][field]}
      inputMode="decimal"
      size={6}
      onChange={(e) => setConcept(index, { [field]: e.target.value })}
    />
  );

  return (
    <div className="studio-editor">
      <p>
        <label>
          List name{" "}
          <input
            aria-label="Terminology list name"
            value={model.name}
            onChange={(e) => setModel({ ...model, name: e.target.value })}
          />
        </label>{" "}
        <label>
          Code system{" "}
          <input
            aria-label="Terminology code system"
            value={model.system}
            onChange={(e) => setModel({ ...model, system: e.target.value })}
            placeholder="e.g. http://loinc.org"
          />
        </label>
      </p>

      <table className="studio-table">
        <thead>
          <tr>
            <th>Code</th>
            <th>Display</th>
            <th>Unit</th>
            <th>Ref low</th>
            <th>Ref high</th>
            <th>Crit low</th>
            <th>Crit high</th>
            <th>Active</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {model.concepts.map((c, i) => (
            <tr key={i}>
              <td>
                <input
                  aria-label={`Concept ${i + 1} code`}
                  value={c.code}
                  size={10}
                  onChange={(e) => setConcept(i, { code: e.target.value })}
                />
              </td>
              <td>
                <input
                  aria-label={`Concept ${i + 1} display`}
                  value={c.display}
                  size={28}
                  onChange={(e) => setConcept(i, { display: e.target.value })}
                />
              </td>
              <td>
                <input
                  aria-label={`Concept ${i + 1} unit`}
                  value={c.unit}
                  size={8}
                  onChange={(e) => setConcept(i, { unit: e.target.value })}
                />
              </td>
              <td>{numField(i, "low", "reference low")}</td>
              <td>{numField(i, "high", "reference high")}</td>
              <td>{numField(i, "criticalLow", "critical low")}</td>
              <td>{numField(i, "criticalHigh", "critical high")}</td>
              <td>
                <input
                  type="checkbox"
                  aria-label={`Concept ${i + 1} active`}
                  checked={c.active}
                  onChange={(e) => setConcept(i, { active: e.target.checked })}
                />
              </td>
              <td>
                <button
                  type="button"
                  aria-label={`Remove concept ${i + 1}`}
                  onClick={() =>
                    setModel({ ...model, concepts: model.concepts.filter((_, j) => j !== i) })
                  }
                >
                  ✕
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        <button
          type="button"
          onClick={() =>
            setModel({
              ...model,
              concepts: [
                ...model.concepts,
                { code: "", display: "", active: true, unit: "", low: "", high: "",
                  criticalLow: "", criticalHigh: "", rest: {} },
              ],
            })
          }
        >
          Add concept
        </button>
      </p>
    </div>
  );
}

export function TerminologyTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<TerminologyModel>
      client={client}
      kind="terminology"
      newKeyHint="e.g. lab-catalog"
      parse={parseTerminology}
      serialize={serializeTerminology}
      empty={emptyTerminology}
      validate={terminologyError}
    >
      {(model, setModel) => <TerminologyEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
