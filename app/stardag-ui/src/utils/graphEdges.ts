/**
 * The React Flow id of a dependency edge.
 *
 * The API can return the same source/target pair more than once: once
 * under the build's own scope and once under a node's provenance scope,
 * each with its own dynamic/cross-scope marking. React Flow keys edges by
 * id, so an id made of the pair alone collapses them and one scope's
 * style and tooltip are lost. The scope is therefore part of the id; an
 * edge recorded before scopes existed (`scope_key` null) is `legacy`.
 */
export function reactFlowEdgeId(edge: {
  source: number | string;
  target: number | string;
  scope_key?: string | null;
}): string {
  return `${edge.source}-${edge.target}-${edge.scope_key ?? "legacy"}`;
}
