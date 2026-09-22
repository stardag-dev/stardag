import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider, useBreadcrumb } from "../context/BreadcrumbContext";
import type { Build, Task } from "../types/task";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1", slug: "default", name: "default" },
  }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { profile: { sub: "user-1" } } }),
}));

// The graph and the two self-fetching panels are not what this file is
// about, and each drags in a renderer or a request stream of its own.
vi.mock("./DagGraph", () => ({ DagGraph: () => <div data-testid="dag" /> }));
vi.mock("./BuildStopPanel", () => ({ BuildStopPanel: () => null }));
vi.mock("./BuildSchedulingPanel", () => ({ BuildSchedulingPanel: () => null }));
vi.mock("./TaskDetail", () => ({ TaskDetail: () => <div data-testid="detail" /> }));

vi.mock("../api/tasks", () => ({
  fetchBuild: vi.fn(),
  fetchTasksInBuild: vi.fn(),
  fetchBuildGraph: vi.fn(),
  cancelBuild: vi.fn(),
  completeBuild: vi.fn(),
  failBuild: vi.fn(),
}));

import { fetchBuild, fetchBuildGraph, fetchTasksInBuild } from "../api/tasks";
import { BuildView } from "./BuildView";

const BUILD_ID = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

function makeBuild(overrides: Partial<Build> = {}): Build {
  return {
    id: BUILD_ID,
    environment_id: "env-1",
    user_id: null,
    name: "golden-diamond-28",
    description: null,
    commit_hash: null,
    root_task_ids: [],
    created_at: new Date().toISOString(),
    status: "running",
    started_at: new Date().toISOString(),
    completed_at: null,
    status_triggered_by_user: null,
    last_active_at: new Date().toISOString(),
    last_activity_at: new Date().toISOString(),
    executor_metadata: {
      kind: "modal",
      workspace: "demo-workspace",
      app_name: "sd-stop-demo",
      reactive: true,
    },
    scope_key: "5c6ed85f155d9a01:2b7c",
    ...overrides,
  };
}

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    task_id: TASK_ID,
    environment_id: "env-1",
    task_namespace: "demo",
    task_name: "Parked",
    task_data: {},
    version: null,
    output_uri: null,
    created_at: new Date().toISOString(),
    status: "running",
    started_at: new Date().toISOString(),
    completed_at: null,
    error_message: null,
    artifact_count: 0,
    ...overrides,
  } as Task;
}

/**
 * Draws whatever the view put in the breadcrumb, the same way `App`'s
 * own `BreadcrumbNav` does, so the assertions below read the rendered
 * trail rather than the context's internals.
 */
function CrumbProbe() {
  const { items } = useBreadcrumb();
  return (
    <div data-testid="crumbs">
      {items.map((item, i) => (
        <span key={i} data-testid="crumb" title={item.title}>
          {item.label}
          {item.detail}
        </span>
      ))}
    </div>
  );
}

/** The trail's crumbs, in order. */
const crumbs = () => screen.getAllByTestId("crumb");

function renderView() {
  return render(
    <BreadcrumbProvider>
      <CrumbProbe />
      <BuildView buildId={BUILD_ID} onBack={vi.fn()} />
    </BreadcrumbProvider>,
  );
}

describe("BuildView header and tool-and-info bar", () => {
  beforeEach(() => {
    vi.mocked(fetchBuild).mockResolvedValue(makeBuild());
    vi.mocked(fetchTasksInBuild).mockResolvedValue([makeTask()]);
    vi.mocked(fetchBuildGraph).mockResolvedValue({ nodes: [], edges: [] });
  });

  // The whole point of the breadcrumb reduction: the trail says which
  // build you have open, and the status is the one mark that belongs to
  // "where am I". Everything else describes the build and lives below.
  it("puts only the status badge in the breadcrumb", async () => {
    renderView();
    await screen.findByText("golden-diamond-28");

    const trail = screen.getByTestId("crumbs");
    expect(trail).toHaveTextContent("running");
    expect(trail).not.toHaveTextContent("Modal: sd-stop-demo");
    expect(trail).not.toHaveTextContent("reactive");
    expect(trail).not.toHaveTextContent(/scope:/);
  });

  it("carries the full build id as the crumb's tooltip", async () => {
    renderView();
    await screen.findByText("golden-diamond-28");
    expect(crumbs()[1]).toHaveAttribute("title", BUILD_ID);
  });

  it("shortens a selected task's id in the trail, keeping the full one", async () => {
    const user = userEvent.setup();
    renderView();
    await screen.findByText("golden-diamond-28");

    // The table selects from the row itself, not from a button in it.
    await user.click(await screen.findByText("Parked"));

    await waitFor(() => expect(crumbs()).toHaveLength(3));
    const taskCrumb = crumbs()[2];
    expect(taskCrumb).toHaveTextContent(TASK_ID.slice(0, 8));
    expect(taskCrumb).not.toHaveTextContent(TASK_ID);
    expect(taskCrumb).toHaveAttribute("title", TASK_ID);
  });

  it("shows the chips evicted from the breadcrumb in the info section", async () => {
    renderView();
    expect(await screen.findByTitle(/open the Modal app dashboard/i)).toHaveTextContent(
      "Modal: sd-stop-demo",
    );
    expect(screen.getByTitle(/Reactive build/i)).toBeInTheDocument();
    expect(screen.getByText(/^scope: 5c6ed85f155d…$/)).toBeInTheDocument();
    expect(screen.getByText("1 task")).toBeInTheDocument();
  });

  // It used to be a full-width disclosure strip stacked above the DAG.
  it("keeps the build config behind a chip, not a band above the DAG", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchBuild).mockResolvedValue(
      makeBuild({ build_config: { "demo.Parked": { limit: 3 } } }),
    );
    renderView();

    const chip = await screen.findByRole("button", { name: /config: 1 class/ });
    expect(screen.queryByText(/"limit": 3/)).not.toBeInTheDocument();

    await user.click(chip);
    expect(await screen.findByText(/"limit": 3/)).toBeInTheDocument();
  });

  it("offers no config chip when the build set no overrides", async () => {
    renderView();
    await screen.findByText("golden-diamond-28");
    expect(screen.queryByRole("button", { name: /config:/ })).not.toBeInTheDocument();
  });
});
