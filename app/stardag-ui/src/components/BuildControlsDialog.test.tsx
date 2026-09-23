import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BuildStatus, Task, TaskStatus } from "../types/task";
import { CLAIM_PAGE_SIZE, MAX_CLAIM_PAGES } from "../utils/stoppable";
import { BuildControlsDialog, MAX_ROWS_DRAWN } from "./BuildControlsDialog";

vi.mock("../api/tasks", () => ({
  fetchTasks: vi.fn(),
  cancelBuild: vi.fn(),
  completeBuild: vi.fn(),
  failBuild: vi.fn(),
}));

vi.mock("../context/AuthContext", () => ({
  useAuth: () => ({ user: { profile: { sub: "user-1" } } }),
}));

import { cancelBuild, completeBuild, failBuild, fetchTasks } from "../api/tasks";

const BUILD = "11111111-1111-1111-1111-111111111111";
const OTHER_BUILD = "22222222-2222-2222-2222-222222222222";

const MINUTE = 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    id: "row-1",
    task_id: "tid-grind-beans",
    environment_id: "env-1",
    task_namespace: "",
    task_name: "GrindBeans",
    task_data: {},
    version: null,
    output_uri: null,
    created_at: ago(90 * MINUTE),
    status: "running" as TaskStatus,
    started_at: ago(60 * MINUTE),
    completed_at: null,
    error_message: null,
    artifact_count: 0,
    latest_status: "running",
    latest_status_at: ago(20 * MINUTE),
    latest_status_build_id: BUILD,
    latest_executor: "modal",
    latest_executor_ref: "fc-abc123",
    latest_executor_metadata: {
      kind: "modal",
      workspace: "acme",
      app_name: "pipeline",
      app_id: "ap-1",
      function_id: "fu-1",
      function_name: "worker_gpu",
    },
    ...overrides,
  };
}

/**
 * Serve `tasks` as page 1 and claim a `total`.
 *
 * `total` larger than what is served is how a truncated scan is simulated:
 * the panel keeps asking for pages, and this keeps handing back the same
 * one, which is exactly the shape of an environment with more claim
 * holders than the panel will walk.
 */
function answerWith(tasks: Task[], total = tasks.length) {
  vi.mocked(fetchTasks).mockResolvedValue({
    tasks,
    total,
    page: 1,
    page_size: 100,
  });
}

const onBuildChanged = vi.fn();

function renderPanel(buildStatus: BuildStatus = "running", holdsClaims = true) {
  return render(
    <BuildControlsDialog
      buildId={BUILD}
      environmentId="env-1"
      buildStatus={buildStatus}
      holdsClaims={holdsClaims}
      refreshToken={0}
      onBuildChanged={onBuildChanged}
    />,
  );
}

/**
 * Render, then open the dialog.
 *
 * Nothing is fetched until it is open — that is the point of the dialog
 * (STA-83), so every test that wants a list has to open one first.
 */
async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  renderPanel();
  await user.click(screen.getByRole("button", { name: "Build controls" }));
  await waitFor(() => expect(fetchTasks).toHaveBeenCalled());
}

beforeEach(() => {
  vi.mocked(fetchTasks).mockReset();
  vi.mocked(cancelBuild).mockReset();
  vi.mocked(completeBuild).mockReset();
  vi.mocked(failBuild).mockReset();
  onBuildChanged.mockReset();
});

describe("BuildControlsDialog", () => {
  it("says nothing is running rather than showing an empty dialog", async () => {
    // As a band above the DAG this rendered nothing at all, which was
    // right for something that appeared unbidden. In a dialog somebody
    // opened on purpose, silence reads as a broken dialog.
    answerWith([makeTask({ latest_status_build_id: OTHER_BUILD })]);
    const user = userEvent.setup();
    await openDialog(user);
    expect(await screen.findByText(/holding no execution claims/i)).toBeInTheDocument();
  });

  it("fetches nothing until the dialog is opened", async () => {
    // The scan is up to 20 sequential requests and it used to run on
    // every 5s auto-refresh, drawing nothing (STA-83).
    answerWith([makeTask()]);
    renderPanel();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Build controls" }),
      ).toBeInTheDocument(),
    );
    expect(fetchTasks).not.toHaveBeenCalled();
  });

  it("offers no way in on a completed build", async () => {
    answerWith([makeTask()]);
    const { container } = renderPanel("completed");
    expect(container).toBeEmptyDOMElement();
    expect(fetchTasks).not.toHaveBeenCalled();
  });

  it("asks only for the statuses that may still have a container", async () => {
    answerWith([]);
    const user = userEvent.setup();
    await openDialog(user);
    expect(vi.mocked(fetchTasks).mock.calls[0][0]).toMatchObject({
      status: ["running", "interrupted"],
      environment_id: "env-1",
    });
  });

  it("lists this build's executions and hands over the command", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();

    await user.click(screen.getByRole("button", { name: "Build controls" }));

    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    expect(screen.getByText(`stardag builds stop ${BUILD}`)).toBeInTheDocument();
  });

  it("links each call to its Modal dashboard page for a hard kill", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    const link = screen.getByRole("link", { name: "fc-abc123" });
    expect(link).toHaveAttribute("href", expect.stringContaining("modal.com"));
    expect(link).toHaveAttribute("href", expect.stringContaining("fc-abc123"));
  });

  it("puts the worker filter into the command it hands over", async () => {
    // The panel's contract: the command acts on exactly the list on
    // screen. If the filter narrowed one and not the other, showing both
    // would be worse than showing neither.
    answerWith([
      makeTask({
        task_id: "gpu-task",
        task_name: "Featurise",
        latest_executor_metadata: { function_name: "worker_gpu" },
      }),
      makeTask({
        task_id: "cpu-task",
        task_name: "Aggregate",
        latest_executor_metadata: { function_name: "worker_cpu" },
      }),
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    await user.selectOptions(screen.getByLabelText("Worker"), "gpu");

    expect(screen.getByText("Featurise")).toBeInTheDocument();
    expect(screen.queryByText("Aggregate")).not.toBeInTheDocument();
    expect(
      screen.getByText(`stardag builds stop ${BUILD} --worker gpu`),
    ).toBeInTheDocument();
    expect(screen.getByText(/1 not selected/)).toBeInTheDocument();
    expect(
      screen.getByText(/will keep running once the build is cancelled/),
    ).toBeInTheDocument();
  });

  it("puts the executor filter into the command too", async () => {
    // Parity with the CLI's own flags: every filter the panel offers has
    // to be expressible in the command it hands over, or the two stop
    // describing the same set.
    answerWith([
      makeTask({ task_id: "on-modal", task_name: "Featurise" }),
      makeTask({
        task_id: "elsewhere",
        task_name: "Aggregate",
        latest_executor: "prefect",
      }),
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    await user.selectOptions(screen.getByLabelText("Executor"), "modal");

    expect(screen.getByText("Featurise")).toBeInTheDocument();
    expect(screen.queryByText("Aggregate")).not.toBeInTheDocument();
    expect(
      screen.getByText(`stardag builds stop ${BUILD} --executor modal`),
    ).toBeInTheDocument();
  });

  it("offers no executor filter when there is only one", async () => {
    // One executor is not a choice, it is a fact the table already states.
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    expect(screen.queryByLabelText("Executor")).not.toBeInTheDocument();
  });

  it("offers no command when the ticked rows are filtered away", async () => {
    // The regression this pins is a widening, which is the worst
    // direction: ticks survive a filter change, so the dropdowns can be
    // moved until no ticked row is shown. "No ids" is not a narrower
    // request — a command with no filters stops *everything* the build
    // holds, so the operator who ticked one row would copy a command that
    // kills all of them.
    answerWith([
      makeTask({
        task_id: "gpu-task",
        task_name: "Featurise",
        latest_executor_metadata: { function_name: "worker_gpu" },
      }),
      makeTask({
        task_id: "cpu-task",
        task_name: "Aggregate",
        latest_executor_metadata: { function_name: "worker_cpu" },
      }),
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    await user.click(screen.getByRole("checkbox", { name: /Include Featurise/ }));
    expect(
      screen.getByText(`stardag builds stop ${BUILD} --task-id gpu-task`),
    ).toBeInTheDocument();

    // Now filter the ticked row out of view.
    await user.selectOptions(screen.getByLabelText("Worker"), "cpu");

    expect(screen.queryByText(`stardag builds stop ${BUILD}`)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Copy" })).not.toBeInTheDocument();
    expect(
      screen.getByText(/None of the rows you ticked match these filters/),
    ).toBeInTheDocument();
  });

  it("copies the command as shown", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await window.navigator.clipboard.readText()).toBe(
      `stardag builds stop ${BUILD}`,
    );
  });

  it("does not leave the copied-flash timer running after unmount", async () => {
    // Unmounting mid-flash would set state on a dead component; a second
    // copy inside the flash would let the first timer clear the label
    // early. One tracked timer, cleared on both paths.
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      answerWith([makeTask()]);
      const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
      const { unmount } = renderPanel();
      await user.click(await screen.findByRole("button", { name: "Build controls" }));
      await user.click(screen.getByRole("button", { name: "Copy" }));
      expect(screen.getByRole("button", { name: "Copied" })).toBeInTheDocument();

      unmount();
      // Would warn about a state update on an unmounted component if the
      // timer still fired into the dead tree.
      vi.advanceTimersByTime(5000);
    } finally {
      vi.useRealTimers();
    }
  });

  it("pages until the server says it is done", async () => {
    // One page is not enough: a build's executions can sit entirely on
    // later pages, and finding none is what this panel renders as absent.
    vi.mocked(fetchTasks)
      .mockResolvedValueOnce({
        tasks: Array.from({ length: 100 }, (_, i) =>
          makeTask({ task_id: `other-${i}`, latest_status_build_id: OTHER_BUILD }),
        ),
        total: 101,
        page: 1,
        page_size: 100,
      })
      .mockResolvedValueOnce({
        tasks: [makeTask({ task_name: "OnPageTwo" })],
        total: 101,
        page: 2,
        page_size: 100,
      });

    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    expect(vi.mocked(fetchTasks).mock.calls.map((c) => c[0]?.page)).toEqual([1, 2]);
    expect(screen.getByText("OnPageTwo")).toBeInTheDocument();
  });

  it("says so rather than vanishing when the scan gives up early", async () => {
    // The dangerous direction: finding nothing and having stopped looking
    // are the same screen otherwise, and one of them is a build whose live
    // executions nobody was shown.
    answerWith(
      [makeTask({ latest_status_build_id: OTHER_BUILD })],
      MAX_CLAIM_PAGES * CLAIM_PAGE_SIZE + 500,
    );
    const user = userEvent.setup();
    await openDialog(user);

    expect(await screen.findByRole("status")).toHaveTextContent(
      /could not be determined here/,
    );
  });

  it("warns when a truncated scan did find some of this build's work", async () => {
    answerWith([makeTask()], MAX_CLAIM_PAGES * CLAIM_PAGE_SIZE + 500);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    expect(screen.getByText(/may be incomplete/)).toBeInTheDocument();
  });

  it("ticking rows narrows the command to those task ids", async () => {
    // Parity with the CLI's repeatable --task-id, and the reason ids
    // replace the other flags: they name the set exactly on their own.
    answerWith([
      makeTask({ task_id: "keep-me", task_name: "Featurise" }),
      makeTask({ task_id: "leave-me", task_name: "Aggregate" }),
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    // Nothing ticked means the command targets everything listed.
    expect(screen.getByText(`stardag builds stop ${BUILD}`)).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /Include Featurise/ }));

    expect(
      screen.getByText(`stardag builds stop ${BUILD} --task-id keep-me`),
    ).toBeInTheDocument();
    // The row that was not ticked stays on screen — a selection whose
    // alternatives are off-screen is not a selection.
    expect(screen.getByText("Aggregate")).toBeInTheDocument();
    expect(screen.getByText(/1 not selected/)).toBeInTheDocument();
  });

  it("marks an execution stardag cannot stop rather than hiding it", async () => {
    answerWith([makeTask({ latest_executor: "prefect" })]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    expect(
      screen.getByText(/run on an executor stardag cannot stop/),
    ).toBeInTheDocument();
  });

  it("lists a claim whose spawn has not reported a call id yet", async () => {
    // STA-88, the panel half. The row is RUNNING and held by this build
    // from the moment it is claimed, which is before its container
    // exists; dropping it until the ref arrived made the panel silently
    // short during exactly the fan-out somebody opens it to look at.
    answerWith([makeTask({ latest_executor: null, latest_executor_ref: null })]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    expect(screen.getByText("not recorded yet")).toBeInTheDocument();
    expect(
      screen.getByText(/Rows without a call id exit at their next checkpoint/),
    ).toBeInTheDocument();
    // The pending wording, not the other-executor one.
    expect(screen.queryByText(/executor stardag cannot stop/)).toBe(null);
  });

  it("does not call an unattributed row permanently unstoppable", async () => {
    // No executor, no ref, no metadata. Written by a non-detached
    // execution *and* by a Modal claim whose best-effort metadata lookup
    // returned None, so neither verdict is safe.
    answerWith([
      makeTask({
        latest_executor: null,
        latest_executor_ref: null,
        latest_executor_metadata: null,
      }),
    ]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: "Build controls" }));

    // Ambiguous, so it is grouped with the pending rows and the wording
    // names both possibilities rather than promising a call id.
    expect(
      screen.getByText(/Rows without a call id exit at their next checkpoint/),
    ).toBeInTheDocument();
    expect(screen.getByText("not recorded yet")).toBeInTheDocument();
    expect(screen.queryByText(/executor stardag cannot stop/)).toBe(null);
  });

  it("reports a read failure rather than looking empty", async () => {
    vi.mocked(fetchTasks).mockRejectedValue(new Error("gateway timeout"));
    const user = userEvent.setup();
    await openDialog(user);
    expect(await screen.findByRole("alert")).toHaveTextContent("gateway timeout");
  });

  // The warning is the reason these two controls share a dialog. It is
  // driven by the build's own task list now, so it does not depend on
  // whether the stop scan has answered — it is right from first paint.
  it("warns before the stop scan has answered", async () => {
    vi.mocked(fetchTasks).mockReturnValue(new Promise(() => {}) as never);
    const user = userEvent.setup();
    renderPanel("running", true);
    await user.click(screen.getByRole("button", { name: "Build controls" }));

    await user.click(await screen.findByRole("button", { name: "Cancel build" }));
    expect(screen.getByText(/cancelling here will not stop them/i)).toBeInTheDocument();
  });

  // As a panel, vanishing on `completed` was invisible. As a dialog
  // somebody is reading, it is not — and one way to reach `completed`
  // is to mark it so from this very dialog.
  it("stays open when the build reaches a finished status", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    const view = renderPanel();
    await user.click(screen.getByRole("button", { name: "Build controls" }));
    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();

    view.rerender(
      <BuildControlsDialog
        buildId={BUILD}
        environmentId="env-1"
        buildStatus="completed"
        holdsClaims
        refreshToken={0}
        onBuildChanged={onBuildChanged}
      />,
    );

    expect(screen.getByText("GrindBeans")).toBeInTheDocument();
  });

  it("confirms an override where the override section cannot", async () => {
    answerWith([makeTask()]);
    vi.mocked(failBuild).mockResolvedValue({
      id: BUILD,
      status: "failed",
    } as never);
    const user = userEvent.setup();
    await openDialog(user);

    await user.click(await screen.findByRole("button", { name: "Mark failed" }));
    await user.click(screen.getByRole("button", { name: "Mark failed" }));

    // The section itself unmounts once the status is no longer
    // overridable, so the confirmation has to live outside it.
    expect(await screen.findByText(/now recorded as failed/i)).toBeInTheDocument();
    expect(screen.getByText(/Nothing running was stopped/i)).toBeInTheDocument();
  });

  // Auto-refresh re-runs the effect every 5s and a scan is up to 20
  // sequential requests with no cancellation.
  it("does not start a second scan while one is in flight", async () => {
    vi.mocked(fetchTasks).mockReturnValue(new Promise(() => {}) as never);
    const user = userEvent.setup();
    const view = renderPanel();
    await user.click(screen.getByRole("button", { name: "Build controls" }));
    await waitFor(() => expect(fetchTasks).toHaveBeenCalledTimes(1));

    for (const token of [1, 2, 3]) {
      view.rerender(
        <BuildControlsDialog
          buildId={BUILD}
          environmentId="env-1"
          buildStatus="running"
          holdsClaims
          refreshToken={token}
          onBuildChanged={onBuildChanged}
        />,
      );
    }

    expect(fetchTasks).toHaveBeenCalledTimes(1);
  });

  // With no filter set, rows falling out of the selection means they
  // stopped running — telling the user to widen filters they never set
  // sends them looking for a control that is already at "all".
  it("says the ticked rows finished, not that the filters are too narrow", async () => {
    answerWith([
      makeTask({ task_id: "gone", task_name: "Featurise" }),
      makeTask({ task_id: "stays", task_name: "Aggregate" }),
    ]);
    const user = userEvent.setup();
    const view = renderPanel();
    await user.click(screen.getByRole("button", { name: "Build controls" }));
    await user.click(
      await screen.findByRole("checkbox", { name: /Include Featurise/ }),
    );

    // The rescan no longer lists the ticked one: it completed. Another
    // execution is still running, so the list itself is not empty.
    answerWith([makeTask({ task_id: "stays", task_name: "Aggregate" })]);
    view.rerender(
      <BuildControlsDialog
        buildId={BUILD}
        environmentId="env-1"
        buildStatus="running"
        holdsClaims
        refreshToken={1}
        onBuildChanged={onBuildChanged}
      />,
    );

    expect(
      await screen.findByText(/no longer running, so there is nothing to stop/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/widen the filters/i)).toBeNull();
  });

  // --- Overriding the recorded status ---
  //
  // These live in the same dialog as the stop list because the two were
  // confusable while they were separate controls: on a build you wanted
  // stopped, "Cancel" looked like the answer, and it releases the claims
  // while every container runs on.

  // #375 pinned this copy to say *nothing* about claims, because the
  // behaviour was mid-flight: STA-81 was about to make a terminal
  // transition release them, and no sentence was true on both sides. It
  // has landed, so the honest constraint is no longer silence but
  // accuracy — each action names the claims exactly when it changes
  // them. Same discipline, a settled fact to attach it to.
  it("names the claims exactly where the action changes them", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    await openDialog(user);

    const override = () =>
      screen.getByRole("region", { name: /Record an outcome instead/ });

    await user.click(await screen.findByRole("button", { name: "Cancel build" }));
    expect(override().textContent ?? "").toMatch(/releases the build's claims/i);
    await user.click(screen.getByRole("button", { name: "Back" }));

    await user.click(screen.getByRole("button", { name: "Mark failed" }));
    expect(override().textContent ?? "").toMatch(/releases the claims/i);
    await user.click(screen.getByRole("button", { name: "Back" }));
  });

  // The one terminal override that releases nothing (STA-103), so it is
  // withheld unless the scan has positively said nothing is running.
  it("withholds Mark completed while the build holds claims", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    await openDialog(user);

    expect(screen.queryByRole("button", { name: "Mark completed" })).toBeNull();
    expect(
      screen.getByText(/is the one outcome that releases no claims/i),
    ).toBeInTheDocument();
  });

  // The gate reads the build's own task list, not the stop scan — which
  // asks for the *stoppable* statuses, so a SUSPENDED task holds a claim
  // it cannot see, and which can truncate besides. A build whose only
  // task is suspended has an empty stop list and a held claim.
  it("withholds Mark completed for a claim the stop list cannot see", async () => {
    answerWith([]);
    const user = userEvent.setup();
    renderPanel("running", true);
    await user.click(screen.getByRole("button", { name: "Build controls" }));

    expect(await screen.findByText(/holding no execution claims/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark completed" })).toBeNull();
    expect(
      screen.getByText(/is the one outcome that releases no claims/i),
    ).toBeInTheDocument();
  });

  it("offers Mark completed once the build holds no claims", async () => {
    answerWith([makeTask({ latest_status_build_id: OTHER_BUILD })]);
    const user = userEvent.setup();
    renderPanel("running", false);
    await user.click(screen.getByRole("button", { name: "Build controls" }));

    expect(
      await screen.findByRole("button", { name: "Mark completed" }),
    ).toBeInTheDocument();
  });

  it("warns that cancelling does not stop what is running", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    await openDialog(user);

    // The section's own line carries the non-effect for every action;
    // the per-action warning carries the one that only bites while
    // something is actually running.
    expect(screen.getByText(/nothing running is stopped/i)).toBeInTheDocument();

    await user.click(await screen.findByRole("button", { name: "Cancel build" }));
    expect(screen.getByText(/cancelling here will not stop them/i)).toBeInTheDocument();
    expect(
      screen.getByText(
        /ends the selected containers first and cancels the build afterwards/i,
      ),
    ).toBeInTheDocument();
    expect(cancelBuild).not.toHaveBeenCalled();
  });

  // The old copy said in words that no override was needed alongside the
  // command. The structure says it now — stop first, record second — so
  // what has to hold is that the stop line states the build is cancelled
  // and its claims released, which is what makes an override redundant.
  it("says the command cancels the build and releases its claims", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    await openDialog(user);

    expect(
      await screen.findByText(
        /then cancels the build and releases every claim it holds/i,
      ),
    ).toBeInTheDocument();
  });

  it("does not warn about running work when the build holds no claims", async () => {
    answerWith([makeTask({ latest_status_build_id: OTHER_BUILD })]);
    const user = userEvent.setup();
    renderPanel("running", false);
    await user.click(screen.getByRole("button", { name: "Build controls" }));

    await user.click(await screen.findByRole("button", { name: "Cancel build" }));
    expect(screen.queryByText(/cancelling here will not stop them/i)).toBeNull();
  });

  it("overrides only after the second, confirming click", async () => {
    answerWith([makeTask()]);
    vi.mocked(failBuild).mockResolvedValue({ id: BUILD } as never);
    const user = userEvent.setup();
    await openDialog(user);

    await user.click(await screen.findByRole("button", { name: "Mark failed" }));
    expect(failBuild).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Mark failed" }));
    await waitFor(() =>
      expect(failBuild).toHaveBeenCalledWith(BUILD, "env-1", "user-1"),
    );
    expect(onBuildChanged).toHaveBeenCalled();
  });

  it("offers no override on a build whose record is already final", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel("failed");
    await user.click(screen.getByRole("button", { name: "Build controls" }));

    expect(screen.queryByRole("button", { name: "Cancel build" })).toBeNull();
    // The stop half is still there: a failed build's containers run on.
    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
  });

  // STA-83: rendering was uncapped while fetching was paginated, so a wide
  // fan-out put every execution in the DOM and pushed the page around.
  it("caps the rows it draws and says how many it left out", async () => {
    const many = Array.from({ length: MAX_ROWS_DRAWN + 12 }, (_, i) =>
      makeTask({ id: `row-${i}`, task_id: `tid-${i}`, task_name: `Task${i}` }),
    );
    answerWith(many);
    const user = userEvent.setup();
    await openDialog(user);

    expect(await screen.findByText("Task0")).toBeInTheDocument();
    expect(screen.getByText(`Task${MAX_ROWS_DRAWN - 1}`)).toBeInTheDocument();
    expect(screen.queryByText(`Task${MAX_ROWS_DRAWN}`)).toBe(null);
    expect(screen.getByText(/12 more executions not listed/)).toBeInTheDocument();
  });

  // Ticking switches the command to exact task ids, so the undrawn rows
  // stop being included — and saying otherwise errs towards "everything
  // is covered", which is the dangerous direction.
  it("stops claiming the undrawn rows are covered once rows are ticked", async () => {
    answerWith(
      Array.from({ length: MAX_ROWS_DRAWN + 12 }, (_, i) =>
        makeTask({ id: `row-${i}`, task_id: `tid-${i}`, task_name: `Task${i}` }),
      ),
    );
    const user = userEvent.setup();
    await openDialog(user);

    expect(
      await screen.findByText(/still targets every one of them/),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "Include Task0" }));

    expect(screen.queryByText(/still targets every one of them/)).toBeNull();
    expect(screen.getByText(/these are not included/)).toBeInTheDocument();
  });

  // The command is unaffected by how much of the list is drawn.
  it("still targets every execution when rows are left undrawn", async () => {
    answerWith(
      Array.from({ length: MAX_ROWS_DRAWN + 5 }, (_, i) =>
        makeTask({ id: `row-${i}`, task_id: `tid-${i}`, task_name: `Task${i}` }),
      ),
    );
    const user = userEvent.setup();
    await openDialog(user);

    expect(await screen.findByText(`stardag builds stop ${BUILD}`)).toBeInTheDocument();
  });
});
