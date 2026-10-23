// Rich Design Elevation pass (DESIGN_SPEC §8.14): layout-mirror
// loading placeholders. Skeletons mirror the STRUCTURE of what is
// loading — they never fake data (no invented numbers, no phantom
// rows presented as content). Adopted ONLY where the structure is
// already known: Home's card grid (SkeletonCards) and MasterDetail
// list panes (SkeletonRows). Everywhere else the text Loading state
// stays. Assistive tech gets the same loading label the text state
// uses (role="status" + visible-to-AT text); the bars are hidden
// from AT. Shimmer is disabled under prefers-reduced-motion (CSS).

export function SkeletonCards({
  count = 6,
  label = "Loading…",
}: {
  count?: number;
  label?: string;
}) {
  return (
    <div className="skeleton-cards" role="status">
      <span className="visually-hidden">{label}</span>
      <div className="module-card-grid" aria-hidden="true">
        {Array.from({ length: count }, (_, i) => (
          <div key={i} className="skeleton skeleton-card">
            <span className="skeleton-line">
              <span className="skeleton-bar skeleton-bar-tile" />
              <span className="skeleton-bar skeleton-bar-title" />
            </span>
            <span className="skeleton-bar" />
            <span className="skeleton-bar skeleton-bar-short" />
          </div>
        ))}
      </div>
    </div>
  );
}

export function SkeletonRows({
  count = 5,
  label = "Loading…",
}: {
  count?: number;
  label?: string;
}) {
  return (
    <div className="skeleton-rows" role="status">
      <span className="visually-hidden">{label}</span>
      <div className="skeleton-row-list" aria-hidden="true">
        {Array.from({ length: count }, (_, i) => (
          <div key={i} className="skeleton skeleton-row">
            <span className="skeleton-bar skeleton-bar-title" />
            <span className="skeleton-bar skeleton-bar-short" />
          </div>
        ))}
      </div>
    </div>
  );
}
