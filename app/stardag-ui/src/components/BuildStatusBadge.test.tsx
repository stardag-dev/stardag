import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { BuildStatusBadge } from "./BuildStatusBadge";
import { tooltipOf } from "../test/tooltip";

describe("BuildStatusBadge", () => {
  it("labels exit_early readably", () => {
    render(<BuildStatusBadge status="exit_early" />);
    expect(screen.getByText("exited early")).toBeInTheDocument();
  });

  it("marks a resumed running build", () => {
    render(<BuildStatusBadge status="running" isResumed />);
    expect(screen.getByText("running (resumed)")).toBeInTheDocument();
  });

  it("shows a registered plan-only build as registered", () => {
    render(<BuildStatusBadge status="registered" planOnly />);
    const badge = screen.getByText("registered");
    expect(tooltipOf(badge)).toContain("nothing ran");
  });

  it("shows a pending plan-only build as registration incomplete", () => {
    render(<BuildStatusBadge status="pending" planOnly />);
    const badge = screen.getByText("registration incomplete");
    expect(tooltipOf(badge)).toContain("never finished registering");
  });

  it("leaves an ordinary pending build pending", () => {
    render(<BuildStatusBadge status="pending" />);
    expect(screen.getByText("pending")).toBeInTheDocument();
  });
});
