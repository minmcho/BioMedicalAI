import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { SkeletonCards, SkeletonRows } from "./Skeleton";

// Rich Design Elevation (spec §8.14): skeletons mirror layout only —
// they announce the same loading label as the text state and render
// no data-shaped content.

afterEach(cleanup);

describe("SkeletonCards", () => {
  it("renders the requested number of placeholder cards with a status label", () => {
    const { container } = render(<SkeletonCards count={3} />);
    expect(screen.getByRole("status").textContent).toContain("Loading…");
    expect(container.querySelectorAll(".skeleton-card")).toHaveLength(3);
    expect(
      container.querySelector(".module-card-grid")?.getAttribute("aria-hidden"),
    ).toBe("true");
  });

  it("accepts a custom label", () => {
    render(<SkeletonRows count={2} label="Loading patients…" />);
    expect(screen.getByRole("status").textContent).toContain(
      "Loading patients…",
    );
  });
});

describe("SkeletonRows", () => {
  it("renders the requested number of placeholder rows", () => {
    const { container } = render(<SkeletonRows count={4} />);
    expect(container.querySelectorAll(".skeleton-row")).toHaveLength(4);
  });
});
