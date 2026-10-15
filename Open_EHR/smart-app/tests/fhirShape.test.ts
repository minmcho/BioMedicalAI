import { describe, expect, it } from "vitest";
import {
  bundleResources,
  groupLabTrends,
  sparklinePoints,
  sparklineSvg,
  summarize,
} from "../src/fhirShape";
import {
  conditionBundle,
  medicationBundle,
  observationBundle,
  patientResource,
} from "./fixtures";

describe("bundleResources", () => {
  it("unpacks a searchset Bundle", () => {
    const resources = bundleResources(conditionBundle);
    expect(resources).toHaveLength(2);
    expect(resources[0].resourceType).toBe("Condition");
  });

  it("passes a single resource through and rejects garbage", () => {
    expect(bundleResources(patientResource)).toEqual([patientResource]);
    expect(bundleResources(null)).toEqual([]);
    expect(bundleResources({ nope: true })).toEqual([]);
  });
});

describe("summarize", () => {
  it("composes demographics, conditions, and medications", () => {
    const summary = summarize(
      patientResource,
      bundleResources(conditionBundle),
      bundleResources(medicationBundle),
    );
    expect(summary.name).toBe("Camila Maria Sandbox");
    expect(summary.birthDate).toBe("1959-06-11");
    expect(summary.conditions.map((c) => c.code)).toEqual(["I10", "E11.9"]);
    expect(summary.medications).toEqual([
      { display: "Lisinopril 10 MG Oral Tablet", status: "active" },
    ]);
  });
});

describe("groupLabTrends", () => {
  it("groups by code, sorts points by date, and skips non-numeric values", () => {
    const trends = groupLabTrends(bundleResources(observationBundle));
    const codes = trends.map((t) => t.code);
    expect(codes).toEqual(["2345-7", "4548-4"]); // sorted by display name
    const a1c = trends.find((t) => t.code === "4548-4");
    expect(a1c?.unit).toBe("%");
    expect(a1c?.points.map((p) => p.value)).toEqual([7.9, 7.4, 7.1]);
    expect(a1c?.points.map((p) => p.date)).toEqual([
      "2025-01-01T08:00:00Z",
      "2025-04-01T08:00:00Z",
      "2025-07-01T08:00:00Z",
    ]);
  });
});

describe("sparklines", () => {
  it("maps a series into the SVG box with min at the bottom", () => {
    const pts = sparklinePoints(
      [
        { date: "2025-01-01", value: 10 },
        { date: "2025-02-01", value: 20 },
      ],
      100,
      30,
      2,
    );
    expect(pts).toBe("2.0,28.0 98.0,2.0");
  });

  it("keeps a flat series at mid-height and handles a single point", () => {
    expect(sparklinePoints([{ date: "2025-01-01", value: 5 }], 100, 30, 2)).toBe("50.0,15.0");
    expect(sparklinePoints([], 100, 30, 2)).toBe("");
  });

  it("renders an SVG polyline with the series label", () => {
    const [series] = groupLabTrends(bundleResources(observationBundle)).filter(
      (t) => t.code === "4548-4",
    );
    const svg = sparklineSvg(series);
    expect(svg).toContain("<polyline");
    expect(svg).toContain("Hemoglobin A1c");
  });
});
