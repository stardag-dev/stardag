import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task, TaskStatus } from "../types/task";
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
    expect(screen.getByText(/1 excluded by these filters/)).toBeInTheDocument();
    expect(
      screen.getByText(/will keep running once the build is cancelled/),
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

  it("says when the environment has more claim holders than one page", async () => {
    // Under-reporting here is the dangerous direction: an execution the
    // operator never saw is one that keeps running after the claims go.
    answerWith([makeTask()], 250);
    const user = userEvent.setup();
    renderPanel();
    await user.click(await screen.findByRole("button", { name: /Stop running/ }));

    expect(screen.getByText(/may be incomplete/)).toBeInTheDocument();
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
