import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { SEARCH_DEBOUNCE_MS } from "../hooks/useTreeSearch";
import { JsonDiffView } from "./JsonDiffView";

const dataset = (split: string) => ({
  __namespace: "examples.ml",
  __name: "Dataset",
  split,
});

const LEFT = {
  __namespace: "examples.ml",
  __name: "Metrics",
  model: {
    __namespace: "examples.ml",
    __name: "TrainModel",
    learning_rate: 0.1,
    epochs: 10,
    optimizer: "adam",
  },
  dataset: dataset("test"),
  threshold: 0.5,
  notes: "baseline run",
};

const RIGHT = {
  __namespace: "examples.ml",
  __name: "Metrics",
  model: {
    __namespace: "examples.ml",
    __name: "TrainModel",
    learning_rate: 0.01,
    epochs: 10,
    optimizer: "adam",
    warmup: 100,
  },
  dataset: dataset("test"),
  threshold: 0.5,
};

function renderDiff(
  left: Record<string, unknown> = LEFT,
  right: Record<string, unknown> = RIGHT,
) {
  render(<JsonDiffView left={left} right={right} />);
  return screen.getByRole("tree", { name: "Parameter differences" });
}

function item(name: string | RegExp) {
  return screen.getByRole("treeitem", { name });
}

function search(text: string) {
  fireEvent.change(
    screen.getByRole("searchbox", { name: "Search parameter differences" }),
    { target: { value: text } },
  );
  act(() => {
    vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
  });
}

describe("JsonDiffView", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("expands what differs, labels nested tasks, and colours each status", () => {
    renderDiff();
    const model = item(/^model:/);
    expect(model).toHaveAttribute("aria-expanded", "true");
    expect(model).toHaveAccessibleName(
      "model: examples.ml.TrainModel object, 6 keys, 1 added, 1 changed",
    );
    expect(within(model).getByText("examples.ml.TrainModel")).toBeInTheDocument();

    const lr = item(/^learning_rate:/);
    expect(lr).toHaveAccessibleName(
      "learning_rate: changed from 0.1, number to 0.01, number",
    );
    expect(lr).toHaveAttribute("data-status", "changed");
    // Old and new both shown, as a deletion and an insertion.
    expect(lr.querySelector("del")).toHaveTextContent("0.1");
    expect(lr.querySelector("ins")).toHaveTextContent("0.01");

    expect(item(/^warmup:/)).toHaveAttribute("data-status", "added");
    expect(item(/^warmup:/).className).toMatch(/bg-green-50/);
    expect(item(/^notes:/)).toHaveAttribute("data-status", "removed");
    expect(item(/^notes:/).className).toMatch(/bg-red-50/);
    expect(item(/^threshold:/)).toHaveAttribute("data-status", "unchanged");
    // The overall summary.
    expect(screen.getByText("1 added, 1 removed, 1 changed")).toBeInTheDocument();
  });

  it("collapses an identical nested task, and summarises a collapsed one", () => {
    renderDiff();
    const ds = item(/^dataset:/);
    expect(ds).toHaveAttribute("aria-expanded", "false");
    expect(ds).toHaveTextContent("identical");
    expect(screen.queryByRole("treeitem", { name: /^split:/ })).toBeNull();

    const model = item(/^model:/);
    expect(within(model).queryByTestId("diff-counts")).toBeNull();
    fireEvent.click(model);
    expect(model).toHaveAttribute("aria-expanded", "false");
    const badge = within(model).getByTestId("diff-counts");
    expect(badge).toHaveTextContent("+1~1");
    expect(badge).toHaveAccessibleName("1 added, 1 changed");
  });

  it("shows only the differences on request, and everything again", () => {
    renderDiff();
    expect(item(/^threshold:/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Only differences" }));
    expect(screen.queryByRole("treeitem", { name: /^threshold:/ })).toBeNull();
    expect(screen.queryByRole("treeitem", { name: /^dataset:/ })).toBeNull();
    expect(screen.queryByRole("treeitem", { name: /^epochs:/ })).toBeNull();
    expect(item(/^learning_rate:/)).toBeInTheDocument();
    // Positions describe what is drawn.
    expect(item(/^model:/)).toHaveAttribute("aria-setsize", "2");
    expect(item(/^warmup:/)).toHaveAttribute("aria-posinset", "2");
    fireEvent.click(screen.getByRole("checkbox", { name: "Only differences" }));
    expect(item(/^threshold:/)).toBeInTheDocument();
  });

  it("says so when nothing differs", () => {
    renderDiff(LEFT, LEFT);
    expect(screen.getByText("Identical parameters.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Only differences" }));
    expect(screen.getByText("No differences.")).toBeInTheDocument();
  });

  it("expands and collapses everything", () => {
    renderDiff();
    fireEvent.click(screen.getByRole("button", { name: "Expand all" }));
    expect(item(/^split:/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Collapse all" }));
    expect(item(/^model:/)).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("treeitem", { name: /^learning_rate:/ })).toBeNull();
  });

  it("searches both sides, revealing and highlighting matches", () => {
    renderDiff();
    // A value only on the left, under a collapsed node elsewhere: "test"
    // is the identical dataset's split.
    search("baseline");
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    expect(item(/^notes:/).querySelector("mark")).toHaveTextContent("baseline");

    search("split");
    // Revealed inside the collapsed identical task.
    expect(item(/^split:/)).toBeInTheDocument();
    expect(item(/^dataset:/)).toHaveAttribute("aria-expanded", "true");

    // Both sides of a change can match: the old value and the new.
    search("0.1");
    const lr = item(/^learning_rate:/);
    expect(lr.querySelector("del mark")).toHaveTextContent("0.1");
    // Fuzzily on the new side: `0.` and `1` of `0.01`.
    const marked = Array.from(lr.querySelectorAll("ins mark"), (m) => m.textContent);
    expect(marked.join("")).toBe("0.1");

    search("zzzz");
    expect(screen.getByText("No matches")).toBeInTheDocument();
  });

  it("drops stale matches at once when only-differences re-indexes", () => {
    renderDiff();
    search("threshold");
    expect(screen.getByText("1 of 1")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("checkbox", { name: "Only differences" }));
    // Before the debounce re-runs: no stale index to step to.
    expect(screen.getByText("No matches")).toBeInTheDocument();
    expect(() =>
      fireEvent.click(screen.getByRole("button", { name: "Next match" })),
    ).not.toThrow();
    act(() => {
      vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS);
    });
    // `threshold` is unchanged, so it is not in the differences.
    expect(screen.getByText("No matches")).toBeInTheDocument();
  });

  it("steps through matches with next and previous", () => {
    renderDiff();
    search("ep");
    const count = screen.getByText(/ of /);
    expect(count.textContent).toMatch(/^1 of \d+$/);
    fireEvent.click(screen.getByRole("button", { name: "Next match" }));
    expect(screen.getByText(/ of /).textContent).toMatch(/^2 of /);
    fireEvent.click(screen.getByRole("button", { name: "Previous match" }));
    expect(screen.getByText(/ of /).textContent).toMatch(/^1 of /);
  });

  it("folds with the keyboard and copies paths and values", async () => {
    const writeText = vi.fn(async () => {});
    Object.assign(navigator, { clipboard: { writeText } });
    renderDiff();
    const model = item(/^model:/);
    model.focus();
    fireEvent.keyDown(model, { key: "ArrowLeft" });
    expect(model).toHaveAttribute("aria-expanded", "false");
    fireEvent.keyDown(model, { key: "ArrowRight" });
    expect(model).toHaveAttribute("aria-expanded", "true");

    const lr = item(/^learning_rate:/);
    await act(async () => {
      fireEvent.keyDown(lr, { key: "p" });
    });
    expect(writeText).toHaveBeenLastCalledWith("$.model.learning_rate");
    await act(async () => {
      fireEvent.keyDown(lr, { key: "c" });
    });
    expect(writeText).toHaveBeenLastCalledWith("0.01");
    await act(async () => {
      fireEvent.click(within(lr).getByRole("button", { name: "Copy old value" }));
    });
    expect(writeText).toHaveBeenLastCalledWith("0.1");
    writeText.mockClear();
    await act(async () => {
      fireEvent.keyDown(lr, { key: "o" });
    });
    expect(writeText).toHaveBeenLastCalledWith("0.1");
    expect(within(lr).getByRole("button", { name: "Copy old value" })).toHaveAttribute(
      "title",
      "Copy old value (o)",
    );
    // A removed node copies its (left) value.
    await act(async () => {
      fireEvent.keyDown(item(/^notes:/), { key: "c" });
    });
    expect(writeText).toHaveBeenLastCalledWith("baseline run");
  });

  it("labels a nested task whose type changed with both names", () => {
    const right = {
      ...RIGHT,
      model: { __namespace: "examples.ml", __name: "FineTune", base: "m1" },
    };
    renderDiff(LEFT, right);
    const model = item(/^model:/);
    expect(within(model).getByText("examples.ml.TrainModel").tagName).toBe("SPAN");
    expect(within(model).getByText("examples.ml.FineTune")).toBeInTheDocument();
    expect(model.getAttribute("aria-label")).toContain(
      "examples.ml.TrainModel to examples.ml.FineTune",
    );
  });

  it("pages a wide changed array", () => {
    const left = { xs: Array.from({ length: 450 }, (_, i) => i) };
    const right = { xs: Array.from({ length: 450 }, (_, i) => i * 2) };
    renderDiff(left, right);
    expect(
      screen.getByRole("treeitem", { name: /^Show 200 more/ }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("treeitem", { name: /^449:/ })).toBeNull();
  });
});
