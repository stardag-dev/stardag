import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PlanMember, Task } from "../types/task";
import { MEMBERSHIP_HELP } from "../utils/membership";

vi.mock("../api/registry", () => ({
  fetchTask: vi.fn(),
  fetchTaskArtifacts: vi.fn(async () => ({ artifacts: [] })),
  fetchTaskEvents: vi.fn(async () => []),
  EVENT_LIST_LIMIT: 500,
  fetchBuildExecutions: vi.fn(async () => []),
  fetchDeployments: vi.fn(async () => []),
}));
vi.mock("./TaskClaimPanel", () => ({ TaskClaimPanel: () => null }));

import { fetchTask, fetchTaskEvents } from "../api/registry";
import { TaskDetail } from "./TaskDetail";

const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    task_id: TASK_ID,
    task_namespace: "demo",
    task_name: "Train",
    version: null,
    output_uri: null,
    status: "pending",
    status_at: null,
    started_at: null,
    completed_at: null,
    error_message: null,
    claim_expires_at: null,
    claim_plan_id: null,
    claim_build_id: null,
    execution_id: null,
    instances: [
      {
        id: "i-1",
        deployment_id: "dep-1",
        settings_hash: "a".repeat(64),
        instance_hash: "h".repeat(16),
        body: { __namespace: "demo", __name: "Train", epochs: 3 },
        expanded_at: "2026-09-24T00:00:02Z",
        created_at: "2026-09-24T00:00:02Z",
      },
    ],
    ...overrides,
  };
}

const member: PlanMember = {
  task_id: TASK_ID,
  instance_id: "i-1",
  instance_hash: "h".repeat(16),
  task_namespace: "demo",
  task_name: "Train",
  status: "pending",
  is_root: false,
  admitted_by: "closure",
  excluded_at: null,
  excluded_reason: null,
  attempts: 0,
  interruptions: 0,
};

beforeEach(() => {
  vi.mocked(fetchTask).mockResolvedValue(makeTask());
});

describe("TaskDetail", () => {
  it("shows the plan membership in the header, with the same explanations", async () => {
    render(
      <TaskDetail
        taskId={TASK_ID}
        environmentId="env-1"
        context={{ buildId: "b", planId: "p", planInstanceId: "i-1", member }}
      />,
    );
    await screen.findByRole("heading", { name: /demo\.Train/ });
    const header = screen.getByText("In this plan:").parentElement!;
    expect(within(header).getByText("closure")).toHaveAttribute(
      "title",
      MEMBERSHIP_HELP.closure,
    );
  });

  it("shows no membership outside a build", async () => {
    render(<TaskDetail taskId={TASK_ID} environmentId="env-1" />);
    await screen.findByRole("heading", { name: /demo\.Train/ });
    expect(screen.queryByText("In this plan:")).not.toBeInTheDocument();
  });

  it("links to the task page from an icon next to the header", async () => {
    const open = vi.fn();
    render(<TaskDetail taskId={TASK_ID} environmentId="env-1" onOpenTaskPage={open} />);
    const heading = await screen.findByRole("heading", { name: /demo\.Train/ });
    const link = within(heading.parentElement!).getByRole("button", {
      name: "Open task page",
    });
    fireEvent.click(link);
    expect(open).toHaveBeenCalledTimes(1);
  });

  it("offers no task-page link on the task page itself", async () => {
    render(<TaskDetail taskId={TASK_ID} environmentId="env-1" />);
    await screen.findByRole("heading", { name: /demo\.Train/ });
    expect(screen.queryByRole("button", { name: "Open task page" })).toBeNull();
  });

  it("opens the full event log over the task's events", async () => {
    vi.mocked(fetchTaskEvents).mockResolvedValue([
      {
        id: "e1",
        event_type: "task_pending",
        created_at: "2026-09-24T00:00:00Z",
        build_id: "01a0c5c3-f18e-7d22-bcaf-add71bd0287c",
        plan_id: "p",
        execution_id: null,
        task_id: TASK_ID,
        report_applied: true,
        error_message: null,
        event_metadata: null,
      },
      {
        id: "e2",
        event_type: "task_structure_diverged",
        created_at: "2026-09-24T00:00:01Z",
        build_id: "01a0c5c3-f18e-7d22-bcaf-add71bd0287c",
        plan_id: "p",
        execution_id: "0199aaaa-bbbb",
        task_id: TASK_ID,
        report_applied: false,
        error_message: null,
        event_metadata: { added: ["x"] },
      },
    ]);
    render(<TaskDetail taskId={TASK_ID} environmentId="env-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "See full event log" }));
    expect(vi.mocked(fetchTaskEvents)).toHaveBeenCalledWith(TASK_ID, "env-1");
    expect(await screen.findByText("Structure Diverged")).toBeInTheDocument();
    expect(screen.getByText("Pending")).toBeInTheDocument();
    expect(screen.getByText("not applied")).toBeInTheDocument();
    expect(screen.getByText("1 field")).toHaveAttribute(
      "title",
      JSON.stringify({ added: ["x"] }, null, 2),
    );
  });
});
