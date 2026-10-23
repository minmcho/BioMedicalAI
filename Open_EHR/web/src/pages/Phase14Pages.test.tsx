import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../api/client";
import { DataPlatformPage } from "./DataPlatformPage";
import { DevicesPage } from "./DevicesPage";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

// Phase 14 pages: the device registry / calibration worklist / honest
// feed status, and the data platform (raw-zone lake + lineage,
// governed pipelines, quarantine, dead letters, dedup candidates) —
// rendered from the /devices + /pipelines API surface through stub
// clients, the Phase 13 page-test pattern.

function devicesClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    devices: vi.fn(async () => ({
      items: [
        {
          device_key: "chem-analyser-1", name: "Chemistry analyser",
          device_type: "analyser", location: "Central lab", serial: "SN-CHEM-1001",
          status: "active", hl7_sender: "ANALYZER-X", mqtt_topic_key: null,
          calibration_due: "2027-02-14T00:00:00+00:00", maintenance_due: null,
          last_calibrated_at: "2026-08-17T00:00:00+00:00",
          calibration: { status: "ok", due: "2027-02-14T00:00:00+00:00", days_remaining: 120 },
          blocked_reason: null, feed_label: "simulated",
        },
        {
          device_key: "fridge-pharmacy-1", name: "Pharmacy fridge sensor",
          device_type: "fridge-sensor", location: "Pharmacy", serial: "SN-FRG-3003",
          status: "active", hl7_sender: null, mqtt_topic_key: "fridge-pharmacy-1",
          calibration_due: "2026-09-01T00:00:00+00:00", maintenance_due: null,
          last_calibrated_at: "2025-09-01T00:00:00+00:00",
          calibration: { status: "overdue", due: "2026-09-01T00:00:00+00:00", days_remaining: -47 },
          blocked_reason: "calibration is overdue (due 2026-09-01)", feed_label: "simulated",
        },
      ],
      total: 2, overdue: 1,
    })),
    calibrationWorklist: vi.fn(async () => ({
      items: [
        {
          device_key: "fridge-pharmacy-1", name: "Pharmacy fridge sensor",
          device_type: "fridge-sensor", location: "Pharmacy", serial: "SN-FRG-3003",
          status: "active", hl7_sender: null, mqtt_topic_key: "fridge-pharmacy-1",
          calibration_due: "2026-09-01T00:00:00+00:00", maintenance_due: null,
          last_calibrated_at: "2025-09-01T00:00:00+00:00",
          calibration: { status: "overdue", due: "2026-09-01T00:00:00+00:00", days_remaining: -47 },
          blocked_reason: "calibration is overdue", feed_label: "simulated",
        },
      ],
      total: 1,
    })),
    feedsStatus: vi.fn(async () => ({
      feed_label: "simulated",
      note: "All device feeds in this deployment are simulated or recorded inputs.",
      devices: { total: 2, overdue: 1, blocked: 1 },
      mllp_gate: { enabled: true, on_unknown_device: "dead-letter", on_calibration_overdue: "block" },
      mqtt: { configured: false, state: "off" },
      lake_entries: 7, quarantine_held: 1, dead_letters: 0,
    })),
    deviceReadings: vi.fn(async () => [
      {
        id: 1, device_key: "fridge-pharmacy-1", metric: "temperature",
        value: 9.4, unit: "C", recorded_at: "2026-10-18T10:00:00+00:00",
        received_at: "2026-10-18T10:00:01+00:00", source: "device-api",
        source_label: "simulated", excursion: true, raw_entry_id: 3,
      },
    ]),
    recordCalibration: vi.fn(async () => ({
      device_key: "fridge-pharmacy-1", calibration_due: "2027-10-18T00:00:00+00:00",
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("DevicesPage", () => {
  it("renders the registry with calibration posture and the honest simulated label", async () => {
    render(<DevicesPage client={devicesClient()} role="clinician" />);
    expect(await screen.findByText("Chemistry analyser")).toBeTruthy();
    expect(screen.getAllByText(/simulated/).length).toBeGreaterThan(0);
    expect(screen.getByText("SN-CHEM-1001")).toBeTruthy();
    // The overdue fridge is flagged and gate-blocked in the table.
    expect(screen.getAllByText("overdue").length).toBeGreaterThan(0);
    expect(screen.getByText(/calibration is overdue \(due 2026-09-01\)/)).toBeTruthy();
    // Feed status is honest: MQTT off with no broker configured.
    expect(screen.getByText(/no broker configured/)).toBeTruthy();
    expect(screen.getByText(/dead-letter/)).toBeTruthy();
    // UX pass (spec P-7): readings live in their own tab now.
    fireEvent.click(screen.getByRole("tab", { name: "Readings & barcode" }));
    // The excursion reading shows its source label.
    expect(await screen.findByText("EXCURSION")).toBeTruthy();
  });

  it("records a calibration through the client for a manage role", async () => {
    const client = devicesClient();
    render(<DevicesPage client={client} role="clinician" />);
    const buttons = await screen.findAllByText("Record calibration");
    fireEvent.click(buttons[1]);
    await waitFor(() =>
      expect(client.recordCalibration).toHaveBeenCalledWith(
        "fridge-pharmacy-1", expect.any(String)));
    expect(await screen.findByText(/Calibration recorded for fridge-pharmacy-1/)).toBeTruthy();
  });

  it("hides calibration actions from the read-only viewer", async () => {
    render(<DevicesPage client={devicesClient()} role="viewer" />);
    expect(await screen.findByText("Chemistry analyser")).toBeTruthy();
    expect(screen.queryByText("Record calibration")).toBeNull();
  });
});

function platformClient(overrides: Record<string, unknown> = {}): ApiClient {
  return {
    lakeEntries: vi.fn(async () => ({
      items: [
        {
          id: 3, payload_type: "device-reading", content_hash: "abcdef1234567890",
          source: "device-api", source_label: "simulated",
          device_key: "fridge-pharmacy-1", interface: "device-api",
          patient_ref: null, disposition: "received", supersedes_id: null,
          received_at: "2026-10-18T10:00:01+00:00", payload_bytes: 84,
        },
      ],
      total_entries: 1, by_disposition: { received: 1 },
    })),
    lakeEntry: vi.fn(async () => ({
      id: 3, payload_type: "device-reading", content_hash: "abcdef1234567890",
      source: "device-api", source_label: "simulated",
      device_key: "fridge-pharmacy-1", interface: "device-api",
      patient_ref: null, disposition: "received", supersedes_id: null,
      received_at: "2026-10-18T10:00:01+00:00", payload_bytes: 84,
      payload: "{\"metric\": \"temperature\", \"value\": 9.4}",
      curated_links: [{ curated_ref: "DeviceReading/1", link_kind: "ingested-as" }],
    })),
    lineage: vi.fn(async () => ({
      curated_ref: "Observation/obs-1", total: 1,
      raw_entries: [
        {
          id: 5, payload_type: "fhir-observation", content_hash: "0123456789abcdef",
          source: "fhir-ingest", source_label: "simulated",
          device_key: "vitals-monitor-1", interface: "fhir-observation",
          patient_ref: "Patient/pat-1", disposition: "received",
          supersedes_id: null, received_at: "2026-10-18T10:05:00+00:00",
          payload_bytes: 210, link_kind: "ingested-as",
          device: { device_key: "vitals-monitor-1" },
        },
      ],
    })),
    pipelineDefinitions: vi.fn(async () => [
      {
        key: "mpi-dedup-scan", version: 1, name: "MPI dedup scan",
        description: null, source: "fhir-patients", schedule: null,
        enabled: true, steps: [{ type: "dedup-scan", checks: 0 }],
        last_run: { run_id: "run-1", status: "succeeded" },
      },
      {
        key: "lake-replay", version: 1, name: "Lake replay",
        description: null, source: "lake", schedule: null,
        enabled: false, steps: [{ type: "replay-lake", checks: 0 }],
        last_run: null,
      },
    ]),
    pipelineRuns: vi.fn(async () => [
      {
        run_id: "run-1", pipeline_key: "mpi-dedup-scan", pipeline_version: 1,
        trigger: "manual", window_start: null, window_end: null,
        status: "succeeded", actor: "dev-admin", detail: {},
        started_at: "2026-10-18T09:00:00+00:00", finished_at: "2026-10-18T09:00:02+00:00",
        steps: [
          {
            step_index: 0, step_type: "dedup-scan", status: "succeeded",
            attempts: 1, rows_in: 3, rows_out: 1, idempotency_key: "k",
            detail: {}, error: null,
          },
        ],
      },
    ]),
    pipelineDeadLetters: vi.fn(async () => []),
    quarantine: vi.fn(async () => [
      {
        id: 2, source: "pipeline", reason: "DQ check row-count-floor failed",
        detail: {}, raw_entry_id: null, pipeline_run_id: "run-9",
        status: "held", decided_by: null, decided_at: null,
        decision_note: null, created_at: "2026-10-18T09:30:00+00:00",
      },
    ]),
    dedupCandidates: vi.fn(async () => [
      {
        id: 1, patient_a: "pat-a", patient_b: "pat-b", score: 80,
        reasons: { dob: "match", name: "exact" }, status: "pending",
        reviewed_by: null, review_note: null,
        created_at: "2026-10-18T09:00:02+00:00",
      },
    ]),
    runPipeline: vi.fn(async () => ({ run_id: "run-2", status: "succeeded" })),
    releaseQuarantine: vi.fn(async () => ({})),
    rejectQuarantine: vi.fn(async () => ({})),
    replayPipelineDeadLetter: vi.fn(async () => ({})),
    reviewDedupCandidate: vi.fn(async () => ({
      candidate: { id: 1, status: "not-duplicates" }, merge_performed: false,
    })),
    ...overrides,
  } as unknown as ApiClient;
}

describe("DataPlatformPage", () => {
  it("renders the lake, pipelines, quarantine and dedup candidates", async () => {
    // Adjusted for the depth-pass Tabs rollout (Workstream 1): the
    // stacked sections now live behind tabs (Lake default), so the
    // test clicks through the tabs — assertions unchanged.
    render(<DataPlatformPage client={platformClient()} role="admin" />);
    expect(await screen.findByText("device-reading")).toBeTruthy();
    expect(screen.getByText(/abcdef123456/)).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Pipelines" }));
    expect(await screen.findByText("MPI dedup scan")).toBeTruthy();
    // The disabled pipeline cannot be run from the page.
    const runButtons = screen.getAllByText("Run now");
    expect(runButtons).toHaveLength(2);
    expect((runButtons[1] as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("tab", { name: "Quarantine" }));
    expect(screen.getByText("DQ check row-count-floor failed")).toBeTruthy();
    fireEvent.click(screen.getByRole("tab", { name: "Dedup review" }));
    expect(screen.getByText("pat-a")).toBeTruthy();
    expect(screen.getByText("pat-b")).toBeTruthy();
    expect(screen.getByText(/never merges/)).toBeTruthy();
  });

  it("shows the raw payload and curated links on demand", async () => {
    const client = platformClient();
    render(<DataPlatformPage client={client} role="admin" />);
    fireEvent.click(await screen.findByText("View payload"));
    expect(await screen.findByText(/"value": 9.4/)).toBeTruthy();
    expect(screen.getByText(/DeviceReading\/1 \(ingested-as\)/)).toBeTruthy();
  });

  it("answers the lineage question with device and hash", async () => {
    const client = platformClient();
    render(<DataPlatformPage client={client} role="clinician" />);
    // Depth-pass Tabs rollout: lineage lives in the default Lake tab.
    expect(await screen.findByText("device-reading")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Lineage resource id"), {
      target: { value: "obs-1" },
    });
    fireEvent.click(screen.getByText("Where did this come from?"));
    await waitFor(() =>
      expect(client.lineage).toHaveBeenCalledWith("Observation", "obs-1"));
    expect(await screen.findByText(/vitals-monitor-1/)).toBeTruthy();
  });

  it("runs a pipeline and reviews a dedup candidate as admin", async () => {
    const client = platformClient();
    render(<DataPlatformPage client={client} role="admin" />);
    // Depth-pass Tabs rollout: pipelines and dedup are separate tabs.
    fireEvent.click(await screen.findByRole("tab", { name: "Pipelines" }));
    const runButtons = await screen.findAllByText("Run now");
    fireEvent.click(runButtons[0]);
    await waitFor(() =>
      expect(client.runPipeline).toHaveBeenCalledWith("mpi-dedup-scan"));
    fireEvent.click(screen.getByRole("tab", { name: "Dedup review" }));
    fireEvent.click(screen.getByText("Not duplicates"));
    await waitFor(() =>
      expect(client.reviewDedupCandidate).toHaveBeenCalledWith(1, "not-duplicates"));
  });

  it("hides run/review actions from the clinician (pipelines.run is admin/configurator)", async () => {
    render(<DataPlatformPage client={platformClient()} role="clinician" />);
    // Depth-pass Tabs rollout: each surface is checked in its tab.
    fireEvent.click(await screen.findByRole("tab", { name: "Pipelines" }));
    expect(await screen.findByText("MPI dedup scan")).toBeTruthy();
    expect(screen.queryByText("Run now")).toBeNull();
    fireEvent.click(screen.getByRole("tab", { name: "Dedup review" }));
    expect(screen.queryByText("Not duplicates")).toBeNull();
    // But quarantine release/reject (devices.manage) stays available.
    fireEvent.click(screen.getByRole("tab", { name: "Quarantine" }));
    expect(screen.getByText("Release")).toBeTruthy();
    expect(screen.getByText("Reject")).toBeTruthy();
  });
});
