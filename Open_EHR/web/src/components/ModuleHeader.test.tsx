import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ModuleHeader } from "./ModuleHeader";

// RTL auto-cleanup does not run in this repo's vitest setup.
afterEach(cleanup);

describe("ModuleHeader (UX pass P-1/P-2 contract)", () => {
  it("renders exactly one h2, the purpose line and the primary action", () => {
    const onClick = vi.fn();
    const { container } = render(
      <ModuleHeader
        title="Patients"
        purpose="Find and register patients."
        primaryAction={{ label: "Start registration", onClick }}
      />,
    );
    expect(container.querySelectorAll("h2")).toHaveLength(1);
    expect(
      screen.getByRole("heading", { name: "Patients" }),
    ).toBeTruthy();
    expect(screen.getByText("Find and register patients.")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Start registration" }),
    );
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("shows the disabled reason next to a disabled primary action", () => {
    render(
      <ModuleHeader
        title="Population health"
        primaryAction={{
          label: "Run ETL now",
          onClick: () => {},
          disabled: true,
          disabledReason: "ETL runs are triggered by an administrator.",
        }}
      />,
    );
    const button = screen.getByRole("button", {
      name: "Run ETL now",
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(
      screen.getByText("ETL runs are triggered by an administrator."),
    ).toBeTruthy();
  });

  it("renders the group icon tile when icon/group are passed (elevation §8.6)", () => {
    const { container } = render(
      <ModuleHeader
        title="Laboratory"
        purpose="Orders, results and benches."
        icon="flask"
        group="Diagnostics & Pharmacy"
      />,
    );
    const header = container.querySelector(".module-header");
    expect(header?.getAttribute("data-group")).toBe(
      "Diagnostics & Pharmacy",
    );
    const tile = container.querySelector(".icon-tile");
    expect(tile).toBeTruthy();
    expect(tile?.querySelector("svg")).toBeTruthy();
    // Still exactly one h2.
    expect(container.querySelectorAll("h2")).toHaveLength(1);
  });

  it("renders no tile and no data-group without the elevation props", () => {
    const { container } = render(<ModuleHeader title="Results inbox" />);
    expect(container.querySelector(".icon-tile")).toBeNull();
    expect(
      container
        .querySelector(".module-header")
        ?.getAttribute("data-group"),
    ).toBeNull();
  });

  it("renders the live-indicator slot and no button without an action", () => {
    render(
      <ModuleHeader title="Results inbox">
        <span>3 critical unacknowledged</span>
      </ModuleHeader>,
    );
    expect(screen.getByText("3 critical unacknowledged")).toBeTruthy();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
