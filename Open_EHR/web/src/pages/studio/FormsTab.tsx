import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import { RowControls, moveItem } from "./fields";
import {
  FORM_TYPE_OPTIONS,
  emptyForm,
  parseForm,
  serializeForm,
  type FormItemModel,
  type FormModel,
} from "./models";

// Forms Designer — visual editor for FHIR Questionnaire form configs.
// Items are edited as structured rows and serialized back to
// Questionnaire JSON ({resourceType, title, item[]}); choice options are
// answerOption valueCodings. Everything saves as a draft and publishes
// through the shell's governance controls.

function FormPreview({ model }: { model: FormModel }) {
  return (
    <div className="studio-preview" aria-label="Form preview">
      <h4>Preview{model.title ? ` — ${model.title}` : ""}</h4>
      {model.items.length === 0 ? (
        <p className="hint">No items yet.</p>
      ) : (
        model.items.map((item, i) => (
          <p key={i} className="studio-preview-field">
            <label>
              {item.text || item.linkId || `Item ${i + 1}`}
              {item.required ? <span aria-hidden="true"> *</span> : null}{" "}
              {item.type === "boolean" ? (
                <input type="checkbox" disabled />
              ) : item.type === "choice" ? (
                <select disabled>
                  <option value="">Select…</option>
                  {item.options.map((o, j) => (
                    <option key={j} value={o.code}>
                      {o.display || o.code}
                    </option>
                  ))}
                </select>
              ) : item.type === "date" ? (
                <input type="date" disabled />
              ) : item.type === "decimal" || item.type === "integer" ? (
                <input type="number" disabled />
              ) : (
                <input type="text" disabled />
              )}
            </label>
          </p>
        ))
      )}
      <p className="hint">Read-only preview of the published-form rendering.</p>
    </div>
  );
}

function FormEditor({ model, setModel }: { model: FormModel; setModel: (m: FormModel) => void }) {
  const setItem = (index: number, patch: Partial<FormItemModel>) => {
    setModel({
      ...model,
      items: model.items.map((it, i) => (i === index ? { ...it, ...patch } : it)),
    });
  };

  return (
    <div className="studio-editor">
      <p>
        <label>
          Form title{" "}
          <input
            aria-label="Form title"
            value={model.title}
            onChange={(e) => setModel({ ...model, title: e.target.value })}
            placeholder="e.g. Intake form"
          />
        </label>
      </p>

      {model.items.map((item, i) => {
        const typeOptions = FORM_TYPE_OPTIONS.includes(item.type)
          ? FORM_TYPE_OPTIONS
          : [item.type, ...FORM_TYPE_OPTIONS];
        return (
          <fieldset key={i} className="studio-item">
            <legend>
              Item {i + 1} — {item.linkId || "no linkId"}{" "}
              <RowControls
                index={i}
                count={model.items.length}
                label={`item ${i + 1}`}
                onMove={(idx, delta) =>
                  setModel({ ...model, items: moveItem(model.items, idx, delta) })
                }
                onRemove={(idx) =>
                  setModel({ ...model, items: model.items.filter((_, j) => j !== idx) })
                }
              />
            </legend>
            <label>
              linkId{" "}
              <input
                aria-label={`Item ${i + 1} linkId`}
                value={item.linkId}
                onChange={(e) => setItem(i, { linkId: e.target.value })}
              />
            </label>{" "}
            <label>
              Text{" "}
              <input
                aria-label={`Item ${i + 1} text`}
                value={item.text}
                onChange={(e) => setItem(i, { text: e.target.value })}
              />
            </label>{" "}
            <label>
              Type{" "}
              <select
                aria-label={`Item ${i + 1} type`}
                value={item.type}
                onChange={(e) => setItem(i, { type: e.target.value })}
              >
                {typeOptions.map((t) => (
                  <option key={t} value={t}>
                    {t}
                  </option>
                ))}
              </select>
            </label>{" "}
            <label>
              <input
                type="checkbox"
                aria-label={`Item ${i + 1} required`}
                checked={item.required}
                onChange={(e) => setItem(i, { required: e.target.checked })}
              />{" "}
              Required
            </label>

            {item.type === "choice" ? (
              <div className="studio-subrows">
                <p className="hint">Answer options (stored as answerOption valueCodings):</p>
                {item.options.map((opt, j) => (
                  <p key={j}>
                    <label>
                      Code{" "}
                      <input
                        aria-label={`Item ${i + 1} option ${j + 1} code`}
                        value={opt.code}
                        onChange={(e) =>
                          setItem(i, {
                            options: item.options.map((o, k) =>
                              k === j ? { ...o, code: e.target.value } : o,
                            ),
                          })
                        }
                      />
                    </label>{" "}
                    <label>
                      Display{" "}
                      <input
                        aria-label={`Item ${i + 1} option ${j + 1} display`}
                        value={opt.display}
                        onChange={(e) =>
                          setItem(i, {
                            options: item.options.map((o, k) =>
                              k === j ? { ...o, display: e.target.value } : o,
                            ),
                          })
                        }
                      />
                    </label>{" "}
                    <button
                      type="button"
                      aria-label={`Remove item ${i + 1} option ${j + 1}`}
                      onClick={() =>
                        setItem(i, { options: item.options.filter((_, k) => k !== j) })
                      }
                    >
                      ✕
                    </button>
                  </p>
                ))}
                <button
                  type="button"
                  onClick={() =>
                    setItem(i, { options: [...item.options, { code: "", display: "" }] })
                  }
                >
                  Add option
                </button>
              </div>
            ) : null}
          </fieldset>
        );
      })}

      <p>
        <button
          type="button"
          onClick={() =>
            setModel({
              ...model,
              items: [
                ...model.items,
                {
                  linkId: `q${model.items.length + 1}`,
                  text: "",
                  type: "string",
                  required: false,
                  options: [],
                  rest: {},
                },
              ],
            })
          }
        >
          Add item
        </button>
      </p>

      <FormPreview model={model} />
    </div>
  );
}

export function FormsTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<FormModel>
      client={client}
      kind="form"
      newKeyHint="e.g. intake"
      parse={parseForm}
      serialize={serializeForm}
      empty={emptyForm}
    >
      {(model, setModel) => <FormEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
