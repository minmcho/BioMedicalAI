"""Rich-text sanitisation + plain-text rendering (Home & Module UX pass).

Server-authoritative rich text for prose fields (clinical notes,
messages, discharge summaries). Stdlib only — built on
``html.parser``; no new dependencies.

Contract (see uxpass_work/PROGRESS.md, RICH TEXT):

- Whitelist tags: ``p br h2 h3 strong b em i u ul ol li blockquote
  code a``.
- Only attribute: ``a[href]`` whose scheme is http, https or mailto.
  Any other href (relative, protocol-relative, ``javascript:``,
  ``data:``, ...) is dropped; the ``<a>`` tag itself is kept.
- Every other tag is stripped with its text content preserved —
  EXCEPT ``script``/``style``, whose content is dropped entirely.
- Every other attribute is stripped.
- :func:`sanitize_html` output is well-formed (balanced tags) and
  idempotent: ``sanitize_html(sanitize_html(x)) == sanitize_html(x)``.
- :func:`html_to_text` renders the plain-text form used for FHIR
  narrative / print surfaces, where author markup must not ride.
"""
import html as _html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = frozenset({
    "p", "br", "h2", "h3", "strong", "b", "em", "i", "u",
    "ul", "ol", "li", "blockquote", "code", "a",
})
VOID_TAGS = frozenset({"br"})
DROP_CONTENT_TAGS = frozenset({"script", "style"})
ALLOWED_HREF_SCHEMES = frozenset({"http", "https", "mailto"})

_SCHEME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9+.\-]*):")
_TAG_RE = re.compile(
    r"<\s*/?\s*(p|br|h2|h3|strong|b|em|i|u|ul|ol|li|blockquote|code|a)\b",
    re.IGNORECASE)
_WS_RE = re.compile(r"\s+")


def _clean_href(raw) -> str | None:
    """Return the href to keep, or None when it must be dropped.

    Only absolute URLs with an http/https/mailto scheme survive.
    Tab/CR/LF are removed before the scheme check (browsers strip
    them too, so ``jav&#x09;ascript:`` must not slip past)."""
    if not isinstance(raw, str):
        return None
    value = raw.strip().replace("\t", "").replace("\r", "").replace("\n", "")
    if not value:
        return None
    match = _SCHEME_RE.match(value)
    if match is None:
        return None
    if match.group(1).lower() not in ALLOWED_HREF_SCHEMES:
        return None
    return value


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._stack: list[str] = []
        self._drop_depth = 0

    # -- helpers ------------------------------------------------------
    def _close_through(self, tag: str) -> None:
        """Close every open tag up to and including ``tag``."""
        while self._stack:
            open_tag = self._stack.pop()
            self._out.append(f"</{open_tag}>")
            if open_tag == tag:
                break

    # -- parser events --------------------------------------------------
    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag not in ALLOWED_TAGS:
            return  # tag stripped; its text still arrives via handle_data
        if tag in VOID_TAGS:
            self._out.append("<br>")
            return
        if tag == "a":
            href = None
            for name, value in attrs:
                if name == "href":
                    href = _clean_href(value)
                    break
            if href is not None:
                self._out.append(
                    f'<a href="{_html.escape(href, quote=True)}">')
            else:
                self._out.append("<a>")
            self._stack.append("a")
            return
        # A new block item implicitly closes a sibling left open
        # (e.g. "<li>one<li>two" or "<p>a<p>b").
        if tag in ("li", "p") and self._stack and self._stack[-1] == tag:
            self._out.append(f"</{tag}>")
            self._stack.pop()
        self._out.append(f"<{tag}>")
        self._stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        # Treat "<x/>" as start+end so void/self-closed forms normalise.
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self._stack:
            self._close_through(tag)
        # Stray end tags are dropped.

    def handle_data(self, data):
        if self._drop_depth:
            return
        self._out.append(_html.escape(data, quote=False))

    # Comments, doctypes, processing instructions: dropped (the
    # HTMLParser default handlers already ignore them).

    def result(self) -> str:
        while self._stack:
            self._out.append(f"</{self._stack.pop()}>")
        return "".join(self._out)


def sanitize_html(html) -> str:
    """Sanitize ``html`` against the whitelist. Non-string / None
    input returns ``""``. Output is balanced and idempotent."""
    if not isinstance(html, str):
        return ""
    if not html:
        return ""
    parser = _Sanitizer()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - html.parser is forgiving;
        # on any surprise, fall back to fully-escaped plain text.
        return _html.escape(html, quote=False)
    return parser.result()


# ---------------------------------------------------------------------------
# Plain-text rendering (FHIR narrative / print)
# ---------------------------------------------------------------------------

_BLOCK_TAGS = frozenset({
    "p", "h2", "h3", "h1", "h4", "h5", "h6", "blockquote", "ul", "ol",
    "div", "section", "article", "header", "footer", "pre", "table",
    "tr", "hr",
})


class _TextRenderer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._blocks: list[str] = []
        self._lines: list[str] = []
        self._cur: list[str] = []
        self._drop_depth = 0
        self._link_stack: list[tuple[str | None, list[str]]] = []

    def _flush_line(self) -> None:
        line = "".join(self._cur).rstrip()
        self._cur = []
        if line.strip():
            self._lines.append(line)

    def _flush_block(self) -> None:
        self._flush_line()
        if self._lines:
            self._blocks.append("\n".join(self._lines))
            self._lines = []

    def handle_starttag(self, tag, attrs):
        if tag in DROP_CONTENT_TAGS:
            self._drop_depth += 1
            return
        if self._drop_depth:
            return
        if tag == "br":
            self._flush_line()
            return
        if tag == "li":
            self._flush_line()
            self._cur.append("• ")
            return
        if tag == "a":
            href = None
            for name, value in attrs:
                if name == "href":
                    href = _clean_href(value)
                    break
            self._link_stack.append((href, self._cur))
            self._cur = []
            return
        if tag in _BLOCK_TAGS:
            self._flush_block()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag in DROP_CONTENT_TAGS:
            if self._drop_depth:
                self._drop_depth -= 1
            return
        if self._drop_depth:
            return
        if tag == "a":
            if not self._link_stack:
                return
            href, outer = self._link_stack.pop()
            text = "".join(self._cur).strip()
            self._cur = outer
            if href and text and href != text:
                self._cur.append(f"{text} ({href})")
            elif text:
                self._cur.append(text)
            elif href:
                self._cur.append(href)
            return
        if tag == "li":
            self._flush_line()
            return
        if tag in _BLOCK_TAGS:
            self._flush_block()

    def handle_data(self, data):
        if self._drop_depth:
            return
        self._cur.append(_WS_RE.sub(" ", data))

    def result(self) -> str:
        self._flush_block()
        return "\n\n".join(block.strip() for block in self._blocks
                           if block.strip())


def html_to_text(html) -> str:
    """Plain-text rendering: block elements separated by blank
    lines, list items prefixed ``"• "``, links rendered as
    ``text (href)`` only when the href differs from the link text,
    headings as their text on their own line. Non-string / None
    input returns ``""``."""
    if not isinstance(html, str):
        return ""
    if not html:
        return ""
    if "<" not in html:
        return html.strip()
    parser = _TextRenderer()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # pragma: no cover - forgiving parser
        return html.strip()
    return parser.result()


def is_rich_html(html) -> bool:
    """True when the value carries any whitelisted formatting tag."""
    if not isinstance(html, str) or not html:
        return False
    return _TAG_RE.search(html) is not None


def strip_to_text(html) -> str:
    """All tags removed, text kept (the html_to_text rendering)."""
    return html_to_text(html)
