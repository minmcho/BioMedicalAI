import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DevicesPage } from "./DevicesPage";

// Phase 15 Devices RFID panel + barcode lookup: tags render from
// /rfid, bind goes through client.request, and the universal barcode
// lookup renders kind / chain / history from /barcodes/lookup.
afterEach(cleanup);

function stubClient(overrides: Record<string, unknown> = {}): ApiClient {
  const request = vi.fn(async (path: string) => {
    if (path === "/rfid/config") return { tag_types: [{ code: "asset", display: "Asset" }], zones: [{ code: "icu", display: "ICU" }], readers: [{ readerId: "reader-icu-1", zone: "icu", active: true }], feed_label: "simulated" };
    if (path === "/rfid/tags") return { items: [
      { epc: "EPC-ASSET-001", tag_type: "asset", entity_type: "Device", entity_id: "pump-1", entity_ref: "Device/pump-1", status: "bound", last_seen_zone: "icu", last_seen_at: "2026-10-18T09:00:00+00:00" },
      { epc: "EPC-INV-009", tag_type: "inventory", entity_type: "", entity_id: "", entity_ref: null, status: "unbound", last_seen_zone: null, last_seen_at: null },
    ], total: 2, feed_label: "simulated" };
    if (path === "/rfid/dead-letters") return { items: [{ id: 1, reader_id: "reader-x", epc: "EPC-UNKNOWN", disposition: "unknown-reader", reason: "Reader 'reader-x' is not in the published registry", created_at: null }], total: 1 };
    if (path === "/rfid/tags/EPC-ASSET-001") return { epc: "EPC-ASSET-001", tag_type: "asset", entity_type: "Device", entity_id: "pump-1", entity_ref: "Device/pump-1", status: "bound", last_seen_zone: "icu", last_seen_at: "2026-10-18T09:00:00+00:00", sightings: [{ id: 7, epc: "EPC-ASSET-001", reader_id: "reader-icu-1", zone: "icu", signal: -55, seen_at: "2026-10-18T09:00:00+00:00", source_label: "simulated" }], sighting_count: 1 };
    if (path === "/rfid/tags/EPC-INV-009/bind") return { epc: "EPC-INV-009", tag_type: "inventory", entity_type: "Device", entity_id: "pump-2", entity_ref: "Device/pump-2", status: "bound", last_seen_zone: null, last_seen_at: null };
    if (path.startsWith("/barcodes/lookup")) return { registered: true, value: "OCSP1|spec-1", kind: "specimen", entity_ref: "Specimen/spec-1", status: "active", issued_by: "lab-tech", label_fields: {}, entity: { resolved: true, specimen: { specimen_id: "spec-1", accession: "ACC-100", status: "available", parent_refs: [] }, family_chain: { specimen_id: "spec-1", accession: "ACC-100", family_barcodes: [{ value: "OCAL1|spec-1-a", kind: "aliquot", entity_ref: "Specimen/spec-1-a", parent_value: "OCSP1|spec-1" }] } }, history: [{ event: "registered", actor: "lab-tech", detail: {}, at: "2026-10-18T08:00:00+00:00" }] };
    return { items: [], total: 0 };
  });
  return {
    request,
    devices: vi.fn(async () => ({ items: [], total: 0, overdue: 0 })),
    calibrationWorklist: vi.fn(async () => ({ items: [], total: 0 })),
    feedsStatus: vi.fn(async () => ({ feed_label: "simulated", devices: { total: 0, overdue: 0, blocked: 0 }, mllp_gate: { enabled: false }, mqtt: { state: "off", configured: false }, lake_entries: 0, quarantine_held: 0, dead_letters: 0 })),
    deviceReadings: vi.fn(async () => []),
    recordCalibration: vi.fn(async () => ({})),
    ...overrides,
  } as unknown as ApiClient;
}

describe("DevicesPage RFID panel (Phase 15)", () => {
  it("renders the tag registry with the honest simulated feed label and dead letters", async () => {
    render(<DevicesPage client={stubClient()} role="clinician" section="rfid" />);
    expect(await screen.findByText("EPC-ASSET-001")).toBeTruthy();
    expect(screen.getByText("Device/pump-1")).toBeTruthy();
    expect(screen.getAllByText(/simulated/).length).toBeGreaterThan(0);
    expect(screen.getByText("unknown-reader")).toBeTruthy();
    // Last seen zone is shown in the registry row.
    expect(screen.getAllByText("icu").length).toBeGreaterThan(0);
  });

  it("binds an unbound tag through the API and confirms", async () => {
    const client = stubClient();
    render(<DevicesPage client={client} role="clinician" section="rfid" />);
    await screen.findByText("EPC-INV-009");
    fireEvent.change(screen.getByLabelText("Entity type for tag EPC-INV-009"), { target: { value: "Device" } });
    fireEvent.change(screen.getByLabelText("Entity id for tag EPC-INV-009"), { target: { value: "pump-2" } });
    const bindButtons = screen.getAllByRole("button", { name: "Bind" });
    fireEvent.click(bindButtons[bindButtons.length - 1]);
    await waitFor(() =>
      expect(client.request).toHaveBeenCalledWith(
        "/rfid/tags/EPC-INV-009/bind",
        expect.objectContaining({ method: "POST" }),
      ),
    );
    expect(await screen.findByText(/Bind tag EPC-INV-009 — done/)).toBeTruthy();
  });

  it("asset find shows the last sighting and zone for an EPC", async () => {
    render(<DevicesPage client={stubClient()} role="clinician" section="rfid" />);
    await screen.findByText("EPC-ASSET-001");
    fireEvent.change(screen.getByLabelText("Tag EPC to find"), { target: { value: "EPC-ASSET-001" } });
    fireEvent.click(screen.getByRole("button", { name: "Find tag" }));
    expect(await screen.findByText(/Last sighting: zone icu via reader-icu-1/)).toBeTruthy();
  });

  it("barcode lookup on the default view renders kind, chain and history", async () => {
    render(<DevicesPage client={stubClient()} role="clinician" />);
    // UX pass (spec P-7): barcode lookup lives in the Readings tab.
    fireEvent.click(await screen.findByRole("tab", { name: "Readings & barcode" }));
    const input = await screen.findByLabelText("Barcode value");
    fireEvent.change(input, { target: { value: "OCSP1|spec-1" } });
    fireEvent.click(screen.getByRole("button", { name: "Look up barcode" }));
    expect(await screen.findByText("specimen")).toBeTruthy();
    expect(screen.getByText(/Specimen chain: accession ACC-100/)).toBeTruthy();
    expect(screen.getByText(/OCAL1\|spec-1-a/)).toBeTruthy();
    expect(screen.getByText(/registered — lab-tech/)).toBeTruthy();
  });
});
