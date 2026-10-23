import { describe, expect, it } from "vitest";
import { cleanHref, htmlToText, sanitizeHtml } from "./richtext";

// The web mirror of backend/app/richtext.py: the same whitelist, the
// same href rules, the same drop/strip behaviour. These cases mirror
// the backend's tests/test_richtext.py expectations so the two
// implementations cannot drift silently.

describe("sanitizeHtml", () => {
  it("keeps whitelisted formatting intact", () => {
    const html =
      "<p>Seen <strong>today</strong>, <em>stable</em>.</p><h2>Plan</h2>" +
      "<ul><li>One</li><li>Two</li></ul><blockquote>Note</blockquote>";
    expect(sanitizeHtml(html)).toBe(html);
  });

  it("drops script and style content entirely", () => {
    expect(
      sanitizeHtml("<p>a</p><script>alert(1)</script><style>.x{}</style><p>b</p>"),
    ).toBe("<p>a</p><p>b</p>");
  });

  it("drops iframe/object/embed with their content", () => {
    expect(
      sanitizeHtml('<iframe><p>inner</p></iframe><object><p>obj</p></object><p>ok</p>'),
    ).toBe("<p>ok</p>");
  });

  it("strips non-whitelisted tags but keeps their text", () => {
    expect(sanitizeHtml("<div><span>hi</span></div>")).toBe("hi");
    expect(sanitizeHtml("<h1>Title</h1><table><tr><td>cell</td></tr></table>")).toBe(
      "Titlecell",
    );
  });

  it("drops a javascript: href but keeps the <a> tag", () => {
    expect(sanitizeHtml('<a href="javascript:alert(1)">x</a>')).toBe("<a>x</a>");
    expect(sanitizeHtml('<a href="jav&#x09;ascript:alert(1)">x</a>')).toBe("<a>x</a>");
    expect(sanitizeHtml('<a href="/relative/path">x</a>')).toBe("<a>x</a>");
    expect(sanitizeHtml('<a href="data:text/html;base64,AAAA">x</a>')).toBe("<a>x</a>");
  });

  it("keeps http/https/mailto hrefs", () => {
    expect(sanitizeHtml('<a href="https://example.org/x">x</a>')).toBe(
      '<a href="https://example.org/x">x</a>',
    );
    expect(sanitizeHtml('<a href="http://example.org">x</a>')).toBe(
      '<a href="http://example.org">x</a>',
    );
    expect(sanitizeHtml('<a href="mailto:team@example.org">mail</a>')).toBe(
      '<a href="mailto:team@example.org">mail</a>',
    );
  });

  it("strips every other attribute", () => {
    expect(sanitizeHtml('<p class="x" onclick="evil()" style="color:red">t</p>')).toBe(
      "<p>t</p>",
    );
    expect(
      sanitizeHtml('<a href="https://example.org" target="_blank" rel="noreferrer">y</a>'),
    ).toBe('<a href="https://example.org">y</a>');
  });

  it("escapes character data instead of letting it become markup", () => {
    expect(sanitizeHtml("1 < 2 & 3 > 2")).toBe("1 &lt; 2 &amp; 3 &gt; 2");
  });

  it("is idempotent", () => {
    const samples = [
      "<p>Hi <strong>there</strong></p><script>x</script>",
      '<a href="javascript:x">bad</a><a href="https://ok.example">good</a>',
      "<div>plain <b>bold</b></div><ul><li>a</li></ul>",
      "bare text & symbols < >",
      "<p>one<p>two",
    ];
    for (const sample of samples) {
      expect(sanitizeHtml(sanitizeHtml(sample))).toBe(sanitizeHtml(sample));
    }
  });

  it("returns empty string for non-string and empty input", () => {
    expect(sanitizeHtml("")).toBe("");
    expect(sanitizeHtml(null)).toBe("");
    expect(sanitizeHtml(undefined)).toBe("");
    expect(sanitizeHtml(42)).toBe("");
  });
});

describe("cleanHref", () => {
  it("mirrors the backend scheme rules", () => {
    expect(cleanHref("https://example.org")).toBe("https://example.org");
    expect(cleanHref("  mailto:a@example.org  ")).toBe("mailto:a@example.org");
    expect(cleanHref("javascript:alert(1)")).toBeNull();
    expect(cleanHref("//example.org/path")).toBeNull();
    expect(cleanHref("")).toBeNull();
    expect(cleanHref(null)).toBeNull();
  });
});

describe("htmlToText", () => {
  it("separates blocks with blank lines", () => {
    expect(htmlToText("<p>One</p><p>Two</p>")).toBe("One\n\nTwo");
    expect(htmlToText("<h2>Head</h2><p>Body</p>")).toBe("Head\n\nBody");
  });

  it("prefixes list items and honours line breaks", () => {
    expect(htmlToText("<ul><li>a</li><li>b</li></ul>")).toBe("• a\n• b");
    expect(htmlToText("<p>line1<br>line2</p>")).toBe("line1\nline2");
  });

  it("renders a link as text (href) only when they differ", () => {
    expect(htmlToText('<p>See <a href="https://example.org">the guide</a></p>')).toBe(
      "See the guide (https://example.org)",
    );
    expect(htmlToText('<p><a href="https://example.org">https://example.org</a></p>')).toBe(
      "https://example.org",
    );
  });

  it("drops script content and passes plain text through", () => {
    expect(htmlToText("<p>a</p><script>evil()</script>")).toBe("a");
    expect(htmlToText("just text")).toBe("just text");
    expect(htmlToText("")).toBe("");
    expect(htmlToText(null)).toBe("");
  });
});
