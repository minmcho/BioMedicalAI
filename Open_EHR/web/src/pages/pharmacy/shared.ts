import { ApiError } from "../../api/client";

/** Shared error formatting for the pharmacy work-centre tabs (same
 *  shape as PharmacyPage's local helper). */
export function errorText(err: unknown): string {
  if (err instanceof ApiError)
    return `${err.status ? `(${err.status}) ` : ""}${err.detail}`;
  return err instanceof Error ? err.message : String(err);
}
