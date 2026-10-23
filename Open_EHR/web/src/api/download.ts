import type { DownloadedFile } from "./types";

/** Save a server-downloaded file (report PDF/CSV, printable document,
 *  dataset export) through a temporary object URL. The server has
 *  already audited the generation; this only hands the bytes to the
 *  browser's download/print path. */
export function saveDownloadedFile(file: DownloadedFile): void {
  const url = URL.createObjectURL(file.blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = file.filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
