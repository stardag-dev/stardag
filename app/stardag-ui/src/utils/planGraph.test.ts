import { describe, expect, it } from "vitest";
import { flowModel, fullPlanView } from "./planGraph";

describe("flowModel", () => {
  it("keys nodes by instance id and drops edges leaving the plan", () => {
    const view = fullPlanView({
      plan_id: "p",
      build_id: "b",
      deployment_id: "d",
      settings_hash: "s",
      members: [
        {
          task_id: "A",
          instance_id: "i-a",
          instance_hash: "h-a",
          task_namespace: "demo",
          task_name: "Load",
          status: "completed",
          is_root: false,
          admitted_by: "static",
          excluded_at: null,
          excluded_reason: null,
          attempts: 1,
          interruptions: 0,
        },
        {
          task_id: "B",
          instance_id: "i-b",
          instance_hash: "h-b",
          task_namespace: "demo",
          task_name: "Train",
          status: "pending",
          is_root: true,
          admitted_by: "root",
          excluded_at: "2026-09-24T00:00:00Z",
          excluded_reason: "operator",
          attempts: 0,
          interruptions: 0,
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
