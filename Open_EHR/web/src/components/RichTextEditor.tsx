import { Fragment, useEffect, useRef, useState } from "react";
import { cleanHref, sanitizeHtml } from "./richtext";

interface Props {
  /** The current value, an HTML string (sanitized on the way in and
   *  on every emission — the server remains authoritative). */
  value: string;
  onChange: (html: string) => void;
  ariaLabel: string;
  disabled?: boolean;
  minRows?: number;
  id?: string;
}

/** Run a formatting command when the host supports it. jsdom (the
 *  unit-test DOM) has no execCommand: the toolbar is still rendered
 *  and clickable there, the command itself is a no-op — exactly the
 *  honest behaviour for an environment that cannot edit rich text. */
function exec(command: string, value?: string): void {
  try {
    if (typeof document !== "undefined" && typeof document.execCommand === "function") {
      document.execCommand(command, false, value);
    }
  } catch {
    // A host that refuses the command leaves the document unchanged;
    // the emission below still reports the current (sanitized) HTML.
  }
}

const TOOLBAR: Array<{ label: string; command: string; arg?: string }> = [
  { label: "Bold", command: "bold" },
  { label: "Italic", command: "italic" },
  { label: "Heading 2", command: "formatBlock", arg: "H2" },
  { label: "Heading 3", command: "formatBlock", arg: "H3" },
  { label: "Bulleted list", command: "insertUnorderedList" },
  { label: "Numbered list", command: "insertOrderedList" },
];

/** Toolbar groups (Rich Design Elevation §8.15): text style /
 *  headings / lists, rendered with separators between groups. The
 *  Link and Clear-formatting buttons form their own groups after
 *  these. */
const TOOLBAR_GROUPS: Array<Array<(typeof TOOLBAR)[number]>> = [
  TOOLBAR.slice(0, 2),
  TOOLBAR.slice(2, 4),
  TOOLBAR.slice(4, 6),
];

/** contentEditable rich-text editor for prose fields (Home & Module
 *  UX pass). Emits sanitized HTML only: every change/blur emission
 *  and every paste passes through sanitizeHtml (the web mirror of the
 *  server whitelist in app/richtext.py). */
export function RichTextEditor({
  value,
  onChange,
  ariaLabel,
  disabled = false,
  minRows = 4,
  id,
}: Props) {
  const editorRef = useRef<HTMLDivElement | null>(null);
  const lastEmitted = useRef<string | null>(null);
  const [linkError, setLinkError] = useState<string | null>(null);

  // Sync external value changes into the editable region — but never
  // overwrite what the user is typing: when the incoming value is the
  // one we just emitted (or already matches the DOM), leave the DOM
  // (and the caret) alone.
  useEffect(() => {
    const el = editorRef.current;
    if (!el) return;
    if (value === lastEmitted.current) return;
    if (el.innerHTML !== value) {
      el.innerHTML = sanitizeHtml(value);
    }
    lastEmitted.current = value;
  }, [value]);

  const emit = () => {
    const el = editorRef.current;
    if (!el) return;
    const clean = sanitizeHtml(el.innerHTML);
    lastEmitted.current = clean;
    onChange(clean);
  };

  const insertHtmlAtCursor = (html: string) => {
    const el = editorRef.current;
    if (!el) return;
    el.focus();
    const selection = typeof window !== "undefined" ? window.getSelection() : null;
    const range =
      selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
    if (range && el.contains(range.commonAncestorContainer)) {
      range.deleteContents();
      const template = document.createElement("template");
      template.innerHTML = html;
      const fragment = template.content;
      const lastNode = fragment.lastChild;
      range.insertNode(fragment);
      if (lastNode) {
        const after = document.createRange();
        after.setStartAfter(lastNode);
        after.collapse(true);
        selection?.removeAllRanges();
        selection?.addRange(after);
      }
    } else {
      el.innerHTML += html;
    }
  };

  const handlePaste = (e: React.ClipboardEvent<HTMLDivElement>) => {
    e.preventDefault();
    const html = e.clipboardData.getData("text/html");
    const text = e.clipboardData.getData("text/plain");
    // Paste is sanitized before it ever reaches the document: HTML
    // clipboards go through the whitelist; plain-text clipboards are
    // inserted as text (sanitizeHtml escapes them to inert text).
    const clean = sanitizeHtml(html || text);
    if (clean) insertHtmlAtCursor(clean);
    emit();
  };

  const handleLink = () => {
    let url: string | null = null;
    try {
      url = typeof window !== "undefined" ? window.prompt("Link URL (http, https or mailto):") : null;
    } catch {
      url = null;
    }
    if (url === null) return; // cancelled — nothing changes
    const href = cleanHref(url);
    if (href === null) {
      // Rejected visibly: only http/https/mailto links are allowed.
      setLinkError("Link not inserted: only http, https and mailto links are allowed.");
      return;
    }
    setLinkError(null);
    editorRef.current?.focus();
    exec("createLink", href);
    emit();
  };

  const handleClearFormatting = () => {
    editorRef.current?.focus();
    exec("removeFormat");
    exec("formatBlock", "P");
    emit();
  };

  const renderTool = (tool: (typeof TOOLBAR)[number]) => (
    <button
      key={tool.label}
      type="button"
      aria-label={tool.label}
      title={tool.label}
      disabled={disabled}
      // Keep the editor selection alive across the click.
      onMouseDown={(e) => e.preventDefault()}
      onClick={() => {
        editorRef.current?.focus();
        exec(tool.command, tool.arg);
        emit();
      }}
    >
      {tool.label}
    </button>
  );

  return (
    <div className="rte-root">
      <div className="rte-toolbar" role="toolbar" aria-label={`${ariaLabel} formatting`}>
        {TOOLBAR_GROUPS.map((group, gi) => (
          <Fragment key={gi}>
            {gi > 0 && <span className="rte-sep" aria-hidden="true" />}
            {group.map(renderTool)}
          </Fragment>
        ))}
        <span className="rte-sep" aria-hidden="true" />
        <button
          type="button"
          aria-label="Link"
          title="Link"
          disabled={disabled}
          onMouseDown={(e) => e.preventDefault()}
          onClick={handleLink}
        >
          Link
        </button>
        <span className="rte-sep" aria-hidden="true" />
        <button
          type="button"
          aria-label="Clear formatting"
          title="Clear formatting"
          disabled={disabled}
          onMouseDown={(e) => e.preventDefault()}
          onClick={handleClearFormatting}
        >
          Clear formatting
        </button>
      </div>
      <div
        ref={editorRef}
        id={id}
        className="rte-editable"
        role="textbox"
        aria-multiline="true"
        aria-label={ariaLabel}
        aria-disabled={disabled || undefined}
        contentEditable={!disabled}
        suppressContentEditableWarning
        style={{
          // Static presentation (border/padding/typography) lives
          // in the .rte-* stylesheet; only the dynamic min-height
          // and the disabled background stay inline.
          minHeight: `${Math.max(1, minRows) * 1.5}em`,
          background: disabled ? "var(--surface-soft)" : undefined,
        }}
        onInput={emit}
        onBlur={emit}
        onPaste={handlePaste}
      />
      {linkError && (
        <span className="field-error" role="alert">
          {linkError}
        </span>
      )}
    </div>
  );
}
