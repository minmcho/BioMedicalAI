import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Empty, Loading } from "./StatusViews";

// Rich Design Elevation (§8.13): Empty is a designed panel; the
// label text is the caller's, unchanged; icon/action are additive.

afterEach(cleanup);

describe("Empty", () => {
  it("renders the label in the panel with no icon or action by default", () => {
    const { container } = render(<Empty label="No results yet." />);
    expect(screen.getByText("No results yet.")).toBeTruthy();
    const panel = container.querySelector(".state-empty");
    expect(panel?.classList.contains("empty-panel")).toBe(true);
    expect(panel?.getAttribute("role")).toBe("status");
    expect(container.querySelector(".empty-icon")).toBeNull();
    expect(container.querySelector("button")).toBeNull();
  });

  it("renders the icon glyph and a working primary action when supplied", () => {
    const onClick = vi.fn();
    const { container } = render(
      <Empty
        label="No specimens collected yet."
        icon="flask"
        action={{ label: "Collect a specimen", onClick }}
      />,
    );
    expect(container.querySelector(".empty-icon svg")).toBeTruthy();
    const button = screen.getByRole("button", {
      name: "Collect a specimen",
    });
    expect(button.classList.contains("btn-primary")).toBe(true);
    fireEvent.click(button);
    expect(onClick).toHaveBeenCalledTimes(1);
  });
});

describe("Loading", () => {
  it("keeps its text form and status role", () => {
    render(<Loading label="Loading results…" />);
    const el = screen.getByText("Loading results…");
    expect(el.getAttribute("role")).toBe("status");
    expect(el.classList.contains("state-loading")).toBe(true);
  });
});
