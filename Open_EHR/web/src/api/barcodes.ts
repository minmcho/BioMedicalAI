// Barcode registry, universal lookup + label printing (Phase 15,
// scope H). The single source of truth is backend/app/barcodes.py:
// the kind vocabulary (wristband | specimen | aliquot | blood |
// medication | asset), the published Studio standards it validates
// against (integration/barcode-standards), and the label renderer
// (GET /barcodes/labels/{kind}/{entity_ref} — a label-sized PDF
// rendered server-side via the Phase 13 print machinery, audited
// server-side on every render). Types for these payloads live here,
// not in api/types.ts, per the Phase 15 web brief.

import type { ApiClient } from "./client";
import type { DownloadedFile } from "./types";

export type BarcodeKind =
  | "wristband"
  | "specimen"
  | "aliquot"
  | "blood"
  | "medication"
  | "asset";

export interface BarcodeRecord {
  value: string;
  kind: string;
  entity_ref: string;
  label_fields: Record<string, unknown>;
  status: string;
  issued_by: string;
  created_at: string | null;
}

export interface BarcodeListResponse {
  items: BarcodeRecord[];
  total: number;
}

export interface BarcodeHistoryEntry {
  event: string;
  actor: string;
  detail: Record<string, unknown>;
  at: string | null;
}

/** Successful GET /barcodes/lookup shape. An unknown value is a 404
 *  on the server (its JSON body says plainly the value is not a
 *  registered barcode); through ApiClient that surfaces as an
 *  ApiError whose `detail` carries the server message. */
export interface BarcodeLookupResult {
  registered: boolean;
  value: string;
  kind?: string;
  entity_ref?: string;
  status?: string;
  issued_by?: string;
  label_fields?: Record<string, unknown>;
  entity?: Record<string, unknown>;
  history?: BarcodeHistoryEntry[];
}

export interface BarcodeStandardsResponse {
  standards: Record<string, unknown>;
  kinds: string[];
}

export interface RegisterBarcodeInput {
  value: string;
  kind: BarcodeKind | string;
  entity_ref: string;
  label_fields?: Record<string, unknown>;
}

/** The label route takes entity_ref as a path suffix
 *  ("Patient/pat-1", "MedicationDispense/disp-1"): encode each
 *  segment, never the separating slash. */
function encodeEntityRef(entityRef: string): string {
  return entityRef.split("/").map(encodeURIComponent).join("/");
}

/** GET /barcodes/labels/{kind}/{entity_ref} — one print-ready label
 *  PDF (via client.requestBlob, the same binary path report PDFs
 *  use). A 404 (unknown entity / no such label) throws ApiError
 *  with the server detail. */
export function fetchBarcodeLabel(
  client: ApiClient,
  kind: BarcodeKind | string,
  entityRef: string,
): Promise<DownloadedFile> {
  return client.requestBlob(
    `/barcodes/labels/${encodeURIComponent(kind)}/${encodeEntityRef(entityRef)}`,
  );
}

/** Wristband label for one patient: kind "wristband", entity
 *  "Patient/<id>" (accepts a bare id or a full Patient/ ref). */
export function fetchWristbandLabel(
  client: ApiClient,
  patientId: string,
): Promise<DownloadedFile> {
  const entityRef = patientId.includes("/")
    ? patientId
    : `Patient/${patientId}`;
  return fetchBarcodeLabel(client, "wristband", entityRef);
}

/** Medication (unit-dose med) label for one dispense: kind
 *  "medication", entity "MedicationDispense/<id>" (accepts a bare
 *  dispense id or a full MedicationDispense/ ref). The backend also
 *  resolves a pharmacy lot number under this kind. */
export function fetchMedicationLabel(
  client: ApiClient,
  dispenseId: string,
): Promise<DownloadedFile> {
  const entityRef = dispenseId.includes("/")
    ? dispenseId
    : `MedicationDispense/${dispenseId}`;
  return fetchBarcodeLabel(client, "medication", entityRef);
}

/** GET /barcodes/lookup?value= — the universal scan. */
export function lookupBarcode(
  client: ApiClient,
  value: string,
): Promise<BarcodeLookupResult> {
  return client.request<BarcodeLookupResult>(
    `/barcodes/lookup?value=${encodeURIComponent(value)}`,
  );
}

/** GET /barcodes[?kind=][?status=] — the registry list. */
export function listBarcodes(
  client: ApiClient,
  params: { kind?: string; status?: string } = {},
): Promise<BarcodeListResponse> {
  const qs = new URLSearchParams();
  if (params.kind) qs.set("kind", params.kind);
  if (params.status) qs.set("status", params.status);
  const query = qs.toString();
  return client.request<BarcodeListResponse>(
    `/barcodes${query ? `?${query}` : ""}`,
  );
}

/** GET /barcodes/standards — published barcode standards. */
export function getBarcodeStandards(
  client: ApiClient,
): Promise<BarcodeStandardsResponse> {
  return client.request<BarcodeStandardsResponse>("/barcodes/standards");
}

/** POST /barcodes — register one barcode value against its entity. */
export function registerBarcode(
  client: ApiClient,
  input: RegisterBarcodeInput,
): Promise<BarcodeRecord> {
  return client.request<BarcodeRecord>("/barcodes", {
    method: "POST",
    body: JSON.stringify(input),
  });
}

/** POST /barcodes/{value}/revoke — retire a barcode value. */
export function revokeBarcode(
  client: ApiClient,
  value: string,
): Promise<BarcodeRecord> {
  return client.request<BarcodeRecord>(
    `/barcodes/${encodeURIComponent(value)}/revoke`,
    { method: "POST" },
  );
}
