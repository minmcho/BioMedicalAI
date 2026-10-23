import type { ReactNode } from "react";

export type StatusTone = "ok" | "warn" | "bad" | "info" | "neutral";

interface StatusBadgeProps {
  tone: StatusTone;
  children: ReactNode;
}

/** Status pill (depth pass, Workstream 1). Wraps the existing badge
 *  idiom; the label text is the caller's children, so status is always
 *  conveyed as text + colour by construction, never colour alone. */
export function StatusBadge({ tone, children }: StatusBadgeProps) {
  const extra =
    tone === "ok" ? ""
    : tone === "warn" ? " badge-warn"
    : tone === "bad" ? " badge-critical"
    : tone === "info" ? " status-badge-info"
    : " status-badge-neutral";
  return <span className={`badge status-badge${extra}`}>{children}</span>;
}
