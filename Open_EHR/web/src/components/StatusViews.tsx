import { ApiError } from "../api/client";
import { ModuleIcon } from "./ModuleIcon";

// Shared loading / empty / error states. Errors are shown honestly: a 403
// reads as permission denied with the server's detail, never as an
// empty list.

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="state state-loading" role="status">
      {label}
    </p>
  );
}

/** Empty state (Rich Design Elevation §8.13): a designed panel —
 *  dashed-border sunken block with an optional icon (same glyph map
 *  as the module icons) and an optional primary action. The label
 *  text is the caller's, unchanged; callers that pass neither new
 *  prop get the same words in the finished panel. */
export function Empty({
  label,
  icon,
  action,
}: {
  label: string;
  icon?: string;
  action?: { label: string; onClick: () => void };
}) {
  return (
    <div className="state state-empty empty-panel" role="status">
      {icon && (
        <span className="empty-icon" aria-hidden="true">
          <ModuleIcon name={icon} size={28} />
        </span>
      )}
      <p className="empty-label">{label}</p>
      {action && (
        <button type="button" className="btn-primary" onClick={action.onClick}>
          {action.label}
        </button>
      )}
    </div>
  );
}

export function ErrorView({ error }: { error: unknown }) {
  if (error instanceof ApiError) {
    if (error.isPermissionDenied) {
      return (
        <div className="state state-error" role="alert">
          <strong>Permission denied.</strong> {error.detail}
        </div>
      );
    }
    if (error.status === 401) {
      return (
        <div className="state state-error" role="alert">
          <strong>Not signed in.</strong> {error.detail}
        </div>
      );
    }
    if (error.status === 0) {
      return (
        <div className="state state-error" role="alert">
          <strong>Backend unreachable.</strong> {error.detail}
        </div>
      );
    }
    return (
      <div className="state state-error" role="alert">
        <strong>Request failed ({error.status}).</strong> {error.detail}
      </div>
    );
  }
  return (
    <div className="state state-error" role="alert">
      <strong>Something went wrong.</strong> {error instanceof Error ? error.message : String(error)}
    </div>
  );
}
