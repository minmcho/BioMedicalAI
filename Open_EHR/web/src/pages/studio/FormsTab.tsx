import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import { RowControls, moveItem } from "./fields";
import {
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
//
// Palette alignment (Studio parity row 19): the field types offered are
// EXACTLY backend/app/configstore.py's FORM_ITEM_TYPES —
//   FORM_ITEM_TYPES = ("group", "display", "boolean", "decimal",
//     "integer", "string", "text", "date", "dateTime", "time", "choice",
//     "open-choice", "quantity")
// — and every type is renderable by web/src/components/FormRenderer.tsx
// (group -> fieldset, display -> static text, text -> textarea,
// dateTime/time -> typed inputs, quantity -> numeric, open-choice ->
// select over answerOption). models.FORM_TYPE_OPTIONS is the older
// six-type subset, so the full mirror lives here. Per-field options
// offered are exactly the ones validate_form checks: `required`
// (boolean) and `valueSet` (a string reference, configstore
// validate_form). No other per-field option is offered.

export const ALL_FORM_TYPES = [
  "group",
  "display",
  "boolean",
  "decimal",
  "integer",
  "string",
  "text",
  "date",
  "dateTime",
  "time",
  "choice",
  "open-choice",
  "quantity",
] as const;

const CHOICE_TYPES = ["choice", "open-choice"];

/** serializeForm (models.ts) writes answerOption only for `choice`;
 *  FormRenderer renders `open-choice` from answerOption too and
 *  validate_form accepts it, so the wrapper restores answerOption for
 *  open-choice items from the same options model — nothing else about
 *  the serialization changes. */
function serializeFormComplete(model: FormModel): Record<string, unknown> {
  const payload = serializeForm(model);
  const items = payload.item as Array<Record<string, unknown>>;
  items.forEach((out, i) => {
    const src = model.items[i];
    if (src && src.type === "open-choice" && src.options.length > 0) {
      out.answerOption = src.options.map((o) => ({
        valueCoding: { code: o.code, display: o.display || o.code },
      }));
    }
  });
  return payload;
}

function FormPreview({ model }: { model: FormModel }) {
  return (
    <div className="studio-preview" aria-label="Form preview">
      <h4>Preview{model.title ? ` — ${model.title}` : ""}</h4>
      {model.items.length === 0 ? (
        <p className="hint">No items yet.</p>
      ) : (
        model.items.map((item, i) => {
          if (item.type === "display") {
            return (
              <p key={i} className="studio-preview-field">
                {item.text || item.linkId || `Item ${i + 1}`}
              </p>
            );
          }
          if (item.type === "group") {
            return (
              <fieldset key={i} className="studio-preview-field">
                <legend>{item.text || item.linkId || `Item ${i + 1}`}</legend>
                <span className="hint">Section heading (group)</span>
              </fieldset>
            );
          }
          return (
            <p key={i} className="studio-preview-field">
              <label>
                {item.text || item.linkId || `Item ${i + 1}`}
                {item.required ? <span aria-hidden="true"> *</span> : null}{" "}
                {item.type === "boolean" ? (
                  <input type="checkbox" disabled />
                ) : CHOICE_TYPES.includes(item.type) ? (
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
                ) : item.type === "dateTime" ? (
                  <input type="datetime-local" disabled />
                ) : item.type === "time" ? (
                  <input type="time" disabled />
                ) : item.type === "text" ? (
                  <textarea disabled />
                ) : item.type === "decimal" ||
                  item.type === "integer" ||
                  item.type === "quantity" ? (
                  <input type="number" disabled />
                ) : (
                  <input type="text" disabled />
                )}
              </label>
            </p>
          );
        })
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

  /** `valueSet` is a validate_form-checked string field the flat model
   *  carries in the item's preserved `rest` bag; editing it here writes
   *  (or, when cleared, removes) exactly that field. */
  const setValueSet = (index: number, item: FormItemModel, value: string) => {
    const rest = { ...item.rest };
    if (value) rest.valueSet = value;
    else delete rest.valueSet;
    setItem(index, { rest });
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
      <p className="hint">
        Field types match the backend validator exactly. A <code>group</code> item is a
        section heading and a <code>display</code> item is static text — neither collects
        an answer. Nested items inside a group are preserved from the loaded payload but
        are edited in the Advanced (JSON) tab.
      </p>

      {model.items.map((item, i) => {
        const typeOptions = (ALL_FORM_TYPES as readonly string[]).includes(item.type)
          ? [...ALL_FORM_TYPES]
          : [item.type, ...ALL_FORM_TYPES];
        const valueSet =
          typeof item.rest.valueSet === "string" ? item.rest.valueSet : "";
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
            </label>{" "}
            {CHOICE_TYPES.includes(item.type) ? (
              <label>
                Value set{" "}
                <input
                  aria-label={`Item ${i + 1} valueSet`}
                  value={valueSet}
                  placeholder="e.g. ValueSet/yes-no"
                  onChange={(e) => setValueSet(i, item, e.target.value)}
                />
              </label>
            ) : null}

            {CHOICE_TYPES.includes(item.type) ? (
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
      serialize={serializeFormComplete}
      empty={emptyForm}
    >
      {(model, setModel) => <FormEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
