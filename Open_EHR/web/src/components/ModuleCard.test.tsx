import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { HomeCard } from "../api/types";
import type { NavItem } from "../auth/session";
import { ModuleCard, ModuleIcon } from "./ModuleCard";

// Home & Module UX pass: the Home card idiom — whole card is ONE
// button (1-click entry), live indicator only when GET /home/cards
// has a provider entry, status always text + tone.

afterEach(cleanup);

const LAB: NavItem = {
  key: "lab",
  label: "Laboratory",
  group: "Diagnostics & Pharmacy",
  purpose: "Laboratory orders, results and benches",
  icon: "flask",
};

const INDICATOR: HomeCard = {
  module: "lab",
  value: 3,
  label: "critical results open",
  status_text: "Needs attention",
  tone: "bad",
  timer_text: "Oldest 42m",
};

describe("ModuleCard", () => {
  it("renders label, purpose and the live indicator (value + label + text+tone badge + timer)", () => {
    render(<ModuleCard item={LAB} indicator={INDICATOR} onOpen={() => {}} />);
    expect(screen.getByText("Laboratory")).toBeTruthy();
    expect(screen.getByText("Laboratory orders, results and benches")).toBeTruthy();
    expect(screen.getByText("3")).toBeTruthy();
    expect(screen.getByText("critical results open")).toBeTruthy();
    // Status is text (never colour alone) carried by the badge.
    const badge = screen.getByText("Needs attention");
    expect(badge.className).toContain("badge-critical");
    expect(screen.getByText("Oldest 42m")).toBeTruthy();
    // No "Open" fallback while an indicator is present.
    expect(screen.queryByText("Open →")).toBeNull();
  });

  it("carries its group identity (data-group) and renders the icon in a tile", () => {
    const { container } = render(
      <ModuleCard item={LAB} onOpen={() => {}} />,
    );
    const card = container.querySelector(".module-card");
    expect(card?.getAttribute("data-group")).toBe("Diagnostics & Pharmacy");
    const tile = container.querySelector(".icon-tile");
    expect(tile).toBeTruthy();
    expect(tile?.querySelector("svg")).toBeTruthy();
  });

  it("without an indicator shows purpose + Open only (no invented numbers)", () => {
    render(<ModuleCard item={LAB} onOpen={() => {}} />);
    expect(screen.getByText("Open →")).toBeTruthy();
    expect(screen.queryByText("critical results open")).toBeNull();
  });

  it("the whole card is one button: a single click opens the module once", () => {
    const onOpen = vi.fn();
    render(<ModuleCard item={LAB} indicator={INDICATOR} onOpen={onOpen} />);
    const card = screen.getByRole("button", { name: "Open Laboratory" });
    fireEvent.click(card);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(onOpen).toHaveBeenCalledWith("lab");
  });

  it("an unknown tone degrades to the neutral badge, keeping the status text", () => {
    render(
      <ModuleCard
        item={LAB}
        indicator={{ ...INDICATOR, tone: "purple" as HomeCard["tone"], status_text: "Unknown state" }}
        onOpen={() => {}}
      />,
    );
    const badge = screen.getByText("Unknown state");
    expect(badge.className).toContain("status-badge-neutral");
  });

  it("omits the timer when the provider sends none", () => {
    const { timer_text: _dropped, ...noTimer } = INDICATOR;
    render(<ModuleCard item={LAB} indicator={noTimer} onOpen={() => {}} />);
    expect(screen.getByText("Needs attention")).toBeTruthy();
    expect(screen.queryByText(/Oldest/)).toBeNull();
  });
});

describe("ModuleIcon", () => {
  it("renders an svg for a known icon name and falls back for unknown names", () => {
    const known = render(<ModuleIcon name="flask" />);
    expect(known.container.querySelector("svg")).toBeTruthy();
    expect(known.container.querySelectorAll("path").length).toBeGreaterThan(0);
    cleanup();
    const fallback = render(<ModuleIcon name="does-not-exist" />);
    expect(fallback.container.querySelector("svg")).toBeTruthy();
    expect(fallback.container.querySelectorAll("path").length).toBe(4); // grid glyph
    cleanup();
    const none = render(<ModuleIcon />);
    expect(none.container.querySelector("svg")).toBeTruthy();
  });
});
