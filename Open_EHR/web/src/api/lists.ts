// Governed dropdown/list management client (Phase 15, workstream F).
// The single source of truth is backend/app/listmgmt.py: routes under
// /lists, registry = published Studio config terminology/
// manageable-lists. Consumers read PUBLISHED only; edits land on the
// governed DRAFT and take effect only when a human publishes in
// System Studio. Types for these payloads live here, not in
// api/types.ts (Phase 15 web brief).

import type { ApiClient } from "./client";

/** One concept in a terminology-backed selection list. Extra fields
 *  ride on the concept (the configstore pattern), so the shape stays
 *  open beyond code/display/active. */
export interface ListConcept {
  code: string;
  display?: string;
  active?: boolean;
  [key: string]: unknown;
}

export interface ManageableListEntry {
  kind: string;
  key: string;
  label: string | null;
  module: string | null;
  managerJobRoles: string[];
  active: boolean;
  caller_can_manage: boolean;
  manage_basis: string | null;
}

export interface ListRegistryResponse {
  registry: { kind: string; key: string; version: number };
  items: ManageableListEntry[];
  total: number;
  excluded_lists: Array<Record<string, unknown>>;
  notes?: string | null;
}

export interface ManagedList {
  kind: string;
  key: string;
  label: string | null;
  module: string | null;
  managerJobRoles: string[];
  active: boolean;
  caller_can_manage: boolean;
  manage_basis: string | null;
  published: { version: number; concepts: ListConcept[] };
  draft: {
    version: number;
    concepts: ListConcept[];
    created_by: string | null;
    updated_by: string | null;
  } | null;
  notice: string;
}

export interface PutListDraftResult {
  kind: string;
  key: string;
  label: string | null;
  module: string | null;
  status: string;
  draft_version: number;
  published_version: number;
  manage_basis: string | null;
  diff: { added: string[]; updated: string[]; deactivated: string[] };
  diff_basis: string;
  concepts: ListConcept[];
  notice: string;
}

/** GET /lists — the published manageable-lists registry; each item
 *  carries caller_can_manage + manage_basis for the caller. */
export function listRegistry(client: ApiClient): Promise<ListRegistryResponse> {
  return client.request<ListRegistryResponse>("/lists");
}

/** GET /lists/{kind}/{key} — published concepts + draft concepts
 *  clearly separated, plus caller_can_manage + the draft-only
 *  notice. */
export function getManagedList(
  client: ApiClient,
  kind: string,
  key: string,
): Promise<ManagedList> {
  return client.request<ManagedList>(
    `/lists/${encodeURIComponent(kind)}/${encodeURIComponent(key)}`,
  );
}

/** PUT /lists/{kind}/{key}/draft — the FULL edited concept list.
 *  Removing a published code is a 422 on the server (deactivate
 *  instead); the ApiError detail carries that message verbatim. */
export function putListDraft(
  client: ApiClient,
  kind: string,
  key: string,
  concepts: ListConcept[],
): Promise<PutListDraftResult> {
  return client.request<PutListDraftResult>(
    `/lists/${encodeURIComponent(kind)}/${encodeURIComponent(key)}/draft`,
    { method: "PUT", body: JSON.stringify({ concepts }) },
  );
}
