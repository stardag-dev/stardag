import { describe, expect, it } from "vitest";
import type { PlanMember } from "../types/task";
import { flowModel, fullPlanView, groupFlowModel, nodeLevels } from "./planGraph";

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

function planMember(id: string, name: string, overrides: Partial<PlanMember> = {}) {
  return {
    task_id: `t-${id}`,
    instance_id: id,
    instance_hash: `h-${id}`,
    task_namespace: "demo",
    task_name: name,
    status: "completed" as const,
    is_root: false,
    admitted_by: "static" as const,
    excluded_at: null,
    excluded_reason: null,
    attempts: 0,
    interruptions: 0,
    ...overrides,
  };
}

// A root fanning out to `n` Shard members, each downstream of one Load.
function fanOut(
  n: number,
  statusOf: (i: number) => PlanMember["status"] = () => "completed",
) {
  const shards = Array.from({ length: n }, (_, i) =>
    planMember(`s${i}`, "Shard", { status: statusOf(i) }),
  );
  return flowModel(
    fullPlanView({
      plan_id: "p",
      build_id: "b",
      deployment_id: "d",
      settings_hash: "s",
      members: [planMember("load", "Load"), ...shards, planMember("root", "Root")],
      edges: [
        ...shards.map((m) => ({
          upstream_instance_id: "load",
          downstream_instance_id: m.instance_id,
          is_dynamic: false,
        })),
        ...shards.map((m) => ({
          upstream_instance_id: m.instance_id,
          downstream_instance_id: "root",
          is_dynamic: m.instance_id === "s0",
        })),
      ],
    }),
  );
}

describe("nodeLevels", () => {
  it("is the longest path from a node with no upstream", () => {
    const levels = nodeLevels(
      ["a", "b", "c"],
      [
        { source: "a", target: "b" },
        { source: "b", target: "c" },
        { source: "a", target: "c" },
      ],
    );
    expect([...levels.entries()]).toEqual([
      ["a", 0],
      ["b", 1],
      ["c", 2],
    ]);
  });
});

describe("groupFlowModel", () => {
  it("collapses a fan-out wider than the cap into one batch, edges re-pointed", () => {
    const grouped = groupFlowModel(fanOut(8), 5);
    expect(grouped.nodes.map((n) => n.id)).toEqual(["load", "root"]);
    expect(grouped.batches).toHaveLength(1);
    const [batch] = grouped.batches;
    expect(batch).toMatchObject({
      label: "Shard",
      status: "completed",
      level: 1,
    });
    expect(batch.memberIds).toHaveLength(8);
    expect(grouped.edges).toEqual([
      { id: `load-${batch.id}`, source: "load", target: batch.id, isDynamic: false },
      // One of the eight edges into the root is dynamic, so the one standing
      // for them all is.
      { id: `${batch.id}-root`, source: batch.id, target: "root", isDynamic: true },
    ]);
  });

  it("does not collapse at or under the cap", () => {
    expect(groupFlowModel(fanOut(5), 5).batches).toEqual([]);
  });

  it("groups by status as well as type and level", () => {
    const grouped = groupFlowModel(
      fanOut(9, (i) => (i < 6 ? "completed" : "failed")),
      5,
    );
    expect(grouped.batches.map((b) => [b.status, b.memberIds.length])).toEqual([
      ["completed", 6],
    ]);
    // Three failed shards stay individual nodes.
    expect(grouped.nodes.filter((n) => n.status === "failed")).toHaveLength(3);
  });

  it("draws an expanded batch member by member", () => {
    const { batches } = groupFlowModel(fanOut(8), 5);
    const expanded = groupFlowModel(fanOut(8), 5, new Set([batches[0].id]));
    expect(expanded.batches).toEqual([]);
    expect(expanded.nodes).toHaveLength(10);
  });
});
