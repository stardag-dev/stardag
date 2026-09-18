"""Recursive upstream/downstream traversal for DAG visualization.

Edges are kept per *structure scope* (see ``models/task_dependency.py``),
so a task can carry edges under several scopes: one per code version and
structure config that ever planned it. Which of them a graph shows is the
question this module answers, two ways:

- **A build's graph** (``scope_key`` given) shows the edges in that build's
  own scope. Unambiguous.
- **The environment-wide view** (``scope_key`` None) follows each node's
  **provenance**: the edges recorded in the scope of the build that produced
  the node's current status. The graph then reads as "how each task was
  actually built", and hops scopes where the history did — a complete
  upstream built last month under old code shows its own old-code
  ancestry, which is also the only scope its edges exist in. A task no
  build has ever touched has no provenance and shows no edges.

In both modes an edge is attributed by its **downstream** node, because a
downstream declares its upstreams and never the other way round. Rows with
a NULL scope predate scopes and count everywhere, which is the behaviour
they had before.
"""

import hashlib
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from stardag_api.models import Build, Task, TaskArtifact, TaskDependency
from stardag_api.models.enums import TaskStatus
from stardag_api.schemas import (
    GroupSummary,
    TaskEdgeExtended,
    TaskGraphExtendedResponse,
    TaskNodeExtended,
)
from stardag_api.services.status import get_all_task_global_statuses


class _TraversedTask:
    """Lightweight container for a traversed task row."""

    __slots__ = ("id", "task_id", "task_name", "task_namespace", "depth", "scope_key")

    def __init__(
        self,
        id: UUID,
        task_id: str,
        task_name: str,
        task_namespace: str,
        depth: int,
        scope_key: str | None = None,
    ):
        self.id = id
        self.task_id = task_id
        self.task_name = task_name
        self.task_namespace = task_namespace
        self.depth = depth
        # Provenance scope: the scope of the build behind the node's
        # current status. None when no build has touched it.
        self.scope_key = scope_key


def _edge_scope_filter(scope_key: str | None, provenance):
    """The predicate deciding whether an edge counts in this view.

    ``provenance`` is the aliased ``Build`` joined on the edge's downstream
    task's ``latest_status_build_id``. An edge counts when it sits in the
    downstream's provenance scope; in a build's view, also when it sits in
    the build's own scope — the build's plan is its own edges, and the
    context beyond the plan (complete upstreams other builds produced) is
    read by provenance like everywhere else. Legacy NULL-scope rows always
    count.
    """
    clauses = [
        TaskDependency.scope_key.is_(None),
        TaskDependency.scope_key == provenance.scope_key,
    ]
    if scope_key is not None:
        clauses.append(TaskDependency.scope_key == scope_key)
    return or_(*clauses)


async def _traverse_bfs(
    db: AsyncSession,
    environment_id: UUID,
    primary_task_pks: list[UUID],
    max_upstream_depth: int,
    max_downstream_depth: int,
    scope_key: str | None,
) -> list[_TraversedTask]:
    """BFS traversal in both directions using iterative queries."""
    prov = aliased(Build)
    # Fetch primary tasks
    result = await db.execute(
        select(
            Task.id,
            Task.task_id,
            Task.task_name,
            Task.task_namespace,
            prov.scope_key,
        )
        .outerjoin(prov, prov.id == Task.latest_status_build_id)
        .where(Task.id.in_(primary_task_pks), Task.environment_id == environment_id)
    )
    rows = result.all()

    visited: dict[UUID, _TraversedTask] = {}
    for row in rows:
        visited[row.id] = _TraversedTask(
            id=row.id,
            task_id=row.task_id,
            task_name=row.task_name,
            task_namespace=row.task_namespace,
            depth=0,
            scope_key=row.scope_key,
        )

    # Upstream BFS (positive depth values). The edge's downstream is in the
    # current frontier; its provenance decides whether the edge counts.
    current_frontier = set(visited.keys())
    for depth in range(1, max_upstream_depth + 1):
        if not current_frontier:
            break

        downstream = aliased(Task)
        downstream_prov = aliased(Build)
        node_prov = aliased(Build)
        result = await db.execute(
            select(
                Task.id,
                Task.task_id,
                Task.task_name,
                Task.task_namespace,
                node_prov.scope_key,
            )
            .join(TaskDependency, TaskDependency.upstream_task_id == Task.id)
            .join(downstream, TaskDependency.downstream_task_id == downstream.id)
            .outerjoin(
                downstream_prov, downstream_prov.id == downstream.latest_status_build_id
            )
            .outerjoin(node_prov, node_prov.id == Task.latest_status_build_id)
            .where(
                TaskDependency.downstream_task_id.in_(current_frontier),
                Task.environment_id == environment_id,
                _edge_scope_filter(scope_key, downstream_prov),
            )
        )
        upstream_rows = result.all()

        next_frontier: set[UUID] = set()
        for row in upstream_rows:
            if row.id not in visited:
                visited[row.id] = _TraversedTask(
                    id=row.id,
                    task_id=row.task_id,
                    task_name=row.task_name,
                    task_namespace=row.task_namespace,
                    depth=depth,
                    scope_key=row.scope_key,
                )
                next_frontier.add(row.id)

        current_frontier = next_frontier

    # Downstream BFS (negative depth values). The edge's downstream is the
    # *new* node; again its provenance decides.
    current_frontier = {pk for pk in primary_task_pks if pk in visited}
    for depth_idx in range(1, max_downstream_depth + 1):
        if not current_frontier:
            break

        node_prov = aliased(Build)
        result = await db.execute(
            select(
                Task.id,
                Task.task_id,
                Task.task_name,
                Task.task_namespace,
                node_prov.scope_key,
            )
            .join(TaskDependency, TaskDependency.downstream_task_id == Task.id)
            .outerjoin(node_prov, node_prov.id == Task.latest_status_build_id)
            .where(
                TaskDependency.upstream_task_id.in_(current_frontier),
                Task.environment_id == environment_id,
                _edge_scope_filter(scope_key, node_prov),
            )
        )
        downstream_rows = result.all()

        next_frontier: set[UUID] = set()
        for row in downstream_rows:
            if row.id not in visited:
                visited[row.id] = _TraversedTask(
                    id=row.id,
                    task_id=row.task_id,
                    task_name=row.task_name,
                    task_namespace=row.task_namespace,
                    depth=-depth_idx,
                    scope_key=row.scope_key,
                )
                next_frontier.add(row.id)

        current_frontier = next_frontier

    return list(visited.values())


async def traverse_upstream(
    db: AsyncSession,
    environment_id: UUID,
    primary_task_pks: list[UUID],
    upstream_depth: int = 0,
    downstream_depth: int = 0,
    max_per_type_per_level: int = 5,
    max_total_nodes: int = 500,
    scope_key: str | None = None,
) -> TaskGraphExtendedResponse:
    """Traverse dependencies recursively and return extended graph data.

    Args:
        db: Database session
        environment_id: Environment to scope queries to
        primary_task_pks: Internal DB PKs of the primary tasks (depth 0)
        upstream_depth: How many levels upstream to traverse (0 = none)
        downstream_depth: How many levels downstream to traverse (0 = none)
        max_per_type_per_level: Max tasks per (task_name, depth, status) before
            ALL tasks in that group collapse into a single batch node
        max_total_nodes: Hard cap on total nodes returned
        scope_key: The structure scope to read edges from (a build's graph).
            None means the environment-wide view, which follows each node's
            provenance scope — see the module docstring.
    """
    if not primary_task_pks:
        return TaskGraphExtendedResponse(
            nodes=[],
            edges=[],
            groups=[],
            truncated=False,
            total_upstream_count=0,
            total_downstream_count=0,
        )

    traversed = await _traverse_bfs(
        db,
        environment_id,
        primary_task_pks,
        upstream_depth,
        downstream_depth,
        scope_key,
    )

    total_upstream_count = sum(1 for t in traversed if t.depth > 0)
    total_downstream_count = sum(1 for t in traversed if t.depth < 0)

    # We need statuses before grouping (status is part of the grouping key)
    all_traversed_pks = [t.id for t in traversed]
    statuses = await get_all_task_global_statuses(db, all_traversed_pks)

    def _get_status(pk: UUID) -> TaskStatus:
        tup = statuses.get(pk)
        return tup[0] if tup else TaskStatus.PENDING

    # The focal scopes: the build's own in build mode, the primaries'
    # provenance otherwise. An edge outside them is a hop between code
    # versions, which the UI draws as such.
    primary_pk_set = set(primary_task_pks)
    if scope_key is not None:
        focal_scopes: set[str] = {scope_key}
    else:
        focal_scopes = {
            t.scope_key
            for t in traversed
            if t.id in primary_pk_set and t.scope_key is not None
        }

    def _cross_scope(edge_scope: str | None) -> bool:
        return edge_scope is not None and edge_scope not in focal_scopes

    # Group by (depth, task_name, task_namespace, status) for batching
    groups_by_key: dict[tuple[int, str, str, TaskStatus], list[_TraversedTask]] = {}
    for t in traversed:
        status = _get_status(t.id)
        key = (t.depth, t.task_name, t.task_namespace, status)
        groups_by_key.setdefault(key, []).append(t)

    # All-or-nothing: if count > threshold, ALL go into batch node
    included_task_pks: list[UUID] = []
    included_tasks: list[_TraversedTask] = []
    groups: list[GroupSummary] = []
    grouped_task_pks: set[UUID] = set()
    pk_to_group: dict[UUID, str] = {}

    for (
        depth,
        task_name,
        task_namespace,
        status,
    ), tasks_in_group in groups_by_key.items():
        if len(tasks_in_group) <= max_per_type_per_level:
            for t in tasks_in_group:
                included_task_pks.append(t.id)
                included_tasks.append(t)
        else:
            # ALL go into batch node - none shown individually
            raw_key = f"{depth}:{task_name}:{task_namespace}:{status.value}"
            short_hash = hashlib.sha256(raw_key.encode()).hexdigest()[:12]
            group_id = f"group-{short_hash}"
            all_pks = [t.id for t in tasks_in_group]
            grouped_task_pks.update(all_pks)
            for pk in all_pks:
                pk_to_group[pk] = group_id

            groups.append(
                GroupSummary(
                    group_id=group_id,
                    task_name=task_name,
                    task_namespace=task_namespace,
                    count=len(tasks_in_group),
                    sample_task_ids=[t.task_id for t in tasks_in_group[:5]],
                    depth=depth,
                    status=status,
                    downstream_task_pks=[],
                )
            )

    # Enforce max_total_nodes hard cap
    truncated = False
    if len(included_tasks) > max_total_nodes:
        included_tasks = included_tasks[:max_total_nodes]
        included_task_pks = [t.id for t in included_tasks]
        truncated = True

    all_relevant_pks = set(included_task_pks) | grouped_task_pks

    # Fetch edges between all relevant tasks, under the same view rule the
    # traversal used: attributed by the downstream's provenance (or the
    # build's scope), legacy rows counting everywhere.
    if all_relevant_pks:
        downstream = aliased(Task)
        downstream_prov = aliased(Build)
        edge_result = await db.execute(
            select(
                TaskDependency.upstream_task_id,
                TaskDependency.downstream_task_id,
                TaskDependency.is_dynamic,
                TaskDependency.scope_key,
            )
            .join(downstream, TaskDependency.downstream_task_id == downstream.id)
            .outerjoin(
                downstream_prov, downstream_prov.id == downstream.latest_status_build_id
            )
            .where(
                TaskDependency.upstream_task_id.in_(all_relevant_pks),
                TaskDependency.downstream_task_id.in_(all_relevant_pks),
                _edge_scope_filter(scope_key, downstream_prov),
            )
        )
        raw_edges = edge_result.all()
    else:
        raw_edges = []

    # Build edges, collapsing grouped task edges to group nodes.
    # For collapsed edges (group ↔ task or group ↔ group), ``is_dynamic`` and
    # ``is_cross_scope`` are each the OR over all contributing raw edges —
    # any dynamic (cross-scope) contributor marks the aggregate. Matches the
    # UI expectation "show this edge as dynamic if any underlying
    # contributor is dynamic".
    included_pk_set = set(included_task_pks)
    edges: list[TaskEdgeExtended] = []
    # group_id -> {counterpart_pk: (any_dynamic, any_cross_scope)}
    group_downstream_pks: dict[str, dict[UUID, tuple[bool, bool]]] = {
        g.group_id: {} for g in groups
    }
    group_upstream_pks: dict[str, dict[UUID, tuple[bool, bool]]] = {
        g.group_id: {} for g in groups
    }
    # (src_group, tgt_group) -> (any_dynamic, any_cross_scope)
    group_to_group_edges: dict[tuple[str, str], tuple[bool, bool]] = {}

    def _merge(
        current: tuple[bool, bool] | None, is_dynamic: bool, cross: bool
    ) -> tuple[bool, bool]:
        prev_dyn, prev_cross = current or (False, False)
        return (prev_dyn or is_dynamic, prev_cross or cross)

    for edge in raw_edges:
        source = edge.upstream_task_id
        target = edge.downstream_task_id
        cross = _cross_scope(edge.scope_key)
        source_grouped = source in grouped_task_pks
        target_grouped = target in grouped_task_pks

        if source_grouped and target_grouped:
            src_group = pk_to_group[source]
            tgt_group = pk_to_group[target]
            if src_group != tgt_group:
                key = (src_group, tgt_group)
                group_to_group_edges[key] = _merge(
                    group_to_group_edges.get(key), edge.is_dynamic, cross
                )
            continue

        if source_grouped:
            group_id = pk_to_group[source]
            if target in included_pk_set:
                group_downstream_pks[group_id][target] = _merge(
                    group_downstream_pks[group_id].get(target), edge.is_dynamic, cross
                )
            continue

        if target_grouped:
            group_id = pk_to_group[target]
            if source in included_pk_set:
                group_upstream_pks[group_id][source] = _merge(
                    group_upstream_pks[group_id].get(source), edge.is_dynamic, cross
                )
            continue

        if source in included_pk_set and target in included_pk_set:
            edges.append(
                TaskEdgeExtended(
                    source=str(source),
                    target=str(target),
                    is_dynamic=edge.is_dynamic,
                    scope_key=edge.scope_key,
                    is_cross_scope=cross,
                )
            )

    for group in groups:
        downstream_pks = group_downstream_pks.get(group.group_id, {})
        group.downstream_task_pks = [str(pk) for pk in downstream_pks]
        for pk, (is_dynamic, cross) in downstream_pks.items():
            edges.append(
                TaskEdgeExtended(
                    source=group.group_id,
                    target=str(pk),
                    is_dynamic=is_dynamic,
                    is_cross_scope=cross,
                )
            )
        upstream_pks = group_upstream_pks.get(group.group_id, {})
        for pk, (is_dynamic, cross) in upstream_pks.items():
            edges.append(
                TaskEdgeExtended(
                    source=str(pk),
                    target=group.group_id,
                    is_dynamic=is_dynamic,
                    is_cross_scope=cross,
                )
            )

    for (src_group, tgt_group), (is_dynamic, cross) in group_to_group_edges.items():
        edges.append(
            TaskEdgeExtended(
                source=src_group,
                target=tgt_group,
                is_dynamic=is_dynamic,
                is_cross_scope=cross,
            )
        )

    # Fetch artifact counts
    artifact_counts: dict[UUID, int] = {}
    if included_task_pks:
        artifact_result = await db.execute(
            select(TaskArtifact.task_pk, func.count(TaskArtifact.id))
            .where(TaskArtifact.task_pk.in_(included_task_pks))
            .group_by(TaskArtifact.task_pk)
        )
        artifact_counts = {row[0]: row[1] for row in artifact_result.all()}

    # Build nodes
    nodes: list[TaskNodeExtended] = []
    for task_info in included_tasks:
        status = _get_status(task_info.id)

        nodes.append(
            TaskNodeExtended(
                id=task_info.id,
                task_id=task_info.task_id,
                task_name=task_info.task_name,
                task_namespace=task_info.task_namespace,
                status=status,
                artifact_count=artifact_counts.get(task_info.id, 0),
                is_primary=task_info.id in primary_pk_set,
                traversal_depth=task_info.depth,
                scope_key=task_info.scope_key,
            )
        )

    return TaskGraphExtendedResponse(
        nodes=nodes,
        edges=edges,
        groups=groups,
        truncated=truncated,
        total_upstream_count=total_upstream_count,
        total_downstream_count=total_downstream_count,
    )
