// Phase 15 RFID registry + barcode lookup client.
// Source of truth: backend/app/rfid.py (/rfid) + backend/app/barcodes.py
// (/barcodes). HONEST LIMIT (from the backend): no physical RFID
// hardware exists in this deployment — every reader event arrives
// through a SIMULATED feed and responses carry source_label /
// feed_label "simulated". The UI must keep that label visible.

import type { ApiClient } from "./client";

export interface RfidConfig {
  tag_types: Array<{ code: string; display: string }>;
  zones: Array<{ code: string; display: string }>;
  readers: Array<{ readerId: string; zone: string; active: boolean }>;
  feed_label: string;
}

export interface RfidTag {
  epc: string;
  tag_type: string;
  entity_type: string;
  entity_id: string;
  entity_ref: string | null;
  status: string;
  last_seen_zone: string | null;
  last_seen_at: string | null;
}

export interface RfidSighting {
  id: number;
  epc: string;
  reader_id: string;
  zone: string;
  signal: number | null;
  seen_at: string | null;
  source_label: string;
}

export interface RfidTagDetail extends RfidTag {
  sightings: RfidSighting[];
  sighting_count: number;
}

export interface RfidDeadLetter {
  id: number;
  reader_id: string;
  epc: string;
  disposition: string;
  reason: string;
  created_at: string | null;
}

export function rfidConfig(client: ApiClient): Promise<RfidConfig> {
  return client.request("/rfid/config");
}
export function listRfidTags(client: ApiClient): Promise<{ items: RfidTag[]; total: number; feed_label: string }> {
  return client.request("/rfid/tags");
}
export function getRfidTag(client: ApiClient, epc: string): Promise<RfidTagDetail> {
  return client.request(`/rfid/tags/${encodeURIComponent(epc)}`);
}
export function createRfidTag(client: ApiClient, input: { epc: string; tag_type: string; entity_type?: string; entity_id?: string }): Promise<RfidTag> {
  return client.request("/rfid/tags", { method: "POST", body: JSON.stringify(input) });
}
export function bindRfidTag(client: ApiClient, epc: string, entityType: string, entityId: string): Promise<RfidTag> {
  return client.request(`/rfid/tags/${encodeURIComponent(epc)}/bind`, {
    method: "POST", body: JSON.stringify({ entity_type: entityType, entity_id: entityId }),
  });
}
export function unbindRfidTag(client: ApiClient, epc: string): Promise<RfidTag> {
  return client.request(`/rfid/tags/${encodeURIComponent(epc)}/unbind`, { method: "POST" });
}
export function retireRfidTag(client: ApiClient, epc: string): Promise<RfidTag> {
  return client.request(`/rfid/tags/${encodeURIComponent(epc)}/retire`, { method: "POST" });
}
export function listRfidDeadLetters(client: ApiClient): Promise<{ items: RfidDeadLetter[]; total: number }> {
  return client.request("/rfid/dead-letters");
}
export function entitySightings(client: ApiClient, entityType: string, entityId: string): Promise<{ tags: RfidTag[]; sightings: RfidSighting[]; total: number; feed_label: string }> {
  return client.request(`/rfid/entities/${encodeURIComponent(entityType)}/${encodeURIComponent(entityId)}/sightings`);
}

// ---- Barcodes: universal lookup + registry ----

export interface BarcodeHistoryEntry { event: string; actor: string; detail: Record<string, unknown>; at: string | null }
export interface SpecimenFamily {
  specimen_id: string;
  accession?: string | null;
  status?: string | null;
  parent_refs?: string[];
  family_barcodes?: Array<{ value: string | null; kind: string | null; entity_ref: string | null; parent_value: string | null }>;
}
export interface BarcodeLookupResult {
  registered: boolean;
  value: string;
  kind: string;
  entity_ref: string;
  status: string;
  issued_by: string;
  label_fields: Record<string, unknown>;
  entity: {
    resolved: boolean;
    patient?: { name: string | null; mrn: string | null; birth_date?: string | null };
    // backend/app/barcodes.py returns the SAME family object under
    // both keys for specimen/aliquot barcodes.
    specimen?: SpecimenFamily;
    family_chain?: SpecimenFamily;
    device?: { device_key: string; name: string; location: string };
    blood_unit?: { unit_id: string; blood_group: string; status: string };
    dispense?: { dispense_id: string; medication_display?: string | null; status?: string | null };
    reason?: string;
  };
  history: BarcodeHistoryEntry[];
}
export interface BarcodeRecordItem { value: string; kind: string; entity_ref: string; status: string; issued_by: string }

export function barcodeLookup(client: ApiClient, value: string): Promise<BarcodeLookupResult> {
  return client.request(`/barcodes/lookup?value=${encodeURIComponent(value)}`);
}
export function listBarcodes(client: ApiClient): Promise<{ items: BarcodeRecordItem[]; total: number }> {
  return client.request("/barcodes");
}
