import { describe, expect, it } from "vitest";
import { allowedTransitions, canTransition, isTerminal } from "./adt";

describe("ADT transition logic (mirrors the backend state machine)", () => {
  it("booked can arrive or cancel, nothing else", () => {
    expect(allowedTransitions("booked")).toEqual(["arrived", "cancelled"]);
    expect(canTransition("booked", "arrived")).toBe(true);
    expect(canTransition("booked", "cancelled")).toBe(true);
    expect(canTransition("booked", "fulfilled")).toBe(false);
    expect(canTransition("booked", "booked")).toBe(false);
  });

  it("arrived and checked-in can fulfil or cancel", () => {
    expect(allowedTransitions("arrived")).toEqual(["fulfilled", "cancelled"]);
    expect(allowedTransitions("checked-in")).toEqual(["fulfilled", "cancelled"]);
  });

  it("proposed and pending can book or cancel", () => {
    expect(allowedTransitions("proposed")).toEqual(["booked", "cancelled"]);
    expect(allowedTransitions("pending")).toEqual(["booked", "cancelled"]);
  });

  it("terminal states offer no transitions and report terminal", () => {
    for (const status of ["fulfilled", "cancelled", "noshow"]) {
      expect(allowedTransitions(status)).toEqual([]);
      expect(isTerminal(status)).toBe(true);
      expect(canTransition(status, "cancelled")).toBe(false);
    }
    expect(isTerminal("booked")).toBe(false);
  });

  it("unknown or missing status offers no transitions", () => {
    expect(allowedTransitions("something-else")).toEqual([]);
    expect(allowedTransitions(undefined)).toEqual([]);
    expect(isTerminal(undefined)).toBe(false);
  });
});
