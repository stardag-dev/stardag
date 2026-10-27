import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { JsonTree, REVEAL_MATCHES, SEARCH_DEBOUNCE_MS } from "./JsonTree";

const VALUE = {
  epochs: 3,
  name: "Alpha",
  model: {
    __namespace: "demo",
    __name: "Train",
    layers: [{ units: 64 }, { units: 32 }],
  },
  tags: ["alpha", "beta"],
};

function renderTree(value: unknown = VALUE) {
  render(<JsonTree value={value} label="Task parameters" />);
  return screen.getByRole("tree", { name: "Task parameters" });
}

function item(name: string | RegExp) {
  return screen.getByRole("treeitem", { name });
}

function search(text: string) {
  fireEvent.change(screen.getByRole("searchbox", { name: "Search task parameters" }), {
    target: { value: text },
  });
  act(() => {
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
  });
}

describe("JsonTree", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("draws an ARIA tree, two levels open, with sizes on collapsed containers", () => {
    renderTree();
    const model = item(/^model:/);
    expect(model).toHaveAttribute("aria-level", "1");
    expect(model).toHaveAttribute("aria-expanded", "true");
    expect(model).toHaveAttribute("aria-posinset", "3");
    expect(model).toHaveAttribute("aria-setsize", "4");
    // A nested task body is labelled with its task name.
    expect(model).toHaveAccessibleName("model: demo.Train object, 3 keys");
    const layers = item(/^layers:/);
    expect(layers).toHaveAttribute("aria-level", "2");
    expect(layers).toHaveAttribute("aria-expanded", "false");
    expect(layers).toHaveTextContent("2 items");
    // Scalars carry no expanded state.
    expect(item("epochs: 3")).not.toHaveAttribute("aria-expanded");
    expect(screen.queryByRole("treeitem", { name: /^units/ })).toBeNull();
  });

  it("toggles a node on click, and expands and collapses everything", () => {
    renderTree();
    fireEvent.click(item(/^layers:/));
    expect(item(/^layers:/)).toHaveAttribute("aria-expanded", "true");
    expect(screen.getAllByRole("treeitem", { name: /^0:/ })).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(screen.getAllByRole("treeitem")).toHaveLength(4);

    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(screen.getAllByRole("treeitem", { name: /^units:/ })).toHaveLength(2);
  });

  it("keeps one row in the tab order and moves and folds with the keyboard", () => {
    renderTree();
    const rows = screen.getAllByRole("treeitem");
    expect(rows.filter((r) => r.tabIndex === 0)).toHaveLength(1);
    expect(rows[0]).toHaveAttribute("tabindex", "0");

    rows[0].focus();
    fireEvent.keyDown(rows[0], { key: "ArrowDown" });
    expect(item("name: Alpha")).toHaveFocus();
    fireEvent.keyDown(item("name: Alpha"), { key: "ArrowDown" });
    const model = item(/^model:/);
    expect(model).toHaveFocus();

    fireEvent.keyDown(model, { key: "ArrowLeft" });
    expect(model).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(model, { key: "ArrowRight" });
    expect(model).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(model, { key: "ArrowRight" });
    expect(item("__namespace: demo")).toHaveFocus();
    fireEvent.keyDown(item("__namespace: demo"), { key: "ArrowLeft" });
    expect(model).toHaveFocus();
    expect(model).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(model, { key: "Enter" });
    expect(model).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(model, { key: "End" });
    expect(item("1: beta")).toHaveFocus();
    fireEvent.keyDown(item("1: beta"), { key: "Home" });
    expect(item("epochs: 3")).toHaveFocus();
  });

  it("highlights matches, expands their ancestors and steps through them", () => {
    const tree = renderTree();
    search("UNITS");
    // Both matches were folded away under `layers`; now they are shown.
    const units = screen.getAllByRole("treeitem", { name: /^units:/ });
    expect(units).toHaveLength(2);
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    const marks = tree.querySelectorAll("mark");
    expect(marks).toHaveLength(2);
    expect(marks[0]).toHaveTextContent("units");

    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    // Enter and Shift+Enter in the box do the same.
    const box = screen.getByRole("searchbox");
    fireEvent.keyDown(box, { key: "Enter", shiftKey: true });
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
  });

  it("re-expands a match folded away since the search ran", () => {
    renderTree();
    search("64");
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(screen.queryByRole("treeitem", { name: "units: 64" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(item("units: 64")).toBeInTheDocument();
  });

  it("waits for typing to pause, says when nothing matches, and clears on Escape", () => {
    const tree = renderTree();
    const box = screen.getByRole("searchbox");
    fireEvent.change(box, { target: { value: "beta" } });
    expect(tree.querySelectorAll("mark")).toHaveLength(0);
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    expect(tree.querySelectorAll("mark")).toHaveLength(1);

    search("zzz");
    expect(screen.getByText("No matches")).toBeInTheDocument();

    fireEvent.keyDown(box, { key: "Escape" });
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    expect(box).toHaveValue("");
    expect(screen.queryByText("No matches")).toBeNull();
  });

  it("says when the value is empty", () => {
    render(<JsonTree value={{}} label="Task parameters" emptyText="No parameters." />);
    expect(screen.getByText("No parameters.")).toBeInTheDocument();
    expect(screen.queryByRole("tree")).toBeNull();
  });
});

describe("JsonTree copy", () => {
  it("copies a node's JSON path and value, from the buttons and the keyboard", async () => {
    // jsdom has no clipboard of its own.
    const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });
    renderTree();
    const model = item(/^model:/);

    fireEvent.click(model.querySelector('[aria-label="Copy path"]')!);
    expect(writeText).toHaveBeenLastCalledWith("$.model");
    // Copying does not toggle the row.
    expect(model).toHaveAttribute("aria-expanded", "true");
    expect(await screen.findByRole("status")).toHaveTextContent("Copied path $.model");

    fireEvent.click(item("name: Alpha").querySelector('[aria-label="Copy value"]')!);
    expect(writeText).toHaveBeenLastCalledWith("Alpha");

    fireEvent.keyDown(model, { key: "c" });
    expect(JSON.parse(writeText.mock.lastCall![0])).toEqual(VALUE.model);
    fireEvent.keyDown(item("epochs: 3"), { key: "p" });
    expect(writeText).toHaveBeenLastCalledWith("$.epochs");
    // Ctrl+C stays the browser's.
    writeText.mockClear();
    fireEvent.keyDown(model, { key: "c", ctrlKey: true });
    expect(writeText).not.toHaveBeenCalled();
  });
});

describe("JsonTree on a large value", () => {
  it("indexes and searches thousands of nodes", () => {
    vi.useFakeTimers();
    const big = Object.fromEntries(
      Array.from({ length: 300 }, (_, i) => [
        `task_${i}`,
        {
          id: i,
          label: `item ${i}`,
          values: Array.from({ length: 10 }, (_, j) => j),
        },
      ]),
    );
    renderTree(big);
    // The top level only: opening the next one would pass the row budget.
    expect(screen.getAllByRole("treeitem")).toHaveLength(300);
    search("item 299");
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    expect(item("label: item 299")).toBeInTheDocument();

    // A query matching most of it reveals only the first matches (plus
    // the one the previous search opened: expansion is kept).
    search("item");
    expect(screen.getByText("1 of 300")).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem", { name: /^label:/ })).toHaveLength(
      REVEAL_MATCHES + 1,
    );
    vi.useRealTimers();
  });
});
