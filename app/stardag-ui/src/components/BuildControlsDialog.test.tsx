import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { BuildStatus, Execution } from "../types/task";

vi.mock("../api/registry", () => ({
  fetchBuildExecutions: vi.fn(),
  cancelBuild: vi.fn(),
  completeBuild: vi.fn(),
  failBuild: vi.fn(),
}));

import { cancelBuild, fetchBuildExecutions } from "../api/registry";
import { BuildControlsDialog } from "./BuildControlsDialog";
import type { ExecutionTaskInfo } from "./ExecutionTable";
import { MAX_ROWS_DRAWN } from "./ExecutionTable";

const BUILD = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const MINUTE = 60_000;

function execution(
  id: string,
  taskId: string,
  inCurrentPlan: boolean,
  overrides: Partial<Execution> = {},
): Execution {
  return {
    id,
    task_id: taskId,
    build_id: BUILD,
    plan_id: inCurrentPlan ? "p-2" : "p-1",
    instance_id: `i-${id}`,
    executor: "modal",
    executor_ref: `fc-${id}`,
    executor_metadata: {
      kind: "modal",
      workspace: "acme",
      app_name: "pipeline",
      app_id: "ap-1",
      function_id: "fu-1",
      function_name: "worker_gpu",
    },
    started_at: new Date(Date.now() - MINUTE).toISOString(),
    claim_released_at: inCurrentPlan ? null : new Date().toISOString(),
    claim_outcome: inCurrentPlan ? null : "taken_over",
    ended_at: null,
    outcome: null,
    in_current_plan: inCurrentPlan,
    ...overrides,
  };
}

const TASK_INFO = new Map<string, ExecutionTaskInfo>([
  ["task-aaaaaaaa", { namespace: "demo", name: "GrindBeans", status: "running" }],
  ["task-bbbbbbbb", { namespace: "demo", name: "Brew", status: "running" }],
]);

function renderDialog(buildStatus: BuildStatus = "running") {
  return render(
    <BuildControlsDialog
      buildId={BUILD}
      environmentId="env-1"
      buildStatus={buildStatus}
      onBuildChanged={vi.fn()}
      taskInfo={TASK_INFO}
    />,
  );
}

async function open(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Build controls" }));
}

const command = () => screen.getByText(/^stardag builds stop /);

beforeEach(() => {
  vi.mocked(fetchBuildExecutions).mockReset();
  vi.mocked(cancelBuild).mockReset();
});

describe("BuildControlsDialog stop list", () => {
  it("lists unended executions, marks orphans, and narrows the command to them", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", false),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(await screen.findByText("orphan")).toBeInTheDocument();
    expect(screen.getByText("claim taken over")).toBeInTheDocument();
    expect(command()).toHaveTextContent(`stardag builds stop ${BUILD}`);

    await user.click(screen.getByLabelText("Orphans only (not in the current plan)"));
    expect(command()).toHaveTextContent(
      `stardag builds stop ${BUILD} --not-in-current-plan`,
    );
    expect(screen.queryByText("holds the claim")).not.toBeInTheDocument();
  });

  it("says what the printed command does, per mode", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", false),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(await screen.findByText(/then cancels the build/)).toBeInTheDocument();

    await user.click(screen.getByLabelText("Orphans only (not in the current plan)"));
    expect(screen.queryByText(/then cancels the build/)).not.toBeInTheDocument();
    expect(screen.getByText(/does not cancel the build/)).toBeInTheDocument();
  });

  it("says so when nothing is left to stop", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([]);
    const user = userEvent.setup();
    renderDialog("failed");
    await open(user);
    expect(await screen.findByText(/nothing to stop/)).toBeInTheDocument();
  });

  it("fetches nothing until the dialog is opened", () => {
    renderDialog();
    expect(fetchBuildExecutions).not.toHaveBeenCalled();
  });

  it("names each task and its status, not a short id", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      // A task the active plan does not hold (an orphan's): short id.
      execution("e2", "task-cccccccc-dddd", false),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Status" })).toBeVisible();
    expect(within(table).getByText("demo.GrindBeans")).toBeInTheDocument();
    expect(within(table).getByText("running")).toBeInTheDocument();
    expect(within(table).getByText("task-ccc")).toBeInTheDocument();
    expect(screen.getByLabelText("Include demo.GrindBeans")).toBeInTheDocument();
  });

  it("links each call to its Modal dashboard page for a hard kill", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    const link = await screen.findByRole("link", { name: "fc-e1" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link.getAttribute("href")).toContain("fc-e1");
  });

  it("puts the worker filter into the command it hands over", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", true, {
        executor_metadata: { kind: "modal", function_name: "worker_cpu" },
      }),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await user.selectOptions(await screen.findByLabelText("Worker"), "gpu");
    expect(command()).toHaveTextContent(`stardag builds stop ${BUILD} --worker gpu`);
    expect(screen.getByText("1 not selected")).toBeInTheDocument();
  });

  it("puts the executor filter into the command too", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", true, {
        executor: "local",
        executor_metadata: null,
      }),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await user.selectOptions(await screen.findByLabelText("Executor"), "modal");
    expect(command()).toHaveTextContent(
      `stardag builds stop ${BUILD} --executor modal`,
    );
  });

  it("offers no executor filter when there is only one", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", true),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await screen.findByRole("table");
    expect(screen.queryByLabelText("Executor")).not.toBeInTheDocument();
  });

  it("ticking rows narrows the command to those task ids", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", true),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(
      await screen.findByText(/Nothing ticked — the command below targets every/),
    ).toBeInTheDocument();
    await user.click(screen.getByLabelText("Include demo.Brew"));
    expect(command()).toHaveTextContent(
      `stardag builds stop ${BUILD} --task-id task-bbbbbbbb`,
    );
    expect(screen.getByText("The command below names the 1 you ticked.")).toBeVisible();
    expect(screen.getByText("1 not selected")).toBeInTheDocument();
    expect(
      screen.getByText(
        /The 1 execution it does not name will keep running once the build/,
      ),
    ).toBeInTheDocument();
  });

  it("offers no command when the ticked rows are filtered away", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", false),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await user.click(await screen.findByLabelText("Include demo.GrindBeans"));
    await user.click(screen.getByLabelText("Orphans only (not in the current plan)"));
    expect(
      screen.getByText(/None of the rows you ticked match these filters/),
    ).toBeInTheDocument();
    expect(screen.getByText(/one with no targets would stop everything/)).toBeVisible();
    expect(screen.queryByText(/^stardag builds stop /)).not.toBeInTheDocument();
  });

  it("says the ticked rows ended, not that the filters are too narrow", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValueOnce([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", true),
    ]);
    const user = userEvent.setup();
    const view = renderDialog();
    await open(user);
    await user.click(await screen.findByLabelText("Include demo.Brew"));
    // The next read no longer lists the ticked row: it reported an end.
    vi.mocked(fetchBuildExecutions).mockResolvedValueOnce([
      execution("e1", "task-aaaaaaaa", true),
    ]);
    view.rerender(
      <BuildControlsDialog
        buildId={BUILD}
        environmentId="env-1"
        buildStatus="running"
        onBuildChanged={vi.fn()}
        taskInfo={TASK_INFO}
        refreshToken={1}
      />,
    );
    expect(
      await screen.findByText(/The executions you ticked are no longer listed/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/match these filters/)).not.toBeInTheDocument();
  });

  it("copies the command as shown", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
    ]);
    const user = userEvent.setup();
    const writeText = vi.spyOn(navigator.clipboard, "writeText").mockResolvedValue();
    renderDialog();
    await open(user);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Copy" }));
    expect(writeText).toHaveBeenCalledWith(`stardag builds stop ${BUILD}`);
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("marks an execution stardag cannot stop rather than hiding it", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true, {
        executor: "k8s",
        executor_metadata: { kind: "k8s" },
      }),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(
      await screen.findByText(/1 run on an executor stardag cannot stop/),
    ).toBeInTheDocument();
    expect(screen.getByText("demo.GrindBeans")).toBeInTheDocument();
  });

  it("lists a claim whose spawn has not reported a call id, with the remedies", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true, { executor_ref: null }),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(await screen.findByText("not recorded yet")).toBeInTheDocument();
    const remedy = screen.getByText(/1 selected has no call id/);
    expect(remedy).toHaveTextContent("--mark-lost");
    expect(remedy).toHaveTextContent("--no-cancel");
    // Not called permanently unstoppable: a spawn may yet report.
    expect(screen.queryByText(/cannot stop;/)).not.toBeInTheDocument();
  });

  it("reports a read failure rather than looking empty", async () => {
    vi.mocked(fetchBuildExecutions).mockRejectedValue(new Error("boom"));
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(await screen.findByText(/Could not read .* executions: boom/)).toBeVisible();
    expect(screen.queryByText(/nothing to stop/)).not.toBeInTheDocument();
  });

  it("caps the rows it draws and says the command still targets them all", async () => {
    const many = Array.from({ length: MAX_ROWS_DRAWN + 3 }, (_, i) =>
      execution(`e${i}`, `task-${i}`, true),
    );
    vi.mocked(fetchBuildExecutions).mockResolvedValue(many);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    expect(
      await screen.findByText(/3 more executions not listed\. The command below still/),
    ).toBeInTheDocument();
    expect(command()).toHaveTextContent(`stardag builds stop ${BUILD}`);
  });

  it("stops claiming the undrawn rows are covered once rows are ticked", async () => {
    const many = Array.from({ length: MAX_ROWS_DRAWN + 3 }, (_, i) =>
      execution(`e${i}`, `task-${i}`, true),
    );
    vi.mocked(fetchBuildExecutions).mockResolvedValue(many);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await user.click(await screen.findByLabelText("Include task-0"));
    expect(screen.getByText(/Ticked rows are named individually/)).toBeInTheDocument();
  });
});

describe("BuildControlsDialog overrides", () => {
  it("warns that an override does not stop what is running", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
    ]);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Cancel build" }));
    expect(
      screen.getByText(/still has executions with no end reported/),
    ).toBeInTheDocument();
  });

  it("overrides only after the second, confirming click", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([]);
    vi.mocked(cancelBuild).mockResolvedValue({ status: "cancelled" } as never);
    const user = userEvent.setup();
    renderDialog();
    await open(user);
    await screen.findByText(/nothing to stop/);
    await user.click(screen.getByRole("button", { name: "Cancel build" }));
    expect(cancelBuild).not.toHaveBeenCalled();
    await act(async () => {
      await user.click(screen.getByRole("button", { name: "Cancel build" }));
    });
    await waitFor(() => expect(cancelBuild).toHaveBeenCalledWith(BUILD, "env-1"));
  });

  it("offers no override on a build whose record is already final", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([]);
    const user = userEvent.setup();
    renderDialog("completed");
    await open(user);
    await screen.findByText(/nothing to stop/);
    expect(screen.queryByRole("button", { name: "Cancel build" })).toBeNull();
  });
});
