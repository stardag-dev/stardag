import { describe, expect, it } from "vitest";
import type { BuildFrontier, FrontierItem, FrontierMember } from "../types/task";
import { flowModel, fullPlanView, partialPlanView } from "./planGraph";

function member(
  taskId: string,
  instanceId: string,
  overrides: Partial<FrontierMember> = {},
): FrontierMember {
  return {
    task_id: taskId,
    instance_id: instanceId,
    instance_hash: `h-${instanceId}`,
    status: "pending",
    is_root: false,
    body: { __namespace: "demo", __name: `Task${taskId}` },
    ...overrides,
  };
}

function item(m: FrontierMember): FrontierItem {
  return { ...m, attempts: 1, interruptions: 0 };
}

function frontier(overrides: Partial<BuildFrontier> = {}): BuildFrontier {
  return {
    build_id: "b",
    plan_id: "p",
    deployment_id: "d",
    settings_hash: "s",
    sealed: true,
    plan_complete: false,
    build_status: "running",
    reactive_app_name: null,
    reactive_tick_kwargs: null,
    runnable: [],
    discovery_jobs: [],
    running: [],
    closure: null,
    ...overrides,
  };
}

describe("partialPlanView", () => {
  it("unions the roots and the frontier items once per instance", () => {
    const root = member("A", "i-a", { is_root: true, status: "running" });
    const view = partialPlanView(
      [root],
      frontier({
        running: [item(root)],
        runnable: [item(member("B", "i-b"))],
        discovery_jobs: [member("C", "i-c")],
      }),
    );
    expect(view.complete).toBe(false);
    expect(view.edges).toEqual([]);
    expect(
      view.members.map((m) => [m.instance_id, m.task_name, m.admitted_by]),
    ).toEqual([
      ["i-a", "TaskA", "root"],
      ["i-b", "TaskB", null],
      ["i-c", "TaskC", null],
    ]);
  });
});

describe("flowModel", () => {
  it("keys nodes by instance id and drops edges leaving the plan", () => {
    const view = fullPlanView({
      plan_id: "p",
      members: [
        {
          task_id: "A",
          instance_id: "i-a",
          task_namespace: "demo",
          task_name: "Load",
          status: "completed",
          is_root: false,
          admitted_by: "static",
          excluded_at: null,
          excluded_reason: null,
        },
        {
          task_id: "B",
          instance_id: "i-b",
          task_namespace: "demo",
          task_name: "Train",
          status: "pending",
          is_root: true,
          admitted_by: "root",
          excluded_at: "2026-09-24T00:00:00Z",
          excluded_reason: "operator",
        },
      ],
      edges: [
        {
          upstream_instance_id: "i-a",
          downstream_instance_id: "i-b",
          is_dynamic: true,
        },
        {
          upstream_instance_id: "i-x",
          downstream_instance_id: "i-b",
          is_dynamic: false,
        },
      ],
    });
    const { nodes, edges } = flowModel(view);
    expect(nodes.map((n) => [n.id, n.taskId, n.label, n.excluded])).toEqual([
      ["i-a", "A", "Load", false],
      ["i-b", "B", "Train", true],
    ]);
    expect(edges).toEqual([
      { id: "i-a-i-b", source: "i-a", target: "i-b", isDynamic: true },
    ]);
  });
});
