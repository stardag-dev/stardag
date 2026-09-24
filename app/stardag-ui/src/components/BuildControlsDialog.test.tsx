import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Execution } from "../types/task";

vi.mock("../api/registry", () => ({
  fetchBuildExecutions: vi.fn(),
  cancelBuild: vi.fn(),
  completeBuild: vi.fn(),
  failBuild: vi.fn(),
}));

import { fetchBuildExecutions } from "../api/registry";
import { BuildControlsDialog } from "./BuildControlsDialog";

const BUILD = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";

function execution(id: string, taskId: string, inCurrentPlan: boolean): Execution {
  return {
    id,
    task_id: taskId,
    build_id: BUILD,
    plan_id: inCurrentPlan ? "p-2" : "p-1",
    instance_id: `i-${id}`,
    executor: "modal",
    executor_ref: `fc-${id}`,
    executor_metadata: { kind: "modal", function_name: "worker_gpu" },
    started_at: new Date(Date.now() - 60_000).toISOString(),
    claim_released_at: inCurrentPlan ? null : new Date().toISOString(),
    claim_outcome: inCurrentPlan ? null : "taken_over",
    ended_at: null,
    outcome: null,
    in_current_plan: inCurrentPlan,
  };
}

describe("BuildControlsDialog", () => {
  it("lists unended executions, marks orphans, and narrows the command to them", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([
      execution("e1", "task-aaaaaaaa", true),
      execution("e2", "task-bbbbbbbb", false),
    ]);
    const user = userEvent.setup();
    render(
      <BuildControlsDialog
        buildId={BUILD}
        environmentId="env-1"
        buildStatus="running"
        onBuildChanged={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Build controls" }));
    expect(await screen.findByText("orphan")).toBeInTheDocument();
    expect(screen.getByText("claim taken over")).toBeInTheDocument();
    expect(screen.getByText(`stardag builds stop ${BUILD}`)).toBeInTheDocument();

    await user.click(screen.getByLabelText("Orphans only (not in the current plan)"));
    expect(
      screen.getByText(`stardag builds stop ${BUILD} --not-in-current-plan`),
    ).toBeInTheDocument();
    expect(screen.queryByText("holds the claim")).not.toBeInTheDocument();
  });

  it("says so when nothing is left to stop", async () => {
    vi.mocked(fetchBuildExecutions).mockResolvedValue([]);
    const user = userEvent.setup();
    render(
      <BuildControlsDialog
        buildId={BUILD}
        environmentId="env-1"
        buildStatus="failed"
        onBuildChanged={vi.fn()}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Build controls" }));
    expect(await screen.findByText(/nothing to stop/)).toBeInTheDocument();
  });
});
