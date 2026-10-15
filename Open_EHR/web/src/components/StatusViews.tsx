import { ApiError } from "../api/client";

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

export function Empty({ label }: { label: string }) {
  return (
    <p className="state state-empty" role="status">
      {label}
    </p>
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
