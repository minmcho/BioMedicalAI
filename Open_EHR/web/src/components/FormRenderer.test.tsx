import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

afterEach(cleanup);
import type { FormDefinition } from "../api/types";
import { buildAnswers, FormRenderer, initialFormValues } from "./FormRenderer";

// The renderer is the web half of "the system configures to the
// requirement": a published form (Studio config) becomes an input
// surface with no code written per form, and the answers it builds are
// the exact QuestionnaireResponse items the server validates.

const FORM: FormDefinition = {
  resourceType: "Questionnaire",
  title: "Synthetic mixed form",
  item: [
    { linkId: "name", text: "Name", type: "string", required: true },
    { linkId: "count", text: "Count", type: "integer" },
    { linkId: "ratio", text: "Ratio", type: "decimal" },
    { linkId: "agree", text: "Agrees", type: "boolean" },
    { linkId: "when", text: "When", type: "date" },
    {
      linkId: "level",
      text: "Level",
      type: "choice",
      answerOption: [
        { valueCoding: { system: "http://example.org/levels", code: "low", display: "Low" } },
        { valueCoding: { system: "http://example.org/levels", code: "high", display: "High" } },
      ],
    },
    { linkId: "notes", text: "Notes", type: "text" },
  ],
};

describe("buildAnswers", () => {
  it("types each answer by the item type and omits empty optionals", () => {
    const answers = buildAnswers(FORM, {
      name: "Synthetic",
      count: "3",
      ratio: "1.5",
      agree: true,
      when: "2026-10-16",
      level: "high",
      notes: "",
    });
    expect(answers).toEqual([
      { linkId: "name", answer: [{ valueString: "Synthetic" }] },
      { linkId: "count", answer: [{ valueInteger: 3 }] },
      { linkId: "ratio", answer: [{ valueDecimal: 1.5 }] },
      { linkId: "agree", answer: [{ valueBoolean: true }] },
      { linkId: "when", answer: [{ valueDate: "2026-10-16" }] },
      {
        linkId: "level",
        answer: [
          {
            valueCoding: {
              system: "http://example.org/levels",
              code: "high",
              display: "High",
            },
          },
        ],
      },
    ]);
  });

  it("initialises booleans to false and everything else blank", () => {
    const values = initialFormValues(FORM);
    expect(values.agree).toBe(false);
    expect(values.name).toBe("");
  });

  it("nests group children under the group linkId", () => {
    const grouped: FormDefinition = {
      item: [
        {
          linkId: "group-1",
          text: "Group",
          type: "group",
          item: [{ linkId: "inner", text: "Inner", type: "string" }],
        },
      ],
    };
    expect(buildAnswers(grouped, { inner: "x" })).toEqual([
      { linkId: "group-1", item: [{ linkId: "inner", answer: [{ valueString: "x" }] }] },
    ]);
    expect(buildAnswers(grouped, { inner: "" })).toEqual([]);
  });
});

describe("FormRenderer", () => {
  it("renders a published form and submits typed answers", async () => {
    const onSubmit = vi.fn(async (_answers: unknown) => {});
    render(<FormRenderer form={FORM} submitLabel="Save form" onSubmit={onSubmit} />);

    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: "Synthetic" } });
    fireEvent.change(screen.getByLabelText(/Count/), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText(/Level/), { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: "Save form" }));

    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const answers = onSubmit.mock.calls[0][0];
    expect(answers).toContainEqual({ linkId: "name", answer: [{ valueString: "Synthetic" }] });
    expect(answers).toContainEqual({ linkId: "count", answer: [{ valueInteger: 2 }] });
    expect(answers).toContainEqual({
      linkId: "level",
      answer: [{ valueCoding: { system: "http://example.org/levels", code: "low", display: "Low" } }],
    });
  });

  it("surfaces a server validation error instead of pretending success", async () => {
    const onSubmit = vi.fn(async () => {
      throw new Error("QuestionnaireResponse is missing answers for required items: name");
    });
    render(<FormRenderer form={FORM} onSubmit={onSubmit} />);
    // Fill the one required field so the browser-level gate passes and
    // the (simulated) server-side refusal is what surfaces.
    fireEvent.change(screen.getByLabelText(/Name/), { target: { value: "Synthetic" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toMatch(/missing answers/);
  });
});
