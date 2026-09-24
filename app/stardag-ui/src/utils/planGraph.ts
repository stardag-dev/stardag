/**
 * The build view's model of its active plan: members and instance edges,
 * from `GET /plans/{id}/graph`.
 */

import type { PlanEdge, PlanGraph, PlanMember, TaskStatus } from "../types/task";

export interface PlanView {
  members: PlanMember[];
  edges: PlanEdge[];
}

export function fullPlanView(graph: PlanGraph): PlanView {
  return { members: graph.members, edges: graph.edges };
}

/** The graph's nodes and edges as the DAG draws them. */
export interface FlowNodeModel {
  // The instance id: one node per plan member.
  id: string;
  taskId: string;
  label: string;
  // `namespace.name`: what "the same type" means when grouping.
  taskType: string;
  status: TaskStatus;
  excluded: boolean;
}

export interface FlowEdgeModel {
  id: string;
  source: string;
  target: string;
  isDynamic: boolean;
}

/**
 * Nodes keyed by instance id, edges upstream → downstream. An edge whose
 * end is not a member of the plan is dropped: edges belong to the scope,
 * not the plan, so the scope may know edges the plan never admitted.
 */
export function flowModel(view: PlanView): {
  nodes: FlowNodeModel[];
  edges: FlowEdgeModel[];
} {
  const nodes = view.members.map((member) => ({
    id: member.instance_id,
    taskId: member.task_id,
    label: member.task_name || member.task_id.slice(0, 8),
    taskType: `${member.task_namespace}.${member.task_name}`,
    status: member.status,
    excluded: member.excluded_at !== null,
  }));
  const ids = new Set(nodes.map((node) => node.id));
  const edges = view.edges
    .filter(
      (edge) =>
        ids.has(edge.upstream_instance_id) && ids.has(edge.downstream_instance_id),
    )
    .map((edge) => ({
      id: `${edge.upstream_instance_id}-${edge.downstream_instance_id}`,
      source: edge.upstream_instance_id,
      target: edge.downstream_instance_id,
      isDynamic: edge.is_dynamic,
    }));
  return { nodes, edges };
}

// ---- Fan-out batching (client-side; v1 grouped on the server) ----

/** v1's default for "group after": more than this many collapse. */
export const DEFAULT_GROUP_AFTER = 5;

export interface FlowBatchModel {
  // `batch:<level>:<type>:<status>`.
  id: string;
  label: string;
  taskType: string;
  status: TaskStatus;
  level: number;
  // The members' task ids and instance (node) ids.
  taskIds: string[];
  memberIds: string[];
}

export interface GroupedFlowModel {
  nodes: FlowNodeModel[];
  batches: FlowBatchModel[];
  edges: FlowEdgeModel[];
}

/**
 * Each node's level: the longest path to it from a node with no upstream,
 * over the plan's edges. Siblings of one fan-out share a level. A cycle
 * (never produced by a plan) cannot loop: levels are bounded by the node
 * count.
 */
export function nodeLevels(
  nodeIds: string[],
  edges: { source: string; target: string }[],
): Map<string, number> {
  const level = new Map(nodeIds.map((id) => [id, 0]));
  for (let pass = 0; pass < nodeIds.length; pass++) {
    let changed = false;
    for (const edge of edges) {
      const next = (level.get(edge.source) ?? 0) + 1;
      if (level.has(edge.target) && next > (level.get(edge.target) ?? 0)) {
        level.set(edge.target, next);
        changed = true;
      }
    }
    if (!changed) break;
  }
  return level;
}

/**
 * v1's fan-out batching, over the plan graph on the client: members of the
 * same type, at the same level, with the same status, are drawn as one
 * batch node with a count once there are more than `groupAfter` of them.
 * A batch whose id is in `expanded` is drawn member by member again.
 * Edges are re-pointed at the batches and de-duplicated; one is dynamic if
 * any edge it stands for is.
 */
export function groupFlowModel(
  model: { nodes: FlowNodeModel[]; edges: FlowEdgeModel[] },
  groupAfter: number,
  expanded: ReadonlySet<string> = new Set(),
): GroupedFlowModel {
  const levels = nodeLevels(
    model.nodes.map((n) => n.id),
    model.edges,
  );
  const buckets = new Map<string, FlowNodeModel[]>();
  for (const node of model.nodes) {
    const key = `batch:${levels.get(node.id) ?? 0}:${node.taskType}:${node.status}`;
    const bucket = buckets.get(key);
    if (bucket) bucket.push(node);
    else buckets.set(key, [node]);
  }
  const nodes: FlowNodeModel[] = [];
  const batches: FlowBatchModel[] = [];
  const drawnAs = new Map<string, string>();
  for (const [id, members] of buckets) {
    if (members.length > groupAfter && !expanded.has(id)) {
      const first = members[0];
      batches.push({
        id,
        label: first.label,
        taskType: first.taskType,
        status: first.status,
        level: levels.get(first.id) ?? 0,
        taskIds: members.map((m) => m.taskId),
        memberIds: members.map((m) => m.id),
      });
      for (const m of members) drawnAs.set(m.id, id);
    } else {
      nodes.push(...members);
    }
  }
  const edges = new Map<string, FlowEdgeModel>();
  for (const edge of model.edges) {
    const source = drawnAs.get(edge.source) ?? edge.source;
    const target = drawnAs.get(edge.target) ?? edge.target;
    if (source === target) continue;
    const id = `${source}-${target}`;
    const seen = edges.get(id);
    if (seen) seen.isDynamic ||= edge.isDynamic;
    else edges.set(id, { id, source, target, isDynamic: edge.isDynamic });
  }
  return { nodes, batches, edges: [...edges.values()] };
}
