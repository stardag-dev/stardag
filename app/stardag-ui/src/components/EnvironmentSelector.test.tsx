import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Environment, WorkspaceSummary } from "../api/workspaces";

const setActiveEnvironment = vi.fn();

const WORKSPACE = { id: "ws-1", name: "acme", slug: "acme" } as WorkspaceSummary;
const MAIN = { id: "env-1", name: "main", slug: "main" } as Environment;
const DEV = { id: "env-2", name: "dev", slug: "dev" } as Environment;

let activeWorkspace: WorkspaceSummary | null = WORKSPACE;
let environments: Environment[] = [MAIN, DEV];
let activeEnvironment: Environment | null = MAIN;

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeWorkspace,
    environments,
    activeEnvironment,
    setActiveEnvironment,
  }),
}));

import { EnvironmentSelector } from "./EnvironmentSelector";

describe("EnvironmentSelector", () => {
  beforeEach(() => {
    setActiveEnvironment.mockClear();
    activeWorkspace = WORKSPACE;
    environments = [MAIN, DEV];
    activeEnvironment = MAIN;
  });

  it("names the active environment on the trigger", () => {
    render(<EnvironmentSelector />);
    expect(
      screen.getByRole("button", { name: /main/, expanded: false }),
    ).toBeInTheDocument();
  });

  it("switches environment from its own control, without the workspace menu", async () => {
    const user = userEvent.setup();
    render(<EnvironmentSelector />);

    await user.click(screen.getByRole("button", { expanded: false }));
    await user.click(screen.getByRole("menuitem", { name: "dev" }));

    expect(setActiveEnvironment).toHaveBeenCalledWith(DEV);
  });

  it("closes the menu once an environment is chosen", async () => {
    const user = userEvent.setup();
    render(<EnvironmentSelector />);

    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.click(screen.getByRole("menuitem", { name: "dev" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  it("closes the menu when the pointer goes down outside it", async () => {
    const user = userEvent.setup();
    render(
      <div>
        <EnvironmentSelector />
        <button type="button">elsewhere</button>
      </div>,
    );

    await user.click(screen.getByRole("button", { expanded: false }));
    expect(screen.getByRole("menu")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "elsewhere" }));
    expect(screen.queryByRole("menu")).not.toBeInTheDocument();
  });

  // A control with nothing to choose between is noise in the trail, and
  // it would also leave its own separator hanging with no crumb after it.
  it("renders nothing when the workspace has no environments", () => {
    environments = [];
    const { container } = render(<EnvironmentSelector />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing before a workspace is active", () => {
    activeWorkspace = null;
    const { container } = render(<EnvironmentSelector />);
    expect(container).toBeEmptyDOMElement();
  });
});
