/**
 * Pure data-shaping for FHIR R4 resources: Bundle unpacking, patient
 * summary composition, and lab-trend series + sparklines. No I/O, no DOM —
 * everything here is unit-tested with synthetic fixtures shaped like the
 * resources public SMART sandboxes return.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */
export type FhirResource = Record<string, any>;

export interface TrendPoint {
  date: string;
  value: number;
}

export interface TrendSeries {
  /** LOINC (or other) code the series is grouped by. */
  code: string;
  display: string;
  unit: string;
  points: TrendPoint[];
}

export interface PatientSummary {
  id: string;
  name: string;
  gender: string;
  birthDate: string;
  conditions: { code: string; display: string }[];
  medications: { display: string; status: string }[];
}

/** Unpack a Bundle (or a single resource, or an array) into resources. */
export function bundleResources(input: unknown): FhirResource[] {
  if (Array.isArray(input)) return input as FhirResource[];
  if (input && typeof input === "object") {
    const obj = input as FhirResource;
    if (obj.resourceType === "Bundle" && Array.isArray(obj.entry)) {
      return obj.entry
        .map((e: FhirResource) => e?.resource)
        .filter((r: unknown): r is FhirResource => !!r && typeof r === "object");
    }
    if (typeof obj.resourceType === "string") return [obj];
  }
  return [];
}

export function firstCoding(codeable: FhirResource | undefined): {
  system: string;
  code: string;
  display: string;
} {
  const coding = codeable?.coding?.[0] ?? {};
  return {
    system: String(coding.system ?? ""),
    code: String(coding.code ?? ""),
    display: String(coding.display ?? codeable?.text ?? coding.code ?? "Unknown"),
  };
}

export function patientDisplayName(patient: FhirResource | undefined): string {
  const name = patient?.name?.[0];
  if (!name) return "Unknown patient";
  const given = Array.isArray(name.given) ? name.given.join(" ") : "";
  return [given, name.family ?? ""].filter(Boolean).join(" ") || name.text || "Unknown patient";
}

export function summarize(
  patient: FhirResource,
  conditions: FhirResource[],
  medications: FhirResource[],
): PatientSummary {
  return {
    id: String(patient?.id ?? ""),
    name: patientDisplayName(patient),
    gender: String(patient?.gender ?? ""),
    birthDate: String(patient?.birthDate ?? ""),
    conditions: conditions.map((c) => {
      const coding = firstCoding(c.code);
      return { code: coding.code, display: coding.display };
    }),
    medications: medications.map((m) => ({
      display: firstCoding(m.medicationCodeableConcept).display,
      status: String(m.status ?? ""),
    })),
  };
}

/**
 * Group laboratory Observations into one trend series per code, points
 * sorted by effective date. Non-numeric or dateless observations are
 * skipped (they cannot be charted honestly).
 */
export function groupLabTrends(observations: FhirResource[]): TrendSeries[] {
  const byCode = new Map<string, TrendSeries>();
  for (const obs of observations) {
    const value = obs?.valueQuantity?.value;
    const date = obs?.effectiveDateTime ?? obs?.issued;
    if (typeof value !== "number" || typeof date !== "string") continue;
    const coding = firstCoding(obs.code);
    if (!coding.code) continue;
    let series = byCode.get(coding.code);
    if (!series) {
      series = {
        code: coding.code,
        display: coding.display,
        unit: String(obs.valueQuantity?.unit ?? ""),
        points: [],
      };
      byCode.set(coding.code, series);
    }
    series.points.push({ date, value });
  }
  const series = [...byCode.values()];
  for (const s of series) s.points.sort((a, b) => a.date.localeCompare(b.date));
  series.sort((a, b) => a.display.localeCompare(b.display));
  return series;
}

/** Polyline `points` attribute for a trend series (SVG coordinate space). */
export function sparklinePoints(
  points: TrendPoint[],
  width = 140,
  height = 36,
  pad = 4,
): string {
  if (points.length === 0) return "";
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  const innerW = width - pad * 2;
  const innerH = height - pad * 2;
  return points
    .map((p, i) => {
      const x = points.length === 1 ? width / 2 : pad + (i / (points.length - 1)) * innerW;
      const y =
        span === 0 ? height / 2 : pad + innerH - ((p.value - min) / span) * innerH;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(" ");
}

function escapeXml(text: string): string {
  return text
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

/** A small inline SVG sparkline for one lab series (original chart code). */
export function sparklineSvg(series: TrendSeries, width = 140, height = 36): string {
  const pts = sparklinePoints(series.points, width, height);
  const latest = series.points[series.points.length - 1];
  const circles = series.points
    .map((p, i) => {
      const [x, y] = pts.split(" ")[i].split(",");
      return `<circle cx="${x}" cy="${y}" r="2"><title>${escapeXml(
        p.date.slice(0, 10),
      )}: ${p.value} ${escapeXml(series.unit)}</title></circle>`;
    })
    .join("");
  return (
    `<svg class="spark" width="${width}" height="${height}" role="img" ` +
    `aria-label="${escapeXml(series.display)} trend, latest ${latest?.value ?? ""} ` +
    `${escapeXml(series.unit)}">` +
    `<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="${pts}"/>` +
    `${circles}</svg>`
  );
}
