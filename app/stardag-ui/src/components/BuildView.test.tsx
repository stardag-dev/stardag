import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import type { Build, BuildFrontier, PlanMember } from "../types/task";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({ activeEnvironment: { id: "env-1" } }),
}));
vi.mock("../hooks/useDeployments", () => ({
  useDeployments: () => ({
    deployments: [],
    byId: new Map(),
    loading: false,
    error: null,
  }),
}));
// The graph, the task panel and the self-fetching dialogs are not what this
// file is about, and each drags in a renderer or a request stream.
vi.mock("./DagGraph", () => ({ DagGraph: () => <div data-testid="dag" /> }));
vi.mock("./TaskDetail", () => ({
  TaskDetail: () => <div data-testid="detail" />,
}));
vi.mock("./BuildControlsDialog", () => ({ BuildControlsDialog: () => null }));
vi.mock("./BuildInfoDialog", () => ({ BuildInfoDialog: () => null }));
vi.mock("./BuildSchedulingPanel", () => ({ BuildSchedulingPanel: () => null }));

const reload = vi.hoisted(() => vi.fn(async () => {}));
const planState = vi.hoisted(() => ({
  current: {} as Record<string, unknown>,
}));
vi.mock("../hooks/useBuildPlan", () => ({
  useBuildPlan: () => planState.current,
}));

import { BuildView } from "./BuildView";

const BUILD_ID = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

function makeBuild(overrides: Partial<Build> = {}): Build {
  return {
    id: BUILD_ID,
    name: "golden-diamond-28",
    description: null,
    status: "running",
    root_task_ids: [TASK_ID],
    created_at: "2026-09-24T00:00:00Z",
    started_at: "2026-09-24T00:00:00Z",
    completed_at: null,
    last_active_at: "2026-09-24T00:00:00Z",
    is_resumed: false,
    status_triggered_by_user_id: null,
    executor_metadata: null,
    reactive_app_name: null,
    reactive_tick_kwargs: null,
    error_message: null,
    ...overrides,
  };
}

const member: PlanMember = {
  task_id: TASK_ID,
  instance_id: "i-1",
  instance_hash: "h1",
  task_namespace: "demo",
  task_name: "Root",
  status: "pending",
  is_root: true,
  admitted_by: "root",
  excluded_at: null,
  excluded_reason: null,
  attempts: 0,
  interruptions: 0,
};

const frontier: BuildFrontier = {
  build_id: BUILD_ID,
  plan_id: "plan-1",
  deployment_id: "dep-1",
  settings_hash: "abcdef0123456789",
  sealed: true,
  plan_complete: false,
  build_status: "running",
  reactive_app_name: null,
  reactive_tick_kwargs: null,
  runnable: [],
  discovery_jobs: [],
  running: [],
  closure: null,
};

function setPlan(build: Build) {
  planState.current = {
    build,
    frontier,
    frontierError: null,
    view: { members: [member], edges: [] },
    planError: null,
    loading: false,
    error: null,
    loadedKey: `env-1:${BUILD_ID}`,
    reload,
    setBuild: vi.fn(),
  };
}

function renderView(onOpenTask?: (taskId: string) => void) {
  return render(
    <BreadcrumbProvider>
      <BuildView buildId={BUILD_ID} onBack={() => {}} onOpenTask={onOpenTask} />
    </BreadcrumbProvider>,
  );
}

beforeEach(() => {
  reload.mockClear();
  setPlan(makeBuild());
});

describe("BuildView auto-refresh", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("refreshes on a single click, after the double-click window", () => {
    renderView();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    expect(reload).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(300));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("toggles auto-refresh on a double-click, and a single click stops it", () => {
    renderView();
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(
      screen.getByRole("button", { name: "Stop auto-refreshing" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();

    act(() => vi.advanceTimersByTime(5000));
    expect(reload).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Stop auto-refreshing" }));
    act(() => vi.advanceTimersByTime(300));
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    act(() => vi.advanceTimersByTime(10000));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("runs one refresh at a time", async () => {
    // The first read never answers, so it stays in flight.
    reload.mockImplementationOnce(() => new Promise(() => {}));
    renderView();
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    act(() => vi.advanceTimersByTime(300));
    expect(reload).toHaveBeenCalledTimes(1);

    fireEvent.click(button);
    act(() => vi.advanceTimersByTime(300));
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("does not stack auto-refreshes on an unanswered read", () => {
    reload.mockImplementation(() => new Promise(() => {}));
    renderView();
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    fireEvent.click(button);
    act(() => vi.advanceTimersByTime(20000));
    expect(reload).toHaveBeenCalledTimes(1);
    reload.mockImplementation(async () => {});
  });

  it("does not offer auto-refresh for a build that is not running", () => {
    setPlan(makeBuild({ status: "completed" }));
    renderView();
    const button = screen.getByRole("button", { name: "Refresh" });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(screen.getByRole("button", { name: "Refresh" })).toBeInTheDocument();
    expect(reload).toHaveBeenCalledTimes(1);
  });
});

describe("BuildView layout", () => {
  it("stacks nothing about the plan above the DAG and the task table", () => {
    renderView();
    // The plan's scope and lifecycle live in the "Plans and scheduling"
    // dialog (mocked here), not in the main column.
    expect(screen.queryByText("Active plan")).not.toBeInTheDocument();
    expect(screen.queryByText(/^settings /)).not.toBeInTheDocument();
    expect(screen.getByTestId("dag")).toBeInTheDocument();
  });
});

describe("BuildView failure reason", () => {
  it("shows why a failed build failed, above the DAG", () => {
    setPlan(makeBuild({ status: "failed", error_message: "Root failed: boom" }));
    renderView();
    expect(screen.getByRole("alert")).toHaveTextContent("Root failed: boom");
  });

  it("shows no reason for a build that is not failed", () => {
    renderView();
    expect(screen.queryByText("Why this build failed")).not.toBeInTheDocument();
  });
});

describe("BuildView fullscreen graph", () => {
  it("opens the plan graph fullscreen and leaves it on Esc", () => {
    renderView();
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen plan graph" }));
    const overlay = screen.getByRole("dialog", {
      name: "Plan graph, fullscreen",
    });
    expect(overlay).toContainElement(screen.getByTestId("dag"));
    // Drawn once: the inline graph gives way to the overlay.
    expect(screen.getAllByTestId("dag")).toHaveLength(1);

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(screen.getByTestId("dag")).toBeInTheDocument();
  });

  it("leaves fullscreen from its close button", () => {
    renderView();
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen plan graph" }));
    fireEvent.click(screen.getByRole("button", { name: "Exit fullscreen" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("BuildView group-after control", () => {
  it("sits in the plan graph header, and in the fullscreen header", () => {
    renderView();
    const toggle = screen.getByRole("button", { name: "Plan graph" });
    const header = toggle.parentElement!;
    expect(within(header).getByLabelText("Group after:")).toHaveValue(5);
    // Not inside the graph.
    expect(screen.getByTestId("dag")).not.toContainElement(
      screen.getByLabelText("Group after:"),
    );

    fireEvent.click(screen.getByRole("button", { name: "Fullscreen plan graph" }));
    const overlay = screen.getByRole("dialog", {
      name: "Plan graph, fullscreen",
    });
    expect(within(overlay).getByLabelText("Group after:")).toHaveValue(5);
  });
});

describe("BuildView missing plan", () => {
  it("says the active plan is missing rather than drawing a partial one", () => {
    setPlan(makeBuild());
    planState.current = {
      ...planState.current,
      view: { members: [], edges: [] },
      planError: "The build's active plan plan-1 was not found.",
    };
    renderView();
    expect(screen.getByRole("alert")).toHaveTextContent("plan-1 was not found");
    expect(screen.queryByTestId("dag")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Group after:")).not.toBeInTheDocument();
  });
});

describe("BuildView plan graph toggle", () => {
  it("uses v1's rotating chevron as a disclosure control", () => {
    renderView();
    const toggle = screen.getByRole("button", { name: "Plan graph" });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("dag-toggle-chevron").getAttribute("class")).toContain(
      "rotate-90",
    );
    expect(toggle.textContent).not.toMatch(/[▾▸]/);
    // Collapsing goes through react-resizable-panels' imperative API,
    // which jsdom's zero-size layout does not drive; checked in a browser.
  });
});
