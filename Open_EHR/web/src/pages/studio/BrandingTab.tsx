import type { CSSProperties } from "react";
import type { ApiClient } from "../../api/client";
import { NAV_GROUP_ORDER } from "../../auth/session";
import { DEFAULT_GROUP_COLORS } from "../../auth/runtimeConfig";
import { ModuleIcon } from "../../components/ModuleIcon";
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

const HEX6 = /^#[0-9a-fA-F]{6}$/;
const GROUP_LABELS = NAV_GROUP_ORDER.filter(
  (g): g is string => typeof g === "string",
);

function BrandingEditor({
  model,
  setModel,
}: {
  model: BrandingModel;
  setModel: (m: BrandingModel) => void;
}) {
  const set = (patch: Partial<BrandingModel>) => setModel({ ...model, ...patch });
  const setGroup = (label: string, value: string) =>
    set({ groupColors: { ...model.groupColors, [label]: value } });
  // The colour a group renders in RIGHT NOW in this draft: the
  // editor's own value when set, else the built-in default palette.
  const effective = (label: string) =>
    model.groupColors[label] ?? DEFAULT_GROUP_COLORS[label];
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

      {/* Rich Design Elevation (DESIGN_SPEC §9): the group-colour
          editor. One colour input + hex text per canonical nav
          group, seeded from the current effective values; saving
          writes theme.groupColors through the same publish flow as
          every other branding field. */}
      <fieldset className="studio-item group-colors-editor">
        <legend>Group colours</legend>
        <p className="hint">
          Every module group has an identity colour — it tints the
          group&rsquo;s icon tiles and card accents on Home and in
          module headers. A group you don&rsquo;t set keeps its
          built-in colour, shown in the picker.
        </p>
        {GROUP_LABELS.map((label) => (
          <p key={label}>
            <label>
              <span
                className="group-color-dot"
                style={{ background: effective(label) }}
                aria-hidden="true"
              />{" "}
              {label}{" "}
              <input
                type="color"
                aria-label={`${label} colour picker`}
                value={HEX6.test(effective(label)) ? effective(label) : DEFAULT_GROUP_COLORS[label]}
                onChange={(e) => setGroup(label, e.target.value)}
              />{" "}
              <input
                aria-label={`${label} colour hex`}
                value={model.groupColors[label] ?? ""}
                placeholder={DEFAULT_GROUP_COLORS[label]}
                onChange={(e) => setGroup(label, e.target.value)}
                size={9}
              />
            </label>
          </p>
        ))}
        <div className="group-colors-preview" aria-label="Group colours preview">
          <div className="group-color-chips">
            {GROUP_LABELS.map((label) => (
              <span key={label} className="group-color-chip">
                <span
                  className="group-color-dot"
                  style={{ background: effective(label) }}
                  aria-hidden="true"
                />
                {label}
              </span>
            ))}
          </div>
          <div
            className="module-card group-preview-card"
            style={
              {
                "--group-accent": effective(GROUP_LABELS[0]),
                "--group-tint": `color-mix(in srgb, ${effective(GROUP_LABELS[0])} 10%, white)`,
              } as CSSProperties
            }
          >
            <span className="module-card-head">
              <span className="icon-tile" aria-hidden="true">
                <ModuleIcon name="users" size={24} />
              </span>
              <span className="module-card-label">Sample module card</span>
            </span>
            <span className="module-card-purpose">
              Preview — the {GROUP_LABELS[0]} colours update as you edit.
            </span>
            <span className="module-card-open">Open →</span>
          </div>
        </div>
      </fieldset>

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
