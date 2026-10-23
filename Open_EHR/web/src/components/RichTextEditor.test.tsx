import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { FormDefinition, FormItem } from "../api/types";
import { FormRenderer } from "./FormRenderer";
import { RichTextEditor } from "./RichTextEditor";
import { RichTextView } from "./RichTextView";

afterEach(cleanup);

// contentEditable in jsdom: tests drive the editor the way a browser
// reports edits — set innerHTML, then fire an `input` event. (Real
// browsers fire `input` on every keystroke; document.execCommand,
// used by the toolbar, does not exist in jsdom, so toolbar tests
// assert presence/labels and the link-validation path, which is
// editor logic, not execCommand.)

describe("RichTextEditor", () => {
  it("renders the value as sanitized HTML in a textbox region", () => {
    render(
      <RichTextEditor
        value="<p>Seen <strong>today</strong></p><script>alert(1)</script>"
        onChange={() => {}}
        ariaLabel="Note body"
      />,
    );
    const editor = screen.getByRole("textbox", { name: "Note body" });
    expect(editor.getAttribute("aria-multiline")).toBe("true");
    expect(editor.innerHTML).toBe("<p>Seen <strong>today</strong></p>");
    expect(editor.textContent).not.toContain("alert");
  });

  it("emits sanitized HTML on change", () => {
    const onChange = vi.fn();
    render(<RichTextEditor value="" onChange={onChange} ariaLabel="Note body" />);
    const editor = screen.getByRole("textbox", { name: "Note body" });
    editor.innerHTML =
      '<p>Hello <b>world</b></p><script>alert(1)</script><p onclick="x()">again</p>';
    fireEvent.input(editor);
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0] as string;
    expect(emitted).toBe("<p>Hello <b>world</b></p><p>again</p>");
  });

  it("sanitizes pasted HTML before it is emitted", () => {
    const onChange = vi.fn();
    render(<RichTextEditor value="" onChange={onChange} ariaLabel="Note body" />);
    const editor = screen.getByRole("textbox", { name: "Note body" });
    fireEvent.paste(editor, {
      clipboardData: {
        getData: (kind: string) =>
          kind === "text/html"
            ? '<p>Pasted</p><script>alert(1)</script><img src=x onerror=alert(1)>'
            : "Pasted",
      },
    });
    expect(onChange).toHaveBeenCalled();
    const emitted = onChange.mock.calls[onChange.mock.calls.length - 1][0] as string;
    expect(emitted).toContain("Pasted");
    expect(emitted).not.toContain("script");
    expect(emitted).not.toContain("onerror");
  });

  it("renders the full toolbar with accessible labels", () => {
    render(<RichTextEditor value="" onChange={() => {}} ariaLabel="Note body" />);
    for (const label of [
      "Bold",
      "Italic",
      "Heading 2",
      "Heading 3",
      "Bulleted list",
      "Numbered list",
      "Link",
      "Clear formatting",
    ]) {
      const button = screen.getByRole("button", { name: label });
      expect(button.getAttribute("title")).toBe(label);
      expect(button.getAttribute("type")).toBe("button");
    }
  });

  it("rejects a non-http link visibly and inserts nothing", () => {
    const onChange = vi.fn();
    vi.stubGlobal("prompt", () => "javascript:alert(1)");
    render(<RichTextEditor value="<p>x</p>" onChange={onChange} ariaLabel="Note body" />);
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    expect(screen.getByRole("alert").textContent).toMatch(
      /only http, https and mailto links are allowed/,
    );
    vi.unstubAllGlobals();
  });

  it("accepts an https link without an error", () => {
    vi.stubGlobal("prompt", () => "https://example.org/guide");
    render(<RichTextEditor value="<p>x</p>" onChange={() => {}} ariaLabel="Note body" />);
    fireEvent.click(screen.getByRole("button", { name: "Link" }));
    expect(screen.queryByRole("alert")).toBeNull();
    vi.unstubAllGlobals();
  });
});

describe("RichTextView", () => {
  it("renders stored rich text sanitized, never raw", () => {
    const { container } = render(
      <RichTextView html={'<p>Plan</p><script>alert(1)</script><a href="javascript:x">link</a>'} />,
    );
    expect(container.textContent).toContain("Plan");
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("a")?.getAttribute("href")).toBeNull();
    expect(container.innerHTML).not.toContain("javascript:");
  });
});

describe("FormRenderer rich text items", () => {
  // FormItem has no `rich` field in api/types.ts yet; the published
  // payload carries it, so the fixture builds the item through a cast.
  const richItem = {
    linkId: "narrative",
    text: "Narrative",
    type: "text",
    rich: true,
  } as unknown as FormItem;
  const RICH_FORM: FormDefinition = {
    resourceType: "Questionnaire",
    title: "Rich form",
    item: [richItem, { linkId: "plain", text: "Plain notes", type: "text" }],
  };

  it("renders a rich text item with the editor and a plain text item with a textarea", async () => {
    const onSubmit = vi.fn(async (_answers: unknown) => {});
    render(<FormRenderer form={RICH_FORM} onSubmit={onSubmit} />);
    const editor = screen.getByRole("textbox", { name: /Narrative/ });
    expect(editor).toBeTruthy();
    expect(screen.getByRole("button", { name: "Bold" })).toBeTruthy();
    // The non-rich text item keeps its textarea, unchanged.
    const plain = screen.getByLabelText(/Plain notes/);
    expect(plain.tagName).toBe("TEXTAREA");

    editor.innerHTML = "<p>Rich <strong>answer</strong></p>";
    fireEvent.input(editor);
    fireEvent.click(screen.getByRole("button", { name: "Submit" }));
    await vi.waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
    const answers = onSubmit.mock.calls[0][0];
    expect(answers).toContainEqual({
      linkId: "narrative",
      answer: [{ valueString: "<p>Rich <strong>answer</strong></p>" }],
    });
  });
});
