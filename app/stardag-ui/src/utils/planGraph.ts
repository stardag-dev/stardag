/**
 * The build view's model of its active plan: members and instance edges.
 *
 * Two sources. The full one is `GET /plans/{id}/graph` (assumed — not
 * served yet). Until it exists the view falls back to what the registry
 * does serve: the plan's roots and the frontier's runnable, discovery-job
 * and running items. That is a **partial** membership — a member that is
 * complete, or pending behind an upstream, appears in neither — and it has
 * no edges, so the view says so rather than drawing it as the plan.
 */

import type {
  BuildFrontier,
  FrontierMember,
  PlanEdge,
  PlanGraph,
  PlanMember,
  TaskStatus,
} from "../types/task";
import { identityOf } from "./instances";

export interface PlanView {
  members: PlanMember[];
  edges: PlanEdge[];
  // False when built from the roots and the frontier only.
  complete: boolean;
}

function memberFromFrontier(item: FrontierMember): PlanMember {
  const { namespace, name } = identityOf(item.body);
  return {
    task_id: item.task_id,
    instance_id: item.instance_id,
    task_namespace: namespace,
    task_name: name,
    status: item.status,
    is_root: item.is_root,
    admitted_by: item.is_root ? "root" : null,
    excluded_at: null,
    excluded_reason: null,
  };
}

/**
 * The partial membership: roots first, then every frontier item not yet
 * seen, keyed by instance id (a plan holds one instance per completion,
 * so the instance id is also unique per task within it).
 */
export function partialPlanView(
  roots: FrontierMember[],
  frontier: BuildFrontier,
): PlanView {
  const byInstance = new Map<string, PlanMember>();
  for (const item of [
    ...roots,
    ...frontier.running,
    ...frontier.runnable,
    ...frontier.discovery_jobs,
  ]) {
    if (!byInstance.has(item.instance_id)) {
      byInstance.set(item.instance_id, memberFromFrontier(item));
    }
  }
  return { members: [...byInstance.values()], edges: [], complete: false };
}

export function fullPlanView(graph: PlanGraph): PlanView {
  return { members: graph.members, edges: graph.edges, complete: true };
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
      (edge) => ids.has(edge.upstream_instance_id) && ids.has(edge.downstream_instance_id),
    )
    .map((edge) => ({
      id: `${edge.upstream_instance_id}-${edge.downstream_instance_id}`,
      source: edge.upstream_instance_id,
      target: edge.downstream_instance_id,
      isDynamic: edge.is_dynamic,
    }));
  return { nodes, edges };
}
