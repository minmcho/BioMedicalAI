import type { ReactNode } from "react";
import { ModuleIcon } from "./ModuleIcon";

export interface ModuleHeaderPrimaryAction {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  /** Shown next to the button whenever the action is disabled, so a
   *  disabled primary action always carries its reason (spec P-6). */
  disabledReason?: string;
}

export interface ModuleHeaderProps {
  title: string;
  purpose?: string;
  primaryAction?: ModuleHeaderPrimaryAction;
  /** Optional slot for live indicators rendered in the header zone. */
  children?: ReactNode;
  /** Rich Design Elevation (§8.6): the module's nav icon name and
   *  group label (from the same nav resolution the Home cards use).
   *  When `icon` is passed the header leads with the group-tinted
   *  icon tile; `group` sets data-group so the header zone carries
   *  the group identity. Both default gracefully — pages that pass
   *  neither render exactly as before. */
  icon?: string;
  group?: string;
}

/** Shared module page header (Home & Module UX pass, spec P-1/P-2).
 *  Renders the page's ONE <h2>, a one-line purpose in the hint style,
 *  and — when the page has one — its single visually primary action
 *  in the existing accent-button idiom (.row-actions button). Pages
 *  adopting ModuleHeader must not render a second <h2>. */
export function ModuleHeader({
  title,
  purpose,
  primaryAction,
  children,
  icon,
  group,
}: ModuleHeaderProps) {
  return (
    <header
      className="page-header module-header"
      data-group={group ?? undefined}
    >
      <div className="module-header-main">
        {icon && (
          <span className="icon-tile" aria-hidden="true">
            <ModuleIcon name={icon} size={24} />
          </span>
        )}
        <div>
          <h2>{title}</h2>
          {purpose && <p className="hint">{purpose}</p>}
        </div>
      </div>
      {(children || primaryAction) && (
        <div
          className="module-header-side"
          style={{
            display: "flex",
            flexDirection: "column",
            alignItems: "flex-end",
            gap: "0.25rem",
          }}
        >
          {children}
          {primaryAction && (
            <>
              <div className="row-actions" style={{ marginTop: 0 }}>
                <button
                  type="button"
                  disabled={primaryAction.disabled === true}
                  onClick={primaryAction.onClick}
                >
                  {primaryAction.label}
                </button>
              </div>
              {primaryAction.disabled === true &&
                primaryAction.disabledReason && (
                  <span className="hint">{primaryAction.disabledReason}</span>
                )}
            </>
          )}
        </div>
      )}
    </header>
  );
}

/** Scroll a page section into view and move focus to its first
 *  control — the wired target for header primary actions that point
 *  at a form lower on the same page. Safe in jsdom (scrollIntoView
 *  may be absent there). */
export function scrollToSection(id: string): void {
  const el = document.getElementById(id);
  if (!el) return;
  if (typeof el.scrollIntoView === "function") {
    el.scrollIntoView({ block: "start" });
  }
  const first = el.querySelector<HTMLElement>(
    "input, select, textarea, button",
  );
  first?.focus();
}
