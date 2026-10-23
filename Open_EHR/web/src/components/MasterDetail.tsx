import { useState, type ReactNode } from "react";
import { SkeletonRows } from "./Skeleton";

export interface MasterDetailProps<T> {
  items: T[];
  getKey: (item: T) => string;
  renderListItem: (item: T) => ReactNode;
  renderDetail: (item: T) => ReactNode;
  /** Optional per-row action rendered as a SIBLING of the row's
   *  select button (never nested inside it), so a row can carry a
   *  one-click jump (e.g. "Open chart") that satisfies the spec's
   *  chart-from-any-list = 1 click budget without selecting first. */
  renderRowAction?: (item: T) => ReactNode;
  selectedKey?: string | null;
  onSelect?: (key: string) => void;
  emptyText?: string;
  detailEmptyText?: string;
  listLabel: string;
  /** Rich Design Elevation (§8.14): while the list data loads, the
   *  list pane mirrors its layout with skeleton rows instead of the
   *  empty text. Optional — pages that don't pass it render exactly
   *  as before. */
  loading?: boolean;
}

/** Generic master-detail (Phase 15, workstream F — shared components).
 *
 *  Left pane: a list of native buttons, one per item; the selected
 *  row carries aria-pressed="true" so selection is conveyed in text /
 *  assistive state, never colour alone. Right pane: the detail of the
 *  selected item.
 *
 *  Controlled when `selectedKey` is provided (including null = no
 *  selection); uncontrolled when it is undefined — the component then
 *  manages its own selection, defaulting to the first item.
 *
 *  Styling reuses the existing calm idiom (.patient-list /
 *  .patient-row for the list, .card / .state for the panes). The
 *  two-column layout itself needs one small .master-detail rule —
 *  reported as INTEGRATION-NEEDED (styles.css is coordinator-owned).
 */
export function MasterDetail<T>({
  items,
  getKey,
  renderListItem,
  renderDetail,
  renderRowAction,
  selectedKey,
  onSelect,
  emptyText = "No items.",
  detailEmptyText = "Select an item to see details.",
  listLabel,
  loading = false,
}: MasterDetailProps<T>) {
  const [internalKey, setInternalKey] = useState<string | null>(null);
  const isControlled = selectedKey !== undefined;

  let effectiveKey: string | null;
  if (isControlled) {
    effectiveKey = selectedKey ?? null;
  } else if (
    internalKey !== null &&
    items.some((item) => getKey(item) === internalKey)
  ) {
    effectiveKey = internalKey;
  } else {
    effectiveKey = items.length > 0 ? getKey(items[0]) : null;
  }

  const selectedItem =
    effectiveKey === null
      ? undefined
      : items.find((item) => getKey(item) === effectiveKey);

  function handleSelect(key: string) {
    if (!isControlled) setInternalKey(key);
    onSelect?.(key);
  }

  return (
    <div className="master-detail">
      <div className="master-detail-list card">
        {loading ? (
          <SkeletonRows count={5} />
        ) : items.length === 0 ? (
          <p className="state state-empty" role="status">
            {emptyText}
          </p>
        ) : (
          <ul className="patient-list" aria-label={listLabel}>
            {items.map((item) => {
              const key = getKey(item);
              const isSelected = key === effectiveKey;
              const rowAction = renderRowAction?.(item);
              return (
                <li key={key} className={rowAction ? "master-row-line" : undefined}>
                  <button
                    type="button"
                    className="patient-row"
                    aria-pressed={isSelected}
                    aria-current={isSelected ? "true" : undefined}
                    onClick={() => handleSelect(key)}
                  >
                    {renderListItem(item)}
                  </button>
                  {rowAction}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <div className="master-detail-detail card">
        {selectedItem ? (
          renderDetail(selectedItem)
        ) : (
          <p className="state state-empty" role="status">
            {items.length === 0 ? detailEmptyText : detailEmptyText}
          </p>
        )}
      </div>
    </div>
  );
}
