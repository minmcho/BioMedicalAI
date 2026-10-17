import { useState } from "react";
import type { ApiClient } from "../../api/client";
import type { FormDefinition } from "../../api/types";
import { FormRenderer } from "../../components/FormRenderer";
import { Empty, ErrorView, Loading } from "../../components/StatusViews";
import { useAsync } from "../../hooks";

interface Props {
  client: ApiClient;
  patientId: string;
  /** notes.write gates flowsheet entry, like the server. */
  canWrite: boolean;
}

const FALLBACK_FORMS = ["flowsheet-vitals", "flowsheet-assessment"];

function cellText(value: unknown, unit?: string | null): string {
  if (value === null || value === undefined) return "";
  const base = typeof value === "object" ? JSON.stringify(value) : String(value);
  return unit ? `${base} ${unit}` : base;
}

/** Flowsheet tab: a time-series grid (items × entries) over the
 *  QuestionnaireResponse collections recorded against a published
 *  flowsheet form, plus entry through the shared FormRenderer. The
 *  form list comes from the published form configs (keys
 *  flowsheet-*), falling back to the two seeded forms. */
export function FlowsheetTab({ client, patientId, canWrite }: Props) {
  const [formKey, setFormKey] = useState(FALLBACK_FORMS[0]);

  const forms = useAsync<string[]>(async () => {
    try {
      const all = await client.effectiveConfigs("form");
      const keys = all.map((c) => c.key).filter((k) => k.startsWith("flowsheet-"));
      return keys.length > 0 ? keys : FALLBACK_FORMS;
    } catch {
      return FALLBACK_FORMS;
    }
  }, [client]);

  const view = useAsync(
    () => client.flowsheetView(formKey, patientId),
    [client, formKey, patientId],
  );

  const formDef = useAsync<FormDefinition | null>(async () => {
    try {
      const cfg = await client.effectiveConfig("form", formKey);
      return cfg.payload as unknown as FormDefinition;
    } catch {
      return null;
    }
  }, [client, formKey]);

  return (
    <section aria-label="Flowsheet">
      <div className="form-field">
        <label htmlFor="flowsheet-form">Flowsheet</label>
        <select
          id="flowsheet-form"
          value={formKey}
          onChange={(e) => setFormKey(e.target.value)}
        >
          {(forms.data ?? FALLBACK_FORMS).map((key) => (
            <option key={key} value={key}>
              {key}
            </option>
          ))}
        </select>
      </div>

      {view.loading && <Loading label="Loading flowsheet…" />}
      {view.error != null && <ErrorView error={view.error} />}
      {view.data && view.data.entries.length === 0 && (
        <Empty label="No entries recorded for this flowsheet yet." />
      )}
      {view.data && view.data.entries.length > 0 && (
        <table className="lab-table flowsheet-grid">
          <thead>
            <tr>
              <th scope="col">Item</th>
              {view.data.entries.map((entry) => (
                <th key={entry.id} scope="col">
                  {entry.authored ? entry.authored.replace("T", " ").slice(0, 16) : entry.id}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.data.form.items.map((item) => (
              <tr key={item.linkId}>
                <th scope="row">{item.text ?? item.linkId}</th>
                {view.data!.entries.map((entry) => {
                  const cell = entry.answers[item.linkId];
                  return (
                    <td key={entry.id}>
                      {cell ? cellText(cell.value, cell.unit) : ""}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {canWrite && formDef.data && (
        <div className="flowsheet-entry">
          <h4>Record entry</h4>
          <FormRenderer
            key={formKey}
            form={formDef.data}
            submitLabel="Add entry"
            onSubmit={async (answers) => {
              await client.submitFlowsheet({
                patient: patientId,
                form: formKey,
                answers,
              });
              view.reload();
            }}
          />
        </div>
      )}
    </section>
  );
}
