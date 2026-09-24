import { fireEvent, render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { StatusBadge } from "./StatusBadge";

describe("StatusBadge", () => {
  it("renders pending status", () => {
    render(<StatusBadge status="pending" />);
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  it("renders running status", () => {
    render(<StatusBadge status="running" />);
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("renders completed status", () => {
    render(<StatusBadge status="completed" />);
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  it("renders failed status", () => {
    render(<StatusBadge status="failed" />);
    expect(screen.getByText("failed")).toBeInTheDocument();
  });
});

describe("StatusBadge claim holder link", () => {
  const HOLDER = "01a0c5c3-f18e-7d22-bcaf-00000000aaaa";
  const VIEWED = "01a0c5c3-f18e-7d22-bcaf-00000000bbbb";

  it("jumps to the build holding the claim, by click, Enter or Space", () => {
    const onOpenBuild = vi.fn();
    render(
      <StatusBadge
        status="running"
        holderBuildId={HOLDER}
        currentBuildId={VIEWED}
        onOpenBuild={onOpenBuild}
      />,
    );
    const badge = screen.getByRole("button", { name: /running/ });
    expect(badge).toHaveAttribute("title", expect.stringMatching(/click to view/));
    fireEvent.click(badge);
    fireEvent.keyDown(badge, { key: "Enter" });
    fireEvent.keyDown(badge, { key: " " });
    expect(onOpenBuild).toHaveBeenCalledTimes(3);
    expect(onOpenBuild).toHaveBeenCalledWith(HOLDER);
  });

  it("is not a link when the holder is the build on screen", () => {
    render(
      <StatusBadge
        status="running"
        holderBuildId={VIEWED}
        currentBuildId={VIEWED}
        onOpenBuild={vi.fn()}
      />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("is not a link for a status that holds no claim", () => {
    render(
      <StatusBadge status="completed" holderBuildId={HOLDER} onOpenBuild={vi.fn()} />,
    );
    expect(screen.queryByRole("button")).toBeNull();
  });
});
