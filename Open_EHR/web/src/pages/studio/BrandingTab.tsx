import type { ApiClient } from "../../api/client";
import { ConfigEditorShell } from "./ConfigEditorShell";
import {
  emptyBranding,
  parseBranding,
  serializeBranding,
  type BrandingModel,
} from "./models";

// Branding tab — the organisation profile: name, portal title, tagline,
// primary colour, logo URL and support contact. The preview chip renders
// the org header the way the app shell does (brand block in the sidebar:
// org name as the brand line, portal wording under it, colour as the
// accent), so what is previewed is what ships on publish.

function BrandingEditor({
  model,
  setModel,
}: {
  model: BrandingModel;
  setModel: (m: BrandingModel) => void;
}) {
  const set = (patch: Partial<BrandingModel>) => setModel({ ...model, ...patch });
  return (
    <div className="studio-editor">
      <p>
        <label>
          Organisation name{" "}
          <input
            aria-label="Organisation name"
            value={model.orgName}
            onChange={(e) => set({ orgName: e.target.value })}
          />
        </label>
      </p>
      <p>
        <label>
          Portal title{" "}
          <input
            aria-label="Portal title"
            value={model.portalTitle}
            onChange={(e) => set({ portalTitle: e.target.value })}
          />
        </label>
      </p>
      <p>
        <label>
          Tagline{" "}
          <input
            aria-label="Tagline"
            value={model.tagline}
            onChange={(e) => set({ tagline: e.target.value })}
          />
        </label>
      </p>
      <p>
        <label>
          Primary colour{" "}
          <input
            type="color"
            aria-label="Primary colour picker"
            value={/^#[0-9a-fA-F]{6}$/.test(model.primaryColor) ? model.primaryColor : "#1a7f37"}
            onChange={(e) => set({ primaryColor: e.target.value })}
          />{" "}
          <input
            aria-label="Primary colour hex"
            value={model.primaryColor}
            onChange={(e) => set({ primaryColor: e.target.value })}
            placeholder="#1a7f37"
            size={9}
          />
        </label>
      </p>
      <p>
        <label>
          Logo URL{" "}
          <input
            aria-label="Logo URL"
            value={model.logoUrl}
            onChange={(e) => set({ logoUrl: e.target.value })}
          />
        </label>
      </p>
      <p>
        <label>
          Support contact{" "}
          <input
            aria-label="Support contact"
            value={model.supportContact}
            onChange={(e) => set({ supportContact: e.target.value })}
            placeholder="e.g. support@example.org"
          />
        </label>
      </p>

      <div
        className="studio-brand-preview"
        style={{ borderTop: `4px solid ${model.primaryColor || "#1a7f37"}` }}
        aria-label="Organisation header preview"
      >
        {model.logoUrl ? (
          <img src={model.logoUrl} alt="" height={28} />
        ) : null}
        <p className="brand" style={{ color: model.primaryColor || undefined }}>
          {model.orgName || "Organisation name"}
        </p>
        <p className="tagline">{model.portalTitle || "Portal title"}</p>
        {model.tagline ? <p className="hint">{model.tagline}</p> : null}
        {model.supportContact ? (
          <p className="pack-detail">Support: {model.supportContact}</p>
        ) : null}
      </div>
      <p className="hint">Preview of the org header as the app renders it.</p>
    </div>
  );
}

export function BrandingTab({ client }: { client: ApiClient }) {
  return (
    <ConfigEditorShell<BrandingModel>
      client={client}
      kind="branding"
      newKeyHint="e.g. default"
      parse={parseBranding}
      serialize={serializeBranding}
      empty={emptyBranding}
    >
      {(model, setModel) => <BrandingEditor model={model} setModel={setModel} />}
    </ConfigEditorShell>
  );
}
