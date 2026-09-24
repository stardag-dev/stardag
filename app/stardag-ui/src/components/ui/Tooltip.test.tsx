import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Tooltip } from "./Tooltip";

function rect(left: number, top: number, width: number, height: number): DOMRect {
  return {
    left,
    top,
    width,
    height,
    right: left + width,
    bottom: top + height,
    x: left,
    y: top,
    toJSON: () => ({}),
  } as DOMRect;
}

afterEach(() => vi.restoreAllMocks());

describe("Tooltip", () => {
  it("shows at once on hover, describes its anchor, and hides on leave", () => {
    render(
      <Tooltip content="What this is">
        <button type="button">Thing</button>
      </Tooltip>,
    );
    expect(screen.queryByRole("tooltip")).toBeNull();
    const button = screen.getByRole("button", { name: "Thing" });
    act(() => {
      fireEvent.pointerEnter(button);
    });
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("What this is");
    // The focusable child is described, not the wrapper, and not only
    // while hovered.
    expect(button).toHaveAccessibleDescription("What this is");
    expect(button.parentElement).not.toHaveAttribute("aria-describedby");
    // No native title anywhere: that is the slow, differently styled one.
    expect(button.parentElement).not.toHaveAttribute("title");
    act(() => {
      fireEvent.pointerLeave(button.parentElement!);
    });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("describes the child from the start, and leaves a caller's own description alone", () => {
    render(
      <>
        <Tooltip content="Help text">
          <button type="button" aria-describedby="own">
            A
          </button>
        </Tooltip>
        <span id="own">Own</span>
        <Tooltip content="Not me" describe={false}>
          <button type="button">B</button>
        </Tooltip>
      </>,
    );
    expect(screen.getByRole("button", { name: "A" })).toHaveAccessibleDescription(
      "Own Help text",
    );
    expect(screen.getByRole("button", { name: "B" })).not.toHaveAttribute(
      "aria-describedby",
    );
  });

  it("clamps into the window when it fits neither below nor above", () => {
    Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 300, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        return this.getAttribute("role") === "tooltip"
          ? rect(0, 0, 200, 200)
          : rect(400, 140, 20, 20);
      },
    );
    render(
      <Tooltip content="Tall">
        <button type="button">Mid</button>
      </Tooltip>,
    );
    act(() => {
      fireEvent.pointerEnter(screen.getByRole("button"));
    });
    // Below would end at 366 > 292; above would start at -66 < 8.
    expect(screen.getByRole("tooltip").style.top).toBe(`${300 - 200 - 8}px`);
    expect(screen.getByRole("tooltip").style.maxWidth).toContain("100vw");
  });

  it("shows on keyboard focus", () => {
    render(
      <Tooltip content="Focus help">
        <button type="button">Thing</button>
      </Tooltip>,
    );
    act(() => {
      screen.getByRole("button").focus();
    });
    expect(screen.getByRole("tooltip")).toHaveTextContent("Focus help");
  });

  it("stays inside the window next to its right edge", () => {
    Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        return this.getAttribute("role") === "tooltip"
          ? rect(0, 0, 240, 40)
          : rect(980, 10, 16, 16);
      },
    );
    render(
      <Tooltip content="A long explanation">
        <button type="button">Edge</button>
      </Tooltip>,
    );
    act(() => {
      fireEvent.pointerEnter(screen.getByRole("button"));
    });
    const tip = screen.getByRole("tooltip");
    // Centred would put its left at 868 and its right at 1108; clamped to
    // 1000 - 240 - 8.
    expect(tip.style.left).toBe("752px");
    expect(tip.style.top).toBe("32px");
  });

  it("flips above its anchor near the bottom edge", () => {
    Object.defineProperty(window, "innerWidth", { value: 1000, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 800, configurable: true });
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(
      function (this: HTMLElement) {
        return this.getAttribute("role") === "tooltip"
          ? rect(0, 0, 100, 40)
          : rect(400, 770, 20, 20);
      },
    );
    render(
      <Tooltip content="Low">
        <button type="button">Low</button>
      </Tooltip>,
    );
    act(() => {
      fireEvent.pointerEnter(screen.getByRole("button"));
    });
    expect(screen.getByRole("tooltip").style.top).toBe(`${770 - 6 - 40}px`);
  });

  it("draws nothing for empty content", () => {
    render(
      <Tooltip content="">
        <button type="button">Thing</button>
      </Tooltip>,
    );
    act(() => {
      fireEvent.pointerEnter(screen.getByRole("button"));
    });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
