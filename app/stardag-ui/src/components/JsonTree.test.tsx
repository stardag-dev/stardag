import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { PAGE_SIZE } from "../utils/jsonTree";
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
    expect(item("epochs: 3, number")).not.toHaveAttribute("aria-expanded");
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
    expect(item("name: Alpha, string")).toHaveFocus();
    fireEvent.keyDown(item("name: Alpha, string"), { key: "ArrowDown" });
    const model = item(/^model:/);
    expect(model).toHaveFocus();

    fireEvent.keyDown(model, { key: "ArrowLeft" });
    expect(model).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(model, { key: "ArrowRight" });
    expect(model).toHaveAttribute("aria-expanded", "true");
    fireEvent.keyDown(model, { key: "ArrowRight" });
    expect(item("__namespace: demo, string")).toHaveFocus();
    fireEvent.keyDown(item("__namespace: demo, string"), { key: "ArrowLeft" });
    expect(model).toHaveFocus();
    expect(model).toHaveAttribute("tabindex", "0");

    fireEvent.keyDown(model, { key: "Enter" });
    expect(model).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(model, { key: "End" });
    expect(item("1: beta, string")).toHaveFocus();
    fireEvent.keyDown(item("1: beta, string"), { key: "Home" });
    expect(item("epochs: 3, number")).toHaveFocus();
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
    expect(screen.queryByRole("treeitem", { name: "units: 64, number" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(item("units: 64, number")).toBeInTheDocument();
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

  it("fuzzy-matches and marks each matched character", () => {
    renderTree({ layers: 2, learning_rate: 0.1 });
    search("lr");
    // Both match; the word-start `r` ranks first despite document order.
    expect(screen.getByText("1 of 2")).toBeInTheDocument();
    const best = item("learning_rate: 0.1, number");
    expect(best.className).toContain("bg-amber-50");
    const marks = [...best.querySelectorAll("mark")].map((m) => m.textContent);
    expect(marks).toEqual(["l", "r"]);
  });

  it("keeps the search position when the value is refreshed unchanged", () => {
    const { rerender } = render(<JsonTree value={VALUE} label="Task parameters" />);
    search("units");
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText("2 of 2")).toBeInTheDocument();

    // A refresh re-fetches an equal body as a new object.
    rerender(<JsonTree value={structuredClone(VALUE)} label="Task parameters" />);
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    expect(screen.getByText("2 of 2")).toBeInTheDocument();

    // A changed body is searched again.
    const changed = structuredClone(VALUE);
    changed.model.layers.push({ units: 16 });
    rerender(<JsonTree value={changed} label="Task parameters" />);
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    expect(screen.getByText("1 of 3")).toBeInTheDocument();
  });

  it("names scalars with their type, so a string and a boolean differ", () => {
    renderTree({ text: "false", flag: false, count: "3", none: null, word: "null" });
    expect(item("text: false, string")).toBeInTheDocument();
    expect(item("flag: false, boolean")).toBeInTheDocument();
    expect(item("count: 3, string")).toBeInTheDocument();
    expect(item("none: null")).toBeInTheDocument();
    expect(item("word: null, string")).toBeInTheDocument();
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

    fireEvent.click(
      item("name: Alpha, string").querySelector('[aria-label="Copy value"]')!,
    );
    expect(writeText).toHaveBeenLastCalledWith("Alpha");

    fireEvent.keyDown(model, { key: "c" });
    expect(JSON.parse(writeText.mock.lastCall![0])).toEqual(VALUE.model);
    fireEvent.keyDown(item("epochs: 3, number"), { key: "p" });
    expect(writeText).toHaveBeenLastCalledWith("$.epochs");
    // Ctrl+C stays the browser's.
    writeText.mockClear();
    fireEvent.keyDown(model, { key: "c", ctrlKey: true });
    expect(writeText).not.toHaveBeenCalled();
  });
});

describe("JsonTree on a large value", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("indexes and searches thousands of nodes", () => {
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
    // The top level only, one page of it: opening the next level would
    // pass the row budget.
    expect(screen.getAllByRole("treeitem")).toHaveLength(PAGE_SIZE + 1);
    expect(item("Show 100 more (100 not shown)")).toBeInTheDocument();

    // A match past the page moves the page to it.
    search("item 299");
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    expect(item("label: item 299, string")).toBeInTheDocument();

    // A query matching most of it reveals only the first matches. (The
    // one the previous search opened stays expanded, but the page has
    // moved back to the first match, away from it.)
    search("item");
    expect(screen.getByText("1 of 300")).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem", { name: /^label:/ })).toHaveLength(
      REVEAL_MATCHES,
    );
  });

  it("draws a page of a wide array however many of its children match", () => {
    const tree = renderTree({ values: Array(10_000).fill("alpha") });
    search("alpha");
    expect(screen.getByText("1 of 10000")).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem")).toHaveLength(1 + PAGE_SIZE + 1);
    expect(tree.querySelectorAll("mark")).toHaveLength(PAGE_SIZE);

    // Stepping back to the last match moves the page to it: still a page.
    fireEvent.click(screen.getByRole("button", { name: "Previous match" }));
    expect(screen.getByText("10000 of 10000")).toBeInTheDocument();
    expect(item("9999: alpha, string")).toBeInTheDocument();
    expect(item("Show 200 earlier (9,800 not shown)")).toBeInTheDocument();
    expect(screen.getAllByRole("treeitem")).toHaveLength(1 + 1 + PAGE_SIZE);
  });

  it("shows the next page from the show-more row, by click or keyboard", () => {
    renderTree({ values: Array.from({ length: 450 }, (_, i) => i) });
    fireEvent.click(item(/^values:/));
    fireEvent.click(item("Show 200 more (250 not shown)"));
    expect(item("399: 399, number")).toBeInTheDocument();
    const next = item("Show 50 more (50 not shown)");
    next.focus();
    fireEvent.keyDown(next, { key: "Enter" });
    expect(item("449: 449, number")).toBeInTheDocument();
    expect(item("400: 400, number")).toHaveFocus();
    expect(screen.queryByRole("treeitem", { name: /^Show/ })).toBeNull();
  });

  it("moves into a paged container's drawn page with ArrowRight", () => {
    renderTree({ values: Array(1000).fill("alpha") });
    search("alpha");
    fireEvent.click(screen.getByRole("button", { name: "Previous match" }));
    const values = item(/^values:/);
    values.focus();
    fireEvent.keyDown(values, { key: "ArrowRight" });
    expect(item("Show 200 earlier (800 not shown)")).toHaveFocus();
    fireEvent.keyDown(item("Show 200 earlier (800 not shown)"), { key: "ArrowDown" });
    expect(item("800: alpha, string")).toHaveFocus();
  });

  it("scrolls to the current match on every step, even a single one", () => {
    const scroll = vi.fn();
    Element.prototype.scrollIntoView = scroll;
    renderTree();
    search("beta");
    expect(scroll).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(scroll).toHaveBeenCalledTimes(3);
  });
});
