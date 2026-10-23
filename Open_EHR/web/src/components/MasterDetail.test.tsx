import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { MasterDetail } from "./MasterDetail";

// RTL auto-cleanup does not run in this repo's vitest setup (no globals).
afterEach(cleanup);

interface Item {
  id: string;
  name: string;
}

const ITEMS: Item[] = [
  { id: "a", name: "Alpha" },
  { id: "b", name: "Beta" },
  { id: "c", name: "Gamma" },
];

function renderMasterDetail(
  overrides: Partial<Parameters<typeof MasterDetail<Item>>[0]> = {},
) {
  return render(
    <MasterDetail<Item>
      items={ITEMS}
      getKey={(i) => i.id}
      renderListItem={(i) => <span>{i.name}</span>}
      renderDetail={(i) => <p>Detail for {i.name}</p>}
      listLabel="Things"
      {...overrides}
    />,
  );
}

describe("MasterDetail", () => {
  it("renders skeleton rows in the list pane while loading (elevation §8.14)", () => {
    const { container } = renderMasterDetail({ items: [], loading: true });
    const pane = container.querySelector(".master-detail-list");
    expect(pane?.querySelectorAll(".skeleton-row")).toHaveLength(5);
    expect(pane?.textContent).toContain("Loading…");
    // The empty text does not show while loading.
    expect(screen.queryByText("No items.")).toBeNull();
  });

  it("defaults to the first item in uncontrolled mode", () => {
    renderMasterDetail();
    expect(screen.getByText("Detail for Alpha")).toBeTruthy();
    const buttons = screen.getAllByRole("button");
    expect(buttons).toHaveLength(3);
    expect(buttons[0].getAttribute("aria-pressed")).toBe("true");
    expect(buttons[1].getAttribute("aria-pressed")).toBe("false");
    expect(screen.getByLabelText("Things")).toBeTruthy();
  });

  it("clicking a row selects it and calls onSelect", () => {
    const onSelect = vi.fn();
    renderMasterDetail({ onSelect });
    fireEvent.click(screen.getByRole("button", { name: /Beta/ }));
    expect(onSelect).toHaveBeenCalledWith("b");
    expect(screen.getByText("Detail for Beta")).toBeTruthy();
    expect(
      screen.getByRole("button", { name: /Beta/ }).getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("controlled mode follows selectedKey and does not self-select", () => {
    const onSelect = vi.fn();
    const { rerender } = renderMasterDetail({
      selectedKey: "c",
      onSelect,
    });
    expect(screen.getByText("Detail for Gamma")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Alpha/ }));
    expect(onSelect).toHaveBeenCalledWith("a");
    // Still Gamma: the parent owns the selection.
    expect(screen.getByText("Detail for Gamma")).toBeTruthy();
    rerender(
      <MasterDetail<Item>
        items={ITEMS}
        getKey={(i) => i.id}
        renderListItem={(i) => <span>{i.name}</span>}
        renderDetail={(i) => <p>Detail for {i.name}</p>}
        listLabel="Things"
        selectedKey="a"
        onSelect={onSelect}
      />,
    );
    expect(screen.getByText("Detail for Alpha")).toBeTruthy();
  });

  it("controlled null shows the detail empty text and no pressed row", () => {
    renderMasterDetail({
      selectedKey: null,
      detailEmptyText: "Nothing selected yet.",
    });
    expect(screen.getByText("Nothing selected yet.")).toBeTruthy();
    for (const b of screen.getAllByRole("button")) {
      expect(b.getAttribute("aria-pressed")).toBe("false");
    }
  });

  it("empty items show emptyText in the list pane and detailEmptyText in the detail pane", () => {
    render(
      <MasterDetail<Item>
        items={[]}
        getKey={(i) => i.id}
        renderListItem={(i) => <span>{i.name}</span>}
        renderDetail={(i) => <p>Detail for {i.name}</p>}
        listLabel="Things"
        emptyText="Nothing here."
        detailEmptyText="No detail available."
      />,
    );
    expect(screen.getByText("Nothing here.")).toBeTruthy();
    expect(screen.getByText("No detail available.")).toBeTruthy();
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
