import { useEffect, useState } from "react";
import { rolesFromText, rolesToText } from "./models";

// Small shared editor controls for the Studio tabs.

/** Comma-separated roles editor. Text is held locally while typing (so a
 *  trailing comma survives) and committed to the model on blur. */
export function RolesInput({
  roles,
  onCommit,
  label,
}: {
  roles: string[];
  onCommit: (roles: string[]) => void;
  label: string;
}) {
  const [text, setText] = useState(() => rolesToText(roles));
  // Re-sync when the model changes underneath us (e.g. loading a version).
  const rolesKey = roles.join(",");
  useEffect(() => {
    setText(rolesToText(roles));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rolesKey]);
  return (
    <input
      aria-label={label}
      value={text}
      placeholder="roles, comma-separated"
      onChange={(e) => setText(e.target.value)}
      onBlur={() => onCommit(rolesFromText(text))}
    />
  );
}

/** Up / down / remove row controls used by every list editor. */
export function RowControls({
  index,
  count,
  onMove,
  onRemove,
  label,
}: {
  index: number;
  count: number;
  onMove: (index: number, delta: number) => void;
  onRemove: (index: number) => void;
  label: string;
}) {
  return (
    <span className="row-controls">
      <button
        type="button"
        aria-label={`Move ${label} up`}
        disabled={index === 0}
        onClick={() => onMove(index, -1)}
      >
        ↑
      </button>{" "}
      <button
        type="button"
        aria-label={`Move ${label} down`}
        disabled={index === count - 1}
        onClick={() => onMove(index, 1)}
      >
        ↓
      </button>{" "}
      <button type="button" aria-label={`Remove ${label}`} onClick={() => onRemove(index)}>
        ✕
      </button>
    </span>
  );
}

/** Move helper shared by the list editors. */
export function moveItem<T>(items: T[], index: number, delta: number): T[] {
  const target = index + delta;
  if (target < 0 || target >= items.length) return items;
  const next = [...items];
  [next[index], next[target]] = [next[target], next[index]];
  return next;
}
