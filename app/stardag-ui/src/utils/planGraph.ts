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
