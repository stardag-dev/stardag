import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task, TaskStatus } from "../types/task";
import { CLAIM_PAGE_SIZE, MAX_CLAIM_PAGES } from "../utils/stoppable";
import { BuildStopPanel } from "./BuildStopPanel";

vi.mock("../api/tasks", () => ({ fetchTasks: vi.fn() }));

import { fetchTasks } from "../api/tasks";

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

function renderPanel() {
  return render(
    <BuildStopPanel buildId={BUILD} environmentId="env-1" refreshToken={0} />,
  );
}

beforeEach(() => {
  vi.mocked(fetchTasks).mockReset();
});

describe("BuildStopPanel", () => {
  it("is absent when the build holds nothing", async () => {
    // The healthy state, and by far the common one: an empty panel above
    // the DAG would be permanent furniture saying nothing.
    answerWith([makeTask({ latest_status_build_id: OTHER_BUILD })]);
    const { container } = renderPanel();
    await waitFor(() => expect(fetchTasks).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });

  it("asks only for the statuses that may still have a container", async () => {
    answerWith([]);
    renderPanel();
    await waitFor(() => expect(fetchTasks).toHaveBeenCalled());
    expect(vi.mocked(fetchTasks).mock.calls[0][0]).toMatchObject({
      status: ["running", "interrupted"],
      environment_id: "env-1",
    });
  });

  it("lists this build's executions and hands over the command", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();

    await screen.findByText(/1 execution held by this build/);
    await user.click(screen.getByRole("button", { name: /Stop running tasks/ }));

    expect(screen.getByText("GrindBeans")).toBeInTheDocument();
    expect(screen.getByText(`stardag builds stop ${BUILD}`)).toBeInTheDocument();
  });

  it("links each call to its Modal dashboard page for a hard kill", async () => {
    answerWith([makeTask()]);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

    await user.click(screen.getByRole("button", { name: "Copy" }));

    expect(await window.navigator.clipboard.readText()).toBe(
      `stardag builds stop ${BUILD}`,
    );
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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    renderPanel();

    expect(await screen.findByRole("status")).toHaveTextContent(
      /could not be determined here/,
    );
  });

  it("warns when a truncated scan did find some of this build's work", async () => {
    answerWith([makeTask()], MAX_CLAIM_PAGES * CLAIM_PAGE_SIZE + 500);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

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
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

    expect(
      screen.getByText(/run on an executor stardag cannot stop/),
    ).toBeInTheDocument();
  });

  it("reports a read failure rather than looking empty", async () => {
    vi.mocked(fetchTasks).mockRejectedValue(new Error("gateway timeout"));
    renderPanel();
    expect(await screen.findByRole("alert")).toHaveTextContent("gateway timeout");
  });
});
