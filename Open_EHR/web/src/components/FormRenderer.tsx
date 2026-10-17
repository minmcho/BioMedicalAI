import { useEffect, useState } from "react";
import type {
  FormDefinition,
  FormItem,
  QuestionnaireAnswerItem,
} from "../api/types";

// Generic renderer for published Studio forms (Questionnaire payloads,
// config kind=form). Phase 5 closes the audit gap that no page rendered
// a published form: the note templates and the flowsheets both render
// through this one component, and the answers it builds are the exact
// QuestionnaireResponse items the server validates (required items,
// per-type answer shapes) — validation parity lives server-side; this
// component is responsible for faithful typing.

export type FormValues = Record<string, string | boolean>;

function walkItems(items: FormItem[] | undefined, fn: (item: FormItem) => void) {
  for (const item of items ?? []) {
    fn(item);
    if (item.item) walkItems(item.item, fn);
  }
}

/** Initial (empty) values for every answerable item in a form. */
export function initialFormValues(form: FormDefinition): FormValues {
  const values: FormValues = {};
  walkItems(form.item, (item) => {
    if (item.type === "group" || item.type === "display") return;
    values[item.linkId] = item.type === "boolean" ? false : "";
  });
  return values;
}

function optionValue(option: NonNullable<FormItem["answerOption"]>[number]): string {
  return option.valueCoding?.code ?? option.valueString ?? "";
}

function typedAnswer(item: FormItem, raw: string | boolean): Record<string, unknown> | null {
  switch (item.type) {
    case "boolean":
      return typeof raw === "boolean" ? { valueBoolean: raw } : null;
    case "integer": {
      if (raw === "") return null;
      const n = Number.parseInt(String(raw), 10);
      return Number.isNaN(n) ? null : { valueInteger: n };
    }
    case "decimal": {
      if (raw === "") return null;
      const n = Number.parseFloat(String(raw));
      return Number.isNaN(n) ? null : { valueDecimal: n };
    }
    case "quantity": {
      if (raw === "") return null;
      const n = Number.parseFloat(String(raw));
      return Number.isNaN(n) ? null : { valueQuantity: { value: n } };
    }
    case "date":
      return raw ? { valueDate: String(raw) } : null;
    case "dateTime":
      return raw ? { valueDateTime: String(raw) } : null;
    case "time":
      return raw ? { valueTime: String(raw) } : null;
    case "choice":
    case "open-choice": {
      if (!raw) return null;
      const option = (item.answerOption ?? []).find((o) => optionValue(o) === raw);
      if (option?.valueCoding) {
        return {
          valueCoding: {
            ...(option.valueCoding.system ? { system: option.valueCoding.system } : {}),
            code: option.valueCoding.code,
            ...(option.valueCoding.display ? { display: option.valueCoding.display } : {}),
          },
        };
      }
      return { valueString: String(raw) };
    }
    default: // string, text, and anything unrecognised rides as a string
      return typeof raw === "string" && raw !== "" ? { valueString: raw } : null;
  }
}

function buildItems(items: FormItem[] | undefined, values: FormValues): QuestionnaireAnswerItem[] {
  const out: QuestionnaireAnswerItem[] = [];
  for (const item of items ?? []) {
    if (item.type === "display") continue;
    if (item.type === "group") {
      const children = buildItems(item.item, values);
      if (children.length > 0) out.push({ linkId: item.linkId, item: children });
      continue;
    }
    const answer = typedAnswer(item, values[item.linkId] ?? "");
    if (answer) out.push({ linkId: item.linkId, answer: [answer] });
  }
  return out;
}

/** Build the QuestionnaireResponse items for a form + raw field values.
 *  Empty optional answers are omitted; the server enforces `required`. */
export function buildAnswers(form: FormDefinition, values: FormValues): QuestionnaireAnswerItem[] {
  return buildItems(form.item, values);
}

interface Props {
  form: FormDefinition;
  submitLabel?: string;
  onSubmit: (answers: QuestionnaireAnswerItem[]) => void | Promise<void>;
}

export function FormRenderer({ form, submitLabel = "Submit", onSubmit }: Props) {
  const [values, setValues] = useState<FormValues>(() => initialFormValues(form));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // A different form (note type / flowsheet switch) starts blank.
  useEffect(() => {
    setValues(initialFormValues(form));
    setError(null);
  }, [form]);

  const set = (linkId: string, value: string | boolean) =>
    setValues((v) => ({ ...v, [linkId]: value }));

  const renderItem = (item: FormItem) => {
    if (item.type === "display") {
      return <p key={item.linkId} className="form-display">{item.text}</p>;
    }
    if (item.type === "group") {
      return (
        <fieldset key={item.linkId} className="form-group">
          <legend>{item.text ?? item.linkId}</legend>
          {(item.item ?? []).map(renderItem)}
        </fieldset>
      );
    }
    const id = `fr-${item.linkId}`;
    const label = (
      <label htmlFor={id}>
        {item.text ?? item.linkId}
        {item.required ? " *" : ""}
      </label>
    );
    const raw = values[item.linkId] ?? "";
    switch (item.type) {
      case "boolean":
        return (
          <div key={item.linkId} className="form-field">
            <label htmlFor={id}>
              <input
                id={id}
                type="checkbox"
                checked={raw === true}
                onChange={(e) => set(item.linkId, e.target.checked)}
              />{" "}
              {item.text ?? item.linkId}
              {item.required ? " *" : ""}
            </label>
          </div>
        );
      case "choice":
      case "open-choice":
        return (
          <div key={item.linkId} className="form-field">
            {label}
            <select
              id={id}
              value={String(raw)}
              required={item.required}
              onChange={(e) => set(item.linkId, e.target.value)}
            >
              <option value="">Select…</option>
              {(item.answerOption ?? []).map((option) => (
                <option key={optionValue(option)} value={optionValue(option)}>
                  {option.valueCoding?.display ?? option.valueString ?? optionValue(option)}
                </option>
              ))}
            </select>
          </div>
        );
      case "text":
        return (
          <div key={item.linkId} className="form-field">
            {label}
            <textarea
              id={id}
              value={String(raw)}
              required={item.required}
              onChange={(e) => set(item.linkId, e.target.value)}
            />
          </div>
        );
      case "integer":
      case "decimal":
      case "quantity":
        return (
          <div key={item.linkId} className="form-field">
            {label}
            <input
              id={id}
              type="number"
              step={item.type === "integer" ? "1" : "any"}
              value={String(raw)}
              required={item.required}
              onChange={(e) => set(item.linkId, e.target.value)}
            />
          </div>
        );
      case "date":
      case "time":
      case "dateTime":
        return (
          <div key={item.linkId} className="form-field">
            {label}
            <input
              id={id}
              type={item.type === "date" ? "date" : item.type === "time" ? "time" : "datetime-local"}
              value={String(raw)}
              required={item.required}
              onChange={(e) => set(item.linkId, e.target.value)}
            />
          </div>
        );
      default:
        return (
          <div key={item.linkId} className="form-field">
            {label}
            <input
              id={id}
              type="text"
              value={String(raw)}
              required={item.required}
              onChange={(e) => set(item.linkId, e.target.value)}
            />
          </div>
        );
    }
  };

  return (
    <form
      className="rendered-form"
      onSubmit={async (e) => {
        e.preventDefault();
        setBusy(true);
        setError(null);
        try {
          await onSubmit(buildAnswers(form, values));
          setValues(initialFormValues(form));
        } catch (err) {
          setError(err instanceof Error ? err.message : "Submission failed");
        } finally {
          setBusy(false);
        }
      }}
    >
      {(form.item ?? []).map(renderItem)}
      {error && (
        <p role="alert" className="form-error">
          {error}
        </p>
      )}
      <button type="submit" disabled={busy}>
        {submitLabel}
      </button>
    </form>
  );
}
