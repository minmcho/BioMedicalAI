import { useMemo } from "react";
import { sanitizeHtml } from "./richtext";

interface Props {
  /** Stored rich text (HTML string). Always sanitized before render —
   *  unsanitized content is never rendered. */
  html: string | null | undefined;
  className?: string;
}

/** Read-only rendering of stored rich text: sanitizeHtml (the web
 *  mirror of the server whitelist) then dangerouslySetInnerHTML.
 *  Plain-text values render as plain text — sanitizeHtml escapes
 *  character data and adds no markup of its own. */
export function RichTextView({ html, className }: Props) {
  const clean = useMemo(() => sanitizeHtml(html ?? ""), [html]);
  return (
    <div
      className={className ?? "rich-text-view"}
      dangerouslySetInnerHTML={{ __html: clean }}
    />
  );
}
