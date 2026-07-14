import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  it("labels the concurrency nav item 'Concurrency' (single-line, see #174)", () => {
    render(<Sidebar activeItem="home" onNavigate={vi.fn()} />);
    expect(screen.getByText("Concurrency")).toBeInTheDocument();
    // The old two-word label wrapped and center-aligned; it must be gone.
    expect(screen.queryByText("Concurrency Limits")).not.toBeInTheDocument();
  });

  it("renders each nav label left-aligned with no-wrap truncation", () => {
    render(<Sidebar activeItem="home" onNavigate={vi.fn()} />);
    for (const label of ["Home", "Task Explorer", "Concurrency", "Settings"]) {
      const span = screen.getByText(label);
      expect(span.className).toContain("truncate");
      expect(span.className).toContain("text-left");
    }
  });

  it("hides labels when collapsed", () => {
    render(<Sidebar activeItem="home" onNavigate={vi.fn()} collapsed />);
    expect(screen.queryByText("Concurrency")).not.toBeInTheDocument();
  });
});
