import { useEffect } from "react";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider, useBreadcrumb } from "../context/BreadcrumbContext";
import type { Build, Task } from "../types/task";

let mockEnvironmentId = "env-1";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: mockEnvironmentId, slug: "default", name: "default" },
  }),
}));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { profile: { sub: "user-1" } } }),
}));

// The graph and the two self-fetching panels are not what this file is
// about, and each drags in a renderer or a request stream of its own.
vi.mock("./DagGraph", () => ({ DagGraph: () => <div data-testid="dag" /> }));
// Counts mounts, so the key can be tested for what it is actually for:
// the dialog clears its scan, filters and ticks by remounting rather
// than by a reset effect.
const controlsMounted = vi.hoisted(() => vi.fn());
// Also captures `onBuildChanged`, so a test can play the part of a
// confirmed override without reaching through the real dialog.
const captureOverride = vi.hoisted(() => vi.fn());
vi.mock("./BuildControlsDialog", () => ({
  BuildControlsDialog: ({
    onBuildChanged,
  }: {
    onBuildChanged: (build: Build) => void;
  }) => {
    useEffect(() => {
      controlsMounted();
      captureOverride(onBuildChanged);
    }, [onBuildChanged]);
    return null;
  },
}));
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

/** The most recent `onBuildChanged` the controls dialog was handed. */
const overrideHandler = () =>
  captureOverride.mock.calls.at(-1)?.[0] as ((build: Build) => void) | undefined;
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
    reactive_app_name: "sd-stop-demo",
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
    mockEnvironmentId = "env-1";
    controlsMounted.mockClear();
    captureOverride.mockClear();
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

  // The executor, reactive flag, scope and config were four coloured
  // pills in the toolbar. They are fixed facts about a build rather than
  // status, and they are now one icon away instead of on screen.
  it("keeps the build's fixed facts out of the toolbar", async () => {
    renderView();
    await screen.findByText("golden-diamond-28");

    expect(screen.queryByText(/Modal: sd-stop-demo/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^reactive$/)).not.toBeInTheDocument();
    expect(screen.queryByText(/^scope:/)).not.toBeInTheDocument();
    // The task count stays: it changes as the build runs.
    expect(screen.getByText("1 task")).toBeInTheDocument();
  });

  it("puts them in the build info dialog, at full length", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchBuild).mockResolvedValue(
      makeBuild({ build_config: { "demo.Parked": { limit: 3 } } }),
    );
    renderView();

    await user.click(await screen.findByRole("button", { name: "Build info" }));

    // The scope key in full, not truncated to something incomparable.
    expect(await screen.findByText("5c6ed85f155d9a01:2b7c")).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: /Modal app sd-stop-demo/ }),
    ).toBeInTheDocument();
    expect(screen.getByText(/Reactive:/)).toBeInTheDocument();
    expect(screen.getByText(/"limit": 3/)).toBeInTheDocument();
  });

  // The button used to disable itself the moment the first click started
  // a fetch, so the second click of the advertised double-click could
  // never land and auto-refresh was unreachable.
  it("reaches auto-refresh on a double-click, mid-fetch", async () => {
    const user = userEvent.setup();
    renderView();
    const refresh = await screen.findByRole("button", { name: "Refresh" });

    // From here every read hangs, which is the state that used to both
    // disable the button and unmount the toolbar behind it.
    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    expect(refresh).toBeEnabled();

    await user.dblClick(refresh);

    expect(
      await screen.findByRole("button", { name: "Stop auto-refreshing" }),
    ).toBeInTheDocument();
  });

  // `kind` decides what the app name is called. Without the guard, any
  // backend that records an app_name was announced as Modal.
  it("does not call a non-Modal executor Modal", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchBuild).mockResolvedValue(
      makeBuild({
        reactive_app_name: null,
        executor_metadata: { kind: "kubernetes", app_name: "batch-runner" },
      }),
    );
    renderView();
    await user.click(await screen.findByRole("button", { name: "Build info" }));

    expect(await screen.findByText(/batch-runner/)).toBeInTheDocument();
    expect(screen.queryByText(/Modal app/)).not.toBeInTheDocument();
  });

  // `reactive_app_name` is set by the reactive-meta endpoint, independently
  // of whatever trigger metadata the build was created with.
  it("reads reactive from the build column, not only from metadata", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchBuild).mockResolvedValue(
      makeBuild({
        reactive_app_name: "sd-ticker",
        executor_metadata: null,
      }),
    );
    renderView();
    await user.click(await screen.findByRole("button", { name: "Build info" }));

    expect(await screen.findByText(/Reactive:/)).toBeInTheDocument();
    expect(screen.getByText(/sd-ticker/)).toBeInTheDocument();
  });

  // Keeping the view across a refresh must not mean keeping it across a
  // change of build: the component stays mounted and holds the previous
  // build's data, so the old DAG and rows would render under the new
  // build's header.
  it("shows the loader when navigating to a different build", async () => {
    const OTHER = "02b1d6d4-0000-7000-8000-000000000000";
    const { rerender } = render(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={BUILD_ID} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );
    expect(await screen.findByText("Parked")).toBeInTheDocument();

    // The next build's read never resolves.
    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={OTHER} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );

    await waitFor(() => expect(screen.queryByText("Parked")).toBeNull());
  });

  // Changing environment restarts the load with the same `buildId`, so
  // an identity check on the build id alone cannot see it — and the
  // component stays mounted across the switch.
  it("shows the loader when the environment changes under the same build", async () => {
    const view = renderView();
    expect(await screen.findByText("Parked")).toBeInTheDocument();

    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    mockEnvironmentId = "env-2";
    view.rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={BUILD_ID} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );

    await waitFor(() => expect(screen.queryByText("Parked")).toBeNull());
  });

  // A slow read for the build you navigated away from used to land,
  // overwrite the build and clear `loading` — after which nothing
  // downstream could tell that the wrong build was on screen.
  it("ignores a superseded read that lands after navigation", async () => {
    const OTHER = "02b1d6d4-0000-7000-8000-000000000000";
    let landFirst: (b: Build) => void = () => {};
    vi.mocked(fetchBuild).mockReturnValue(
      new Promise<Build>((resolve) => {
        landFirst = resolve;
      }) as never,
    );

    const view = renderView();
    // Navigate before the first read has answered.
    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    view.rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={OTHER} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );

    // The abandoned read answers now, with the first build.
    landFirst(makeBuild());
    await waitFor(() => expect(fetchBuild).toHaveBeenCalledTimes(2));

    // It must neither render nor declare the view settled.
    expect(screen.queryByText("golden-diamond-28")).toBeNull();
    expect(screen.queryByText("Parked")).toBeNull();
  });

  // The dialog clears its execution scan, filters and ticks by
  // remounting, so its key has to name everything that invalidates them
  // — the environment as much as the build.
  it("remounts the controls dialog when the environment changes", async () => {
    const view = renderView();
    await screen.findByText("Parked");
    expect(controlsMounted).toHaveBeenCalledTimes(1);

    mockEnvironmentId = "env-2";
    view.rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={BUILD_ID} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );

    await waitFor(() => expect(controlsMounted).toHaveBeenCalledTimes(2));
  });

  // The gesture used to be one-way: the first click of the pair turned
  // auto-refresh off and the second turned it straight back on, so it
  // could be switched on but never off.
  it("turns auto-refresh off again on a second double-click", async () => {
    const user = userEvent.setup();
    renderView();
    const refresh = await screen.findByRole("button", { name: "Refresh" });

    await user.dblClick(refresh);
    const stop = await screen.findByRole("button", { name: "Stop auto-refreshing" });

    await user.dblClick(stop);
    expect(await screen.findByRole("button", { name: "Refresh" })).toBeInTheDocument();
  });

  // The interval has always declined to run on a build that is not
  // running; the toolbar used to light up and claim otherwise.
  it("does not offer auto-refresh on a build that has stopped", async () => {
    const user = userEvent.setup();
    vi.mocked(fetchBuild).mockResolvedValue(makeBuild({ status: "failed" }));
    renderView();
    const refresh = await screen.findByRole("button", { name: "Refresh" });

    await user.dblClick(refresh);

    expect(screen.queryByRole("button", { name: "Stop auto-refreshing" })).toBeNull();
    expect(refresh).not.toHaveAccessibleDescription(/every 5 seconds/i);
  });

  // A pending single click holds the closure of the identity it was
  // aimed at. Firing after navigation does not merely waste a request:
  // it bumps the shared load epoch, discarding the load that is
  // legitimately in flight, then applies its own older answer.
  it("drops a pending click when the build changes under it", async () => {
    const OTHER = "02b1d6d4-0000-7000-8000-000000000000";
    const user = userEvent.setup();
    const view = renderView();
    const refresh = await screen.findByRole("button", { name: "Refresh" });

    // One click: the action is deferred for the double-click window.
    await user.click(refresh);
    const callsBefore = vi.mocked(fetchBuild).mock.calls.length;

    view.rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={OTHER} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );

    // Past the 300ms window, the abandoned click must not have fired a
    // refresh of its own on top of the new build's load.
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(vi.mocked(fetchBuild).mock.calls.length).toBe(callsBefore + 1);
    expect(vi.mocked(fetchBuild).mock.calls.at(-1)?.[0]).toBe(OTHER);
  });

  // `refreshing` was a state snapshot each caller cleared on its own
  // completion, so an older one finishing could unlock the door for a
  // newer request while one was still in flight.
  it("runs one refresh at a time", async () => {
    const user = userEvent.setup();
    renderView();
    const refresh = await screen.findByRole("button", { name: "Refresh" });

    // Reads from here never answer, so the first refresh stays in flight.
    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    await user.click(refresh);
    await waitFor(() => expect(fetchBuild).toHaveBeenCalledTimes(2));

    await user.click(refresh);
    await new Promise((resolve) => setTimeout(resolve, 450));
    expect(fetchBuild).toHaveBeenCalledTimes(2);
  });

  // A read issued before the override carries the pre-override record,
  // and because it is for the same identity the epoch still calls it
  // fresh — so it lands afterwards and reverts what the user just set.
  it("does not let a read that predates an override undo it", async () => {
    let landStaleRead: (b: Build) => void = () => {};
    const user = userEvent.setup();
    renderView();
    await screen.findByText("golden-diamond-28");

    // A refresh is in flight, holding the pre-override record.
    vi.mocked(fetchBuild).mockReturnValue(
      new Promise<Build>((resolve) => {
        landStaleRead = resolve;
      }) as never,
    );
    await user.click(await screen.findByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchBuild).toHaveBeenCalledTimes(2));

    // The override lands first. Invoked through the callback the
    // controls dialog is handed, which is the seam that matters here.
    await act(async () => {
      overrideHandler()?.(makeBuild({ status: "failed" }));
    });
    // The build's own status lives in the breadcrumb; the task table
    // has statuses of its own, so assert on the crumb, not the page.
    await waitFor(() => expect(crumbs()[1]).toHaveTextContent("failed"));

    // Now the older read answers, with the build still running.
    await act(async () => {
      landStaleRead(makeBuild({ status: "running" }));
      await Promise.resolve();
    });

    expect(crumbs()[1]).toHaveTextContent("failed");
    expect(crumbs()[1]).not.toHaveTextContent("running");
  });

  // `refreshing` is what the in-flight marker exists to drive, so it
  // has to be cleared under the same ownership check — otherwise an
  // abandoned refresh settling stops the icon while the current
  // identity's refresh is still running.
  it("keeps the refresh icon spinning when an abandoned refresh settles", async () => {
    const OTHER = "02b1d6d4-0000-7000-8000-000000000000";
    const spinning = () =>
      Boolean(
        screen
          .getByRole("button", { name: "Refresh" })
          .querySelector("svg")
          ?.getAttribute("class")
          ?.includes("animate-spin"),
      );

    const user = userEvent.setup();
    const view = renderView();
    await screen.findByText("golden-diamond-28");

    // Build A's refresh is left in flight.
    let landA: (b: Build) => void = () => {};
    vi.mocked(fetchBuild).mockReturnValue(
      new Promise<Build>((resolve) => {
        landA = resolve;
      }) as never,
    );
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(fetchBuild).toHaveBeenCalledTimes(2));

    // Navigate to B, which loads fine.
    vi.mocked(fetchBuild).mockResolvedValue(makeBuild({ id: OTHER }));
    view.rerender(
      <BreadcrumbProvider>
        <CrumbProbe />
        <BuildView buildId={OTHER} onBack={vi.fn()} />
      </BreadcrumbProvider>,
    );
    await waitFor(() => expect(spinning()).toBe(false));

    // Now B has a refresh of its own in flight.
    vi.mocked(fetchBuild).mockReturnValue(new Promise(() => {}) as never);
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(spinning()).toBe(true));

    // A's abandoned read finally answers. It must not stop B's spinner.
    await act(async () => {
      landA(makeBuild());
      await Promise.resolve();
    });
    expect(spinning()).toBe(true);
  });

  it("says nothing about a config the build never set", async () => {
    const user = userEvent.setup();
    renderView();
    await user.click(await screen.findByRole("button", { name: "Build info" }));
    expect(screen.queryByText(/Build config/)).not.toBeInTheDocument();
  });
});
