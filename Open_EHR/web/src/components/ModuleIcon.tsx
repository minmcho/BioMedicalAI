// Rich Design Elevation pass: the module icon system, extracted
// from ModuleCard.tsx so every surface (Home cards, ModuleHeader,
// empty states, the session-lock card) draws from the same glyph
// map without importing the card. One idiom: 24×24 grid, stroke 1.8,
// round caps/joins, currentColor — the tile/surface supplies the
// group identity colour via CSS `color`. NO emoji, NO icon fonts,
// NO external assets. Unknown names fall back to the grid glyph.

/** Inline SVG paths (24×24, stroke idiom) keyed by the nav item's
 *  icon name. Unknown names fall back to the generic grid glyph. */
export const ICON_PATHS: Record<string, string[]> = {
  users: ["M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2", "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M22 21v-2a4 4 0 0 0-3-3.87", "M16 3.13a4 4 0 0 1 0 7.75"],
  inbox: ["M22 12h-6l-2 3h-4l-2-3H2", "M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11Z"],
  heart: ["M19 14c1.49-1.46 3-3.21 3-5.5A5.5 5.5 0 0 0 16.5 3c-1.76 0-3 .5-4.5 2-1.5-1.5-2.74-2-4.5-2A5.5 5.5 0 0 0 2 8.5c0 2.3 1.5 4.05 3 5.5l7 7Z"],
  calendar: ["M8 2v4", "M16 2v4", "M3 8h18", "M5 4h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Z"],
  receipt: ["M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1Z", "M8 7h8", "M8 11h8", "M8 15h5"],
  siren: ["M7 18v-6a5 5 0 0 1 10 0v6", "M5 18h14", "M12 2v2", "M4.9 4.9l1.4 1.4", "M19.1 4.9l-1.4 1.4", "M3 18h18"],
  scan: ["M3 7V5a2 2 0 0 1 2-2h2", "M17 3h2a2 2 0 0 1 2 2v2", "M21 17v2a2 2 0 0 1-2 2h-2", "M7 21H5a2 2 0 0 1-2-2v-2", "M8 12h8"],
  scalpel: ["M4 20c6 0 12-6 16-16-2 0-4 .5-6 1.5C8 8 4 14 4 20Z", "M4 20l3-3"],
  flask: ["M10 2v6L4.5 18a2 2 0 0 0 1.8 3h11.4a2 2 0 0 0 1.8-3L14 8V2", "M8.5 2h7", "M7 16h10"],
  pill: ["M10.5 20.5 3.5 13.5a4.95 4.95 0 1 1 7-7l7 7a4.95 4.95 0 1 1-7 7Z", "M7 7l10 10"],
  bed: ["M2 20v-8a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v8", "M2 17h20", "M6 10V6a2 2 0 0 1 2-2h8a2 2 0 0 1 2 2v4"],
  command: ["M12 3a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z", "M12 9v12", "M5 21h14", "M3 12h4", "M17 12h4"],
  activity: ["M22 12h-4l-3 9L9 3l-3 9H2"],
  board: ["M3 3h18v12H3z", "M8 21h8", "M12 15v6", "M7 8h4", "M7 11h7"],
  stethoscope: ["M5 3v5a4 4 0 0 0 8 0V3", "M3 3h4", "M11 3h2", "M9 16a5 5 0 0 0 10 0v-2", "M19 10a2 2 0 1 0 0 4 2 2 0 0 0 0-4Z"],
  route: ["M6 19a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M18 11a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z", "M6 16V8a2 2 0 0 1 2-2h5"],
  shield: ["M12 22s8-3.6 8-10V5l-8-3-8 3v7c0 6.4 8 10 8 10Z", "M9 12l2 2 4-4"],
  lifebuoy: ["M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20Z", "M12 16a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M4.9 4.9l5.7 5.7", "M13.4 13.4l5.7 5.7", "M19.1 4.9l-5.7 5.7", "M10.6 13.4l-5.7 5.7"],
  "id-badge": ["M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z", "M12 11a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z", "M7 20c.6-2.3 2.6-3.5 5-3.5s4.4 1.2 5 3.5"],
  people: ["M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2", "M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z", "M23 21v-2a4 4 0 0 0-3-3.87", "M16 3.13a4 4 0 0 1 0 7.75"],
  chart: ["M3 3v18h18", "M7 15v-4", "M12 17V7", "M17 13v-6"],
  file: ["M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z", "M14 2v6h6", "M9 15h6", "M9 11h2"],
  graph: ["M3 3v18h18", "M7 14l4-4 3 3 5-6"],
  device: ["M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z", "M12 2v3", "M12 19v3", "M2 12h3", "M19 12h3", "M5 5l2 2", "M17 17l2 2", "M19 5l-2 2", "M7 17l-2 2"],
  database: ["M12 8c4.97 0 9-1.34 9-3s-4.03-3-9-3-9 1.34-9 3 4.03 3 9 3Z", "M3 5v14c0 1.66 4.03 3 9 3s9-1.34 9-3V5", "M3 12c0 1.66 4.03 3 9 3s9-1.34 9-3"],
  network: ["M12 12a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z", "M5 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z", "M19 21a2.5 2.5 0 1 0 0-5 2.5 2.5 0 0 0 0 5Z", "M12 9.5 5.8 16", "M12 9.5l6.2 6.5", "M7.5 18.5h9"],
  microscope: ["M6 18h8", "M3 21h18", "M10 3v6l4 4H8l2-4V3", "M10 3h4", "M14 13a6 6 0 0 0 4 5"],
  folder: ["M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"],
  boxes: ["M16.5 9.4 7.55 4.24", "M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16Z", "M3.29 7 12 12 20.71 7", "M12 22V12"],
  sliders: ["M4 6h10", "M18 6h2", "M4 12h4", "M12 12h8", "M4 18h13", "M20 18h0", "M16 4v4", "M10 10v4", "M18 16v4"],
  upload: ["M12 16V4", "M6 10l6-6 6 6", "M4 20h16"],
  grid: ["M3 3h7v7H3z", "M14 3h7v7h-7z", "M3 14h7v7H3z", "M14 14h7v7h-7z"],
};

export function ModuleIcon({
  name,
  size = 26,
}: {
  name?: string;
  size?: number;
}) {
  const paths = (name && ICON_PATHS[name]) || ICON_PATHS.grid;
  return (
    <svg
      className="module-card-icon"
      viewBox="0 0 24 24"
      width={size}
      height={size}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {paths.map((d, i) => (
        <path key={i} d={d} />
      ))}
    </svg>
  );
}
