import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { Sidebar } from "./Sidebar";

describe("Sidebar", () => {
  it("offers deployments and the concurrency limits page", () => {
    const onNavigate = vi.fn();
    render(<Sidebar activeItem="builds" onNavigate={onNavigate} />);
    expect(screen.getByText("Deployments")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Concurrency" }));
    expect(onNavigate).toHaveBeenCalledWith("limits");
  });

  it("renders each nav label left-aligned with no-wrap truncation", () => {
    render(<Sidebar activeItem="builds" onNavigate={vi.fn()} />);
    for (const label of ["Builds", "Tasks", "Deployments", "Concurrency", "Settings"]) {
      const span = screen.getByText(label);
      expect(span.className).toContain("truncate");
      expect(span.className).toContain("text-left");
    }
  });

  it("gives each nav item a title tooltip carrying the full label", () => {
    // With single-line `truncate`, an ellipsized label must still be
    // readable on hover — so every item carries `title={label}` (see #174).
    render(<Sidebar activeItem="builds" onNavigate={vi.fn()} />);
    for (const label of ["Builds", "Tasks", "Deployments", "Concurrency", "Settings"]) {
      const button = screen.getByRole("button", { name: label });
      expect(button).toHaveAttribute("title", label);
    }
  });

  it("hides labels when collapsed", () => {
    render(<Sidebar activeItem="builds" onNavigate={vi.fn()} collapsed />);
    expect(screen.queryByText("Deployments")).not.toBeInTheDocument();
  });
});
