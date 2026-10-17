import { describe, expect, it } from "vitest";
import { machineFromWorkflowPayload } from "../api/adt";
import { claimMachineFromPayload } from "./BillingPage";
import { edMachineFromPayloads } from "./EdBoardPage";
import { deriveNextActions } from "./LabPage";
import { nextStatesFromPayload } from "./SurgeryBoardPage";

// Realignment group C: pages render their workflow buttons from the
// published workflow payloads, with the built-in maps as fallback.
// These tests pin the derivations against payloads shaped like the
// seeded defaults (the derivations must reproduce today's buttons).

describe("ADT machine from workflow payload", () => {
  it("builds the transition map and rejects unusable payloads", () => {
    const machine = machineFromWorkflowPayload({
      transitions: [
        { from: "booked", to: "arrived" },
        { from: "booked", to: "cancelled" },
        { from: "arrived", to: "fulfilled" },
      ],
    });
    expect(machine).toEqual({
      booked: ["arrived", "cancelled"],
      arrived: ["fulfilled"],
    });
    expect(machineFromWorkflowPayload(null)).toBeNull();
    expect(machineFromWorkflowPayload({ transitions: [] })).toBeNull();
    expect(machineFromWorkflowPayload({})).toBeNull();
  });
});

describe("lab next actions from workflow payload", () => {
  it("excludes governed targets and keeps the reason flag on rejected", () => {
    const actions = deriveNextActions({
      states: ["ordered", "collected", "received", "in-process", "resulted"],
      transitions: [
        { from: "ordered", to: "collected" },
        { from: "ordered", to: "cancelled" },
        { from: "received", to: "in-process" },
        { from: "received", to: "rejected" },
        { from: "in-process", to: "resulted" },
      ],
      governedStates: ["resulted", "verified", "released"],
    });
    expect(actions["ordered"].map((a) => a.to)).toEqual(["collected", "cancelled"]);
    expect(actions["received"]).toEqual([
      { to: "in-process", label: "Start processing" },
      { to: "rejected", label: "Reject specimen", reason: true },
    ]);
    expect(actions["in-process"]).toBeUndefined();
  });

  it("falls back to the built-in map without a payload", () => {
    const actions = deriveNextActions(null);
    expect(actions["ordered"].map((a) => a.to)).toEqual(["collected", "cancelled"]);
  });
});

describe("claim machine from workflow payload", () => {
  const PAYLOAD = {
    states: ["draft", "ready", "submitted", "paid", "denied", "appealed"],
    transitions: [
      { from: "draft", to: "ready" },
      { from: "ready", to: "submitted" },
      { from: "submitted", to: "paid" },
      { from: "submitted", to: "denied" },
      { from: "denied", to: "appealed" },
      { from: "appealed", to: "submitted" },
    ],
    exportableStates: ["submitted", "paid", "denied", "appealed"],
    maxAppeals: 1,
  };

  it("reproduces the built-in buttons under the seeded defaults", () => {
    const machine = claimMachineFromPayload(PAYLOAD);
    expect(machine?.maxAppeals).toBe(1);
    expect(machine?.exportable).toEqual(["submitted", "paid", "denied", "appealed"]);
    expect(machine?.actions["denied"]).toEqual([
      { to: "appealed", label: "Appeal (once, with note)", note: true },
    ]);
    expect(machine?.actions["appealed"]).toEqual([
      { to: "submitted", label: "Resubmit after appeal" },
    ]);
  });

  it("maxAppeals=2 drops the 'once' wording", () => {
    const machine = claimMachineFromPayload({ ...PAYLOAD, maxAppeals: 2 });
    expect(machine?.maxAppeals).toBe(2);
    expect(machine?.actions["denied"][0].label).toBe("Appeal (with note)");
  });
});

describe("ED machine from workflow + triage payloads", () => {
  it("derives columns from activeStates and the ESI range from min/max", () => {
    const machine = edMachineFromPayloads(
      {
        transitions: [
          { from: "waiting", to: "triage" },
          { from: "triage", to: "in-treatment" },
          { from: "in-treatment", to: "disposition" },
          { from: "disposition", to: "admitted" },
        ],
        activeStates: ["waiting", "triage"],
        governedStates: ["admitted", "departed"],
      },
      { min: 1, max: 3 },
    );
    expect(machine?.columns.map((c) => c.key)).toEqual(["waiting", "triage"]);
    expect(machine?.esiRange).toEqual([1, 2, 3]);
    expect(machine?.nextAction["triage"]).toEqual({
      to: "in-treatment",
      label: "Start treatment",
    });
    // Governed targets never become bare buttons.
    expect(machine?.nextAction["disposition"]).toBeUndefined();
  });
});

describe("surgery next states from workflow payload", () => {
  it("groups targets by source state", () => {
    expect(
      nextStatesFromPayload({
        transitions: [
          { from: "scheduled", to: "pre-op" },
          { from: "scheduled", to: "cancelled" },
          { from: "pre-op", to: "in-op" },
        ],
      }),
    ).toEqual({ scheduled: ["pre-op", "cancelled"], "pre-op": ["in-op"] });
    expect(nextStatesFromPayload(null)).toBeNull();
  });
});
