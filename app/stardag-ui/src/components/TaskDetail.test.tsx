import { fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { PlanMember, Task } from "../types/task";
import { MEMBERSHIP_HELP } from "../utils/membership";

vi.mock("../api/registry", () => ({
  fetchTask: vi.fn(),
  fetchTaskArtifacts: vi.fn(async () => ({ artifacts: [] })),
  fetchTaskEvents: vi.fn(async () => []),
  EVENT_LIST_LIMIT: 500,
  fetchTaskExecutions: vi.fn(async () => []),
  TASK_EXECUTION_LIMIT: 100,
  fetchDeployments: vi.fn(async () => []),
}));
vi.mock("./TaskClaimPanel", () => ({ TaskClaimPanel: () => null }));

import { fetchTask, fetchTaskEvents, fetchTaskExecutions } from "../api/registry";
import type { Execution } from "../types/task";
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

  it("jumps to an event's build from the event log's Build column", async () => {
    const BUILD = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
    vi.mocked(fetchTaskEvents).mockResolvedValue([
      {
        id: "e1",
        event_type: "task_started",
        created_at: "2026-09-24T00:00:00Z",
        build_id: BUILD,
        plan_id: "p",
        execution_id: null,
        task_id: TASK_ID,
        report_applied: true,
        error_message: null,
        event_metadata: null,
      },
    ]);
    const onOpenBuild = vi.fn();
    render(
      <TaskDetail taskId={TASK_ID} environmentId="env-1" onOpenBuild={onOpenBuild} />,
    );
    fireEvent.click(await screen.findByRole("button", { name: "See full event log" }));
    fireEvent.click(await screen.findByRole("button", { name: "…1bd0287c" }));
    expect(onOpenBuild).toHaveBeenCalledWith(BUILD);
  });

  it("lists every execution across builds, ended ones included", async () => {
    const OTHER = "01a0c5c3-f18e-7d22-bcaf-00000000cccc";
    const base: Execution = {
      id: "0199aaaa-0000-7000-8000-000000000001",
      task_id: TASK_ID,
      build_id: "b",
      plan_id: "p",
      instance_id: "i-1",
      executor: "modal",
      executor_ref: "fc-01ABC",
      executor_metadata: {
        kind: "modal",
        workspace: "ws",
        environment: "main",
        app_name: "app",
        function_name: "worker_gpu",
      },
      started_at: "2026-09-24T00:00:00Z",
      claim_released_at: null,
      claim_outcome: null,
      ended_at: null,
      outcome: null,
      in_current_plan: true,
    };
    vi.mocked(fetchTaskExecutions).mockResolvedValue([
      base,
      {
        ...base,
        id: "0199aaaa-0000-7000-8000-000000000002",
        build_id: OTHER,
        executor: null,
        executor_ref: "fc-02DEF",
        ended_at: "2026-09-24T00:05:00Z",
        outcome: "failed",
        claim_outcome: "failed",
        claim_released_at: "2026-09-24T00:05:00Z",
        in_current_plan: false,
      },
    ]);
    const onOpenBuild = vi.fn();
    render(
      <TaskDetail
        taskId={TASK_ID}
        environmentId="env-1"
        context={{ buildId: "b", planId: "p", planInstanceId: "i-1", member }}
        onOpenBuild={onOpenBuild}
      />,
    );
    expect(await screen.findByText("Executions (2)")).toBeInTheDocument();
    expect(vi.mocked(fetchTaskExecutions)).toHaveBeenCalledWith(TASK_ID, "env-1");
    expect(screen.getByText("this build")).toBeInTheDocument();
    expect(screen.getByText("running, holds the claim")).toBeInTheDocument();
    expect(screen.getByText("ended failed")).toBeInTheDocument();
    // The executor falls back to the metadata kind, as the CLI reads it.
    expect(screen.getAllByText("⚡ Modal")).toHaveLength(2);
    expect(screen.getByText("fc-02DEF")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "…0000cccc" }));
    expect(onOpenBuild).toHaveBeenCalledWith(OTHER);
    // The Modal ids table, one click away.
    fireEvent.click(screen.getAllByRole("button", { name: "More details" })[0]);
    expect(screen.getByText("Workspace")).toBeInTheDocument();
  });

  it("lists executions on the task page too, with no build in view", async () => {
    vi.mocked(fetchTaskExecutions).mockResolvedValue([]);
    render(<TaskDetail taskId={TASK_ID} environmentId="env-1" />);
    expect(await screen.findByText("Executions (0)")).toBeInTheDocument();
    expect(screen.getByText(/No execution recorded/)).toBeInTheDocument();
  });
});
