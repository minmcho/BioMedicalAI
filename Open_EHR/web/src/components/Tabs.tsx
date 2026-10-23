import type { ReactNode } from "react";

export interface TabDef {
  key: string;
  label: string;
}

interface TabsProps {
  tabs: TabDef[];
  activeKey: string;
  onChange: (key: string) => void;
  panels: Record<string, ReactNode>;
  ariaLabel?: string;
}

/** Shared tab bar (depth pass, Workstream 1). Renders a tab list of
 *  native buttons (keyboard-operable, aria-selected, role=tablist/tab)
 *  and ONLY the active panel — the established Pharmacy pattern of
 *  conditional render, so inactive panels' effects never run. */
export function Tabs({ tabs, activeKey, onChange, panels, ariaLabel }: TabsProps) {
  return (
    <>
      <div className="tabs-bar" role="tablist" aria-label={ariaLabel}>
        {tabs.map((t) => (
          <button
            key={t.key}
            type="button"
            role="tab"
            aria-selected={activeKey === t.key}
            className={activeKey === t.key ? "tab-button active" : "tab-button"}
            onClick={() => onChange(t.key)}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div role="tabpanel">{panels[activeKey]}</div>
    </>
  );
}
