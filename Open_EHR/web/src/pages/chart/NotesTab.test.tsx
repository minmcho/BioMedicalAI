import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ApiClient } from "../../api/client";
import { NotesTab } from "./NotesTab";

afterEach(cleanup);

// WS2b converted-surface proof: the note free-text and amend fields
// are rich-text editors (role=textbox + toolbar), not textareas, and
// the HTML they emit is what createNote receives. Stored note text
// renders through RichTextView (sanitized HTML, never raw tags as
// text, never a live script element).

function stubClient(notes: unknown[] = []) {
  return {
    noteTypes: vi.fn(async () => ({
      note_types: {
        concepts: [
          { code: "progress", display: "Progress note", templateForm: "note-progress", active: true },
        ],
      },
    })),
    notes: vi.fn(async () => ({ items: notes, total: notes.length })),
    // No published template form in this stub: NotesTab falls back to
    // the free-text-only create path.
    effectiveConfig: vi.fn(async () => {
      throw new Error("no form");
    }),
    createNote: vi.fn(async () => ({})),
    signNote: vi.fn(async () => ({})),
    amendNote: vi.fn(async () => ({})),
  } as unknown as ApiClient & Record<string, ReturnType<typeof vi.fn>>;
}

describe("NotesTab rich text (WS2b)", () => {
  it("authors a note through the rich editor and sends its HTML", async () => {
    const client = stubClient();
    render(<NotesTab client={client} patientId="pat-1" canWrite />);
    const editor = await screen.findByRole("textbox", { name: "Free text (optional)" });
    expect(screen.getByRole("button", { name: "Bold" })).toBeTruthy();
    expect(document.querySelector("textarea")).toBeNull();

    editor.innerHTML = "<p>Seen <strong>today</strong></p>";
    fireEvent.input(editor);
    fireEvent.click(screen.getByRole("button", { name: "Create note" }));
    await vi.waitFor(() => expect(client.createNote).toHaveBeenCalledTimes(1));
    const arg = (client.createNote as ReturnType<typeof vi.fn>).mock.calls[0][0];
    expect(arg.text).toBe("<p>Seen <strong>today</strong></p>");
    expect(arg.noteType).toBe("progress");
  });

  it("renders stored note text as sanitized HTML and amends via the rich editor", async () => {
    const note = {
      resourceType: "DocumentReference",
      id: "note-9",
      status: "current",
      docStatus: "final",
      type: { text: "Progress note" },
      date: "2026-10-16T09:00:00Z",
      noteType: "progress",
      noteContent: {
        templateForm: null,
        questionnaireResponseId: null,
        sections: [],
        text: "<p>Original <em>note</em></p><script>alert(1)</script>",
      },
    };
    const client = stubClient([note]);
    const { container } = render(<NotesTab client={client} patientId="pat-1" canWrite />);
    expect(await screen.findByText(/Original/)).toBeTruthy();
    // RichTextView rendered the <em> as markup and dropped the script.
    expect(container.querySelector("em")?.textContent).toBe("note");
    expect(container.querySelector("script")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Amend" }));
    const amendEditor = await screen.findByRole("textbox", { name: "Amended text" });
    amendEditor.innerHTML = "<p>Corrected course</p>";
    fireEvent.input(amendEditor);
    fireEvent.click(screen.getByRole("button", { name: "Save amendment" }));
    await vi.waitFor(() => expect(client.amendNote).toHaveBeenCalledTimes(1));
    expect((client.amendNote as ReturnType<typeof vi.fn>).mock.calls[0]).toEqual([
      "note-9",
      { text: "<p>Corrected course</p>" },
    ]);
  });
});
