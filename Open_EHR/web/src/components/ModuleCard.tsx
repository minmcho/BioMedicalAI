import type { HomeCard } from "../api/types";
import type { NavItem, PageKey } from "../auth/session";
import { StatusBadge, type StatusTone } from "./StatusBadge";
import { ModuleIcon } from "./ModuleIcon";

// Home & Module UX pass: one card per module on the Home card menu.
// The WHOLE card is a single button — one click lands in the module
// (spec: 1-click entry). The live indicator block renders only when
// GET /home/cards returned a provider entry for the module; modules
// without a provider show purpose + "Open" only (no invented
// numbers). Status is always text + tone via StatusBadge.

// Rich Design Elevation pass: the glyph map + ModuleIcon now live
// in components/ModuleIcon.tsx (extracted so ModuleHeader and other
// surfaces share the icon system); re-exported here for existing
// importers.
export { ModuleIcon } from "./ModuleIcon";

const VALID_TONES: StatusTone[] = ["ok", "warn", "bad", "info", "neutral"];

function toTone(tone: string | undefined): StatusTone {
  return tone && (VALID_TONES as string[]).includes(tone)
    ? (tone as StatusTone)
    : "neutral";
}

export function ModuleCard({
  item,
  indicator,
  onOpen,
}: {
  item: NavItem;
  indicator?: HomeCard;
  onOpen: (key: PageKey) => void;
}) {
  return (
    <button
      type="button"
      className="module-card"
      data-module={item.key}
      data-group={item.group ?? undefined}
      aria-label={`Open ${item.label}`}
      onClick={() => onOpen(item.key)}
    >
      <span className="module-card-head">
        {/* Rich Design Elevation: the glyph sits in a group-tinted
            icon tile (spec §6/§8.1) — the card's group identity. */}
        <span className="icon-tile" aria-hidden="true">
          <ModuleIcon name={item.icon} size={24} />
        </span>
        <span className="module-card-label">{item.label}</span>
      </span>
      {item.purpose && <span className="module-card-purpose">{item.purpose}</span>}
      {indicator ? (
        <span className="module-card-indicator">
          <span className="module-card-value">{indicator.value}</span>
          <span className="module-card-indicator-label">{indicator.label}</span>
          <StatusBadge tone={toTone(indicator.tone)}>
            {indicator.status_text}
          </StatusBadge>
          {indicator.timer_text && (
            <span className="module-card-timer">{indicator.timer_text}</span>
          )}
        </span>
      ) : (
        <span className="module-card-open">Open →</span>
      )}
    </button>
  );
}
