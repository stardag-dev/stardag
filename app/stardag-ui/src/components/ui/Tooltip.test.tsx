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
    expect(button.parentElement).toHaveAttribute("aria-describedby", tip.id);
    // No native title anywhere: that is the slow, differently styled one.
    expect(button.parentElement).not.toHaveAttribute("title");
    act(() => {
      fireEvent.pointerLeave(button.parentElement!);
    });
    expect(screen.queryByRole("tooltip")).toBeNull();
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
