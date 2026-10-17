import { describe, expect, it } from "vitest";
import {
  emptyPermissions,
  parseBranding,
  parseForm,
  parseIntegration,
  parseNavigation,
  parsePermissions,
  parseTerminology,
  parseWorkflow,
  serializeBranding,
  serializeForm,
  serializeIntegration,
  serializeNavigation,
  serializePermissions,
  serializeTerminology,
  serializeWorkflow,
  terminologyError,
} from "./models";

// Payload fixtures mirror the backend's own test/seed shapes:
// backend/tests/test_studio_config.py (FORM/BRANDING/TERMINOLOGY),
// backend/app/lab.py (lab catalog concepts), configstore defaults (ADT).

const FORM = {
  resourceType: "Questionnaire",
  title: "Intake form",
  item: [
    { linkId: "q1", text: "Chief complaint", type: "string", required: true },
    { linkId: "q2", text: "Current smoker?", type: "boolean", required: false,
      valueSet: "ValueSet/yes-no" },
    { linkId: "q3", text: "Pain scale", type: "choice", required: false,
      answerOption: [
        { valueCoding: { code: "none", display: "No pain" } },
        { valueCoding: { code: "severe", display: "Severe pain" } },
      ] },
  ],
};

describe("form model", () => {
  it("round-trips a Questionnaire payload both directions", () => {
    expect(serializeForm(parseForm(FORM))).toEqual(FORM);
  });

  it("parses choice options from answerOption valueCodings", () => {
    const model = parseForm(FORM);
    expect(model.items[2].options).toEqual([
      { code: "none", display: "No pain" },
      { code: "severe", display: "Severe pain" },
    ]);
  });

  it("preserves unmodelled item fields (valueSet) through a round-trip", () => {
    const model = parseForm(FORM);
    model.items[0].text = "Edited text";
    const out = serializeForm(model);
    const items = out.item as Array<Record<string, unknown>>;
    expect(items[0].text).toBe("Edited text");
    expect(items[1].valueSet).toBe("ValueSet/yes-no");
    expect(out.resourceType).toBe("Questionnaire");
  });
});

const WORKFLOW = {
  states: ["booked", "arrived", "fulfilled", "cancelled"],
  transitions: [
    { from: "booked", to: "arrived" },
    { from: "arrived", to: "fulfilled", action: "complete", roles: ["clinician"] },
  ],
};

describe("workflow model", () => {
  it("round-trips a states/transitions payload", () => {
    expect(serializeWorkflow(parseWorkflow(WORKFLOW))).toEqual(WORKFLOW);
  });

  it("carries initial/terminal marks as payload fields", () => {
    const model = parseWorkflow(WORKFLOW);
    model.states[0].initial = true;
    model.states[3].terminal = true;
    const out = serializeWorkflow(model);
    expect(out.initial).toBe("booked");
    expect(out.terminal).toEqual(["cancelled"]);
    const back = parseWorkflow(out);
    expect(back.states.find((s) => s.name === "booked")?.initial).toBe(true);
    expect(back.states.find((s) => s.name === "cancelled")?.terminal).toBe(true);
  });

  it("omits empty action/roles instead of writing empty values", () => {
    const out = serializeWorkflow(parseWorkflow(WORKFLOW));
    const transitions = out.transitions as Array<Record<string, unknown>>;
    expect(transitions[0]).toEqual({ from: "booked", to: "arrived" });
    expect(transitions[1].action).toBe("complete");
    expect(transitions[1].roles).toEqual(["clinician"]);
  });
});

const TERMINOLOGY = {
  name: "OpenCare laboratory catalog (LOINC)",
  system: "http://loinc.org",
  concepts: [
    { code: "718-7", display: "Hemoglobin", active: true, unit: "g/dL",
      referenceRange: { unit: "g/dL", low: 12, high: 15.5, criticalLow: 7, criticalHigh: 20 } },
    { code: "58410-2", display: "CBC panel", active: true, panel: true,
      components: ["718-7"] },
    { code: "6299-2", display: "Urea nitrogen", active: true },
  ],
};

describe("terminology model", () => {
  it("round-trips the lab catalog shape (unit in both places, panel extras kept)", () => {
    expect(serializeTerminology(parseTerminology(TERMINOLOGY))).toEqual(TERMINOLOGY);
  });

  it("parses range numbers into editable text and the shared unit", () => {
    const model = parseTerminology(TERMINOLOGY);
    expect(model.concepts[0]).toMatchObject({
      code: "718-7", unit: "g/dL", low: "12", high: "15.5",
      criticalLow: "7", criticalHigh: "20",
    });
    expect(model.concepts[2].low).toBe("");
  });

  it("serializes edited numbers back to numbers and omits blank ranges", () => {
    const model = parseTerminology(TERMINOLOGY);
    model.concepts[2].low = "7";
    model.concepts[2].unit = "mg/dL";
    const out = serializeTerminology(model);
    const concepts = out.concepts as Array<Record<string, unknown>>;
    expect(concepts[2].referenceRange).toEqual({ low: 7, unit: "mg/dL" });
    expect(concepts[1].components).toEqual(["718-7"]); // panel extra preserved
  });

  it("flags a non-numeric range value as an editor error", () => {
    const model = parseTerminology(TERMINOLOGY);
    model.concepts[0].low = "twelve";
    expect(terminologyError(model)).toMatch(/not a number/);
    model.concepts[0].low = "12";
    expect(terminologyError(model)).toBeNull();
  });
});

const NAVIGATION = {
  items: [
    { module: "scheduling", label: "Scheduling", roles: ["clinician"], order: 1, visible: true },
    { module: "lab", label: "Laboratory", order: 2, visible: false },
  ],
};

describe("navigation model", () => {
  it("round-trips items and derives order from row position", () => {
    expect(serializeNavigation(parseNavigation(NAVIGATION))).toEqual(NAVIGATION);
  });

  it("reordering rows rewrites the stored order", () => {
    const model = parseNavigation(NAVIGATION);
    model.items.reverse();
    const out = serializeNavigation(model);
    const items = out.items as Array<Record<string, unknown>>;
    expect(items[0].module).toBe("lab");
    expect(items[0].order).toBe(1);
    expect(items[1].order).toBe(2);
  });
});

const BRANDING = {
  orgName: "OpenCare Clinic",
  portalTitle: "OpenCare Patient Portal",
  primaryColor: "#1a7f37",
  logoUrl: "https://example.org/logo.png",
};

describe("branding model", () => {
  it("round-trips the governed fields", () => {
    expect(serializeBranding(parseBranding(BRANDING))).toEqual(BRANDING);
  });

  it("carries optional tagline/supportContact when present", () => {
    const model = parseBranding({ ...BRANDING, tagline: "Care, configured.",
      supportContact: "support@example.org" });
    expect(model.tagline).toBe("Care, configured.");
    expect(serializeBranding(model)).toMatchObject({
      tagline: "Care, configured.", supportContact: "support@example.org",
    });
  });
});

describe("permissions model", () => {
  it("starts from the server defaults when no config exists", () => {
    const matrix = emptyPermissions();
    expect(matrix["lab.verify"]).toEqual({
      admin: true, configurator: false, clinician: true, patient: false, viewer: false,
    });
    expect(matrix["studio.manage"].configurator).toBe(true);
  });

  it("applies published overrides over the defaults", () => {
    const matrix = parsePermissions({
      capabilities: { "lab.verify": { configurator: true } },
    });
    expect(matrix["lab.verify"].configurator).toBe(true);
    expect(matrix["lab.verify"].clinician).toBe(true); // default still applies
  });

  it("serializes a complete, valid capabilities payload", () => {
    const matrix = emptyPermissions();
    matrix["billing.manage"].clinician = true;
    const out = serializePermissions(matrix);
    const caps = out.capabilities as Record<string, Record<string, boolean>>;
    expect(caps["billing.manage"]).toEqual({
      admin: true, configurator: true, clinician: true, patient: false, viewer: false,
    });
    // 36 capabilities: the original 14 plus the realignment's six
    // (studio.read, lab.read, pharmacy.read, boards.read,
    // pophealth.view, pophealth.run) plus Phase 5's four (orders.read,
    // orders.place, notes.read, notes.write) plus Phase 6's two
    // (inpatient.read, inpatient.work) plus Phase 7's four
    // (access.read, access.work, coding.read, coding.work) plus
    // Phase 8's three (interop.read, interop.manage, interop.export)
    // plus Phase 9's three (messages.use, portal.self-schedule,
    // pophealth.export) — each new capability ships with defaults on
    // the server (configstore.CAPABILITY_DEFAULTS) and this mirror,
    // per the realignment pattern.
    expect(Object.keys(caps)).toHaveLength(36);
  });
});

const INTEGRATION = {
  viewer: { name: "OHIF viewer (public demo)", baseUrl: "https://viewer.ohif.org" },
};

describe("integration model", () => {
  it("flattens section scalars to key/value rows and back", () => {
    const model = parseIntegration(INTEGRATION);
    expect(model.rows).toEqual([
      { path: "viewer.name", value: "OHIF viewer (public demo)" },
      { path: "viewer.baseUrl", value: "https://viewer.ohif.org" },
    ]);
    expect(serializeIntegration(model)).toEqual(INTEGRATION);
  });

  it("adds a new section through a dotted row", () => {
    const model = parseIntegration(INTEGRATION);
    model.rows.push({ path: "hl7.endpoint", value: "tcp://localhost:2575" });
    const out = serializeIntegration(model);
    expect(out.hl7).toEqual({ endpoint: "tcp://localhost:2575" });
    expect(out.viewer).toEqual(INTEGRATION.viewer);
  });
});
