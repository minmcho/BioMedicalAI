// Rich-text sanitisation + plain-text rendering — the web mirror of
// backend/app/richtext.py (Home & Module UX pass). The SERVER is
// authoritative; this mirror exists so the editor/view never emit or
// render markup the server would strip (defense-in-depth, and so the
// UI shows exactly what will be stored).
//
// Contract (identical to app/richtext.py):
// - Whitelist tags: p br h2 h3 strong b em i u ul ol li blockquote
//   code a.
// - Only attribute: a[href] whose scheme is http, https or mailto
//   (tab/CR/LF removed before the scheme check; relative,
//   protocol-relative, javascript:, data: hrefs are dropped — the
//   <a> tag itself is kept).
// - Every other attribute is stripped.
// - script/style content is dropped entirely. Per the WS2b web
//   contract, iframe/object/embed are also dropped WITH their
//   content (the backend strips those tags keeping text; the web
//   mirror is stricter here because embedded content has no prose
//   meaning in a note).
// - Every other non-whitelisted tag is stripped with its text kept.
// - Output is balanced (the DOM parser balances the tree) and
//   idempotent: sanitizeHtml(sanitizeHtml(x)) === sanitizeHtml(x).

const ALLOWED_TAGS = new Set([
  "p", "br", "h2", "h3", "strong", "b", "em", "i", "u",
  "ul", "ol", "li", "blockquote", "code", "a",
]);

const DROP_CONTENT_TAGS = new Set(["script", "style", "iframe", "object", "embed"]);

const ALLOWED_HREF_SCHEMES = new Set(["http", "https", "mailto"]);

const SCHEME_RE = /^([A-Za-z][A-Za-z0-9+.-]*):/;

/** Mirrors app.richtext._clean_href: return the href to keep, or
 *  null when it must be dropped. */
export function cleanHref(raw: unknown): string | null {
  if (typeof raw !== "string") return null;
  const value = raw.trim().replace(/[\t\r\n]/g, "");
  if (!value) return null;
  const match = SCHEME_RE.exec(value);
  if (!match) return null;
  if (!ALLOWED_HREF_SCHEMES.has(match[1].toLowerCase())) return null;
  return value;
}

function escapeText(text: string): string {
  // Mirrors html.escape(data, quote=False): &, <, > only.
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function escapeAttr(value: string): string {
  // Mirrors html.escape(href, quote=True).
  return escapeText(value).replace(/"/g, "&quot;").replace(/'/g, "&#x27;");
}

function serializeChildren(node: Node): string {
  let out = "";
  node.childNodes.forEach((child) => {
    out += serializeNode(child);
  });
  return out;
}

function serializeNode(node: Node): string {
  if (node.nodeType === 3) {
    // Text node — escaped exactly as the backend escapes character data.
    return escapeText(node.textContent ?? "");
  }
  if (node.nodeType !== 1) {
    // Comments, doctypes, processing instructions: dropped.
    return "";
  }
  const el = node as Element;
  const tag = el.tagName.toLowerCase();
  if (DROP_CONTENT_TAGS.has(tag)) return "";
  if (!ALLOWED_TAGS.has(tag)) {
    // Tag stripped; its text/children still arrive.
    return serializeChildren(el);
  }
  if (tag === "br") return "<br>";
  if (tag === "a") {
    const href = cleanHref(el.getAttribute("href"));
    const inner = serializeChildren(el);
    return href !== null
      ? `<a href="${escapeAttr(href)}">${inner}</a>`
      : `<a>${inner}</a>`;
  }
  return `<${tag}>${serializeChildren(el)}</${tag}>`;
}

/** Sanitize `html` against the whitelist. Non-string / empty input
 *  returns "". Output is balanced and idempotent. */
export function sanitizeHtml(html: unknown): string {
  if (typeof html !== "string" || !html) return "";
  const doc = new DOMParser().parseFromString(html, "text/html");
  return serializeChildren(doc.body);
}

// ---------------------------------------------------------------------------
// Plain-text rendering (mirrors app.richtext.html_to_text): block
// elements separated by blank lines, list items prefixed "• ",
// <br> -> newline, links rendered as "text (href)" only when the href
// differs from the link text, headings as their text on their own
// line, script/style content dropped.
// ---------------------------------------------------------------------------

const BLOCK_TAGS = new Set([
  "p", "h2", "h3", "h1", "h4", "h5", "h6", "blockquote", "ul", "ol",
  "div", "section", "article", "header", "footer", "pre", "table",
  "tr", "hr",
]);

class TextRenderer {
  blocks: string[] = [];
  lines: string[] = [];
  cur: string[] = [];

  flushLine(): void {
    const line = this.cur.join("").replace(/\s+$/, "");
    this.cur = [];
    if (line.trim()) this.lines.push(line);
  }

  flushBlock(): void {
    this.flushLine();
    if (this.lines.length > 0) {
      this.blocks.push(this.lines.join("\n"));
      this.lines = [];
    }
  }

  renderChildren(node: Node): void {
    node.childNodes.forEach((child) => this.renderNode(child));
  }

  renderNode(node: Node): void {
    if (node.nodeType === 3) {
      this.cur.push((node.textContent ?? "").replace(/\s+/g, " "));
      return;
    }
    if (node.nodeType !== 1) return;
    const el = node as Element;
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style") return;
    if (tag === "br") {
      this.flushLine();
      return;
    }
    if (tag === "li") {
      this.flushLine();
      this.cur.push("• ");
      this.renderChildren(el);
      this.flushLine();
      return;
    }
    if (tag === "a") {
      const href = cleanHref(el.getAttribute("href"));
      const outer = this.cur;
      this.cur = [];
      this.renderChildren(el);
      const text = this.cur.join("").trim();
      this.cur = outer;
      if (href && text && href !== text) this.cur.push(`${text} (${href})`);
      else if (text) this.cur.push(text);
      else if (href) this.cur.push(href);
      return;
    }
    if (BLOCK_TAGS.has(tag)) {
      this.flushBlock();
      this.renderChildren(el);
      this.flushBlock();
      return;
    }
    this.renderChildren(el);
  }

  result(): string {
    this.flushBlock();
    return this.blocks
      .map((b) => b.trim())
      .filter((b) => b)
      .join("\n\n");
  }
}

/** Plain-text rendering of rich HTML (previews / print-ish surfaces,
 *  and the emptiness checks prose forms need: an editor holding only
 *  markup has no text). Non-string / empty input returns "". */
export function htmlToText(html: unknown): string {
  if (typeof html !== "string" || !html) return "";
  if (!html.includes("<")) return html.trim();
  const doc = new DOMParser().parseFromString(html, "text/html");
  const renderer = new TextRenderer();
  renderer.renderChildren(doc.body);
  return renderer.result();
}
