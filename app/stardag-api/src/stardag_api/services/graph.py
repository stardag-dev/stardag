"""Recursive upstream traversal for DAG visualization."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.models import Task, TaskArtifact, TaskDependency
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

    __slots__ = ("id", "task_id", "task_name", "task_namespace", "depth")

    def __init__(
        self,
        id: UUID,
        task_id: str,
        task_name: str,
        task_namespace: str,
        depth: int,
    ):
        self.id = id
        self.task_id = task_id
        self.task_name = task_name
        self.task_namespace = task_namespace
        self.depth = depth


async def _traverse_bfs(
    db: AsyncSession,
    environment_id: UUID,
    primary_task_pks: list[UUID],
    max_depth: int,
) -> list[_TraversedTask]:
    """BFS upstream traversal using iterative queries (database-agnostic)."""
    # Fetch primary tasks
    result = await db.execute(
        select(Task.id, Task.task_id, Task.task_name, Task.task_namespace).where(
            Task.id.in_(primary_task_pks), Task.environment_id == environment_id
        )
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
        )

    # Iterative BFS: expand upstream level by level
    current_frontier = set(visited.keys())
    for depth in range(1, max_depth + 1):
        if not current_frontier:
            break

        # Find upstream tasks for current frontier
        result = await db.execute(
            select(
                Task.id,
                Task.task_id,
                Task.task_name,
                Task.task_namespace,
                TaskDependency.downstream_task_id,
            )
            .join(TaskDependency, TaskDependency.upstream_task_id == Task.id)
            .where(
                TaskDependency.downstream_task_id.in_(current_frontier),
                Task.environment_id == environment_id,
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
                )
                next_frontier.add(row.id)
            # Keep the minimum depth (already visited at shallower level = keep that)

        current_frontier = next_frontier

    return list(visited.values())


async def traverse_upstream(
    db: AsyncSession,
    environment_id: UUID,
    primary_task_pks: list[UUID],
    upstream_depth: int = 0,
    max_per_type_per_level: int = 50,
    max_total_nodes: int = 500,
) -> TaskGraphExtendedResponse:
    """Traverse upstream dependencies recursively and return extended graph data.

    Args:
        db: Database session
        environment_id: Environment to scope queries to
        primary_task_pks: Internal DB PKs of the primary tasks (depth 0)
        upstream_depth: How many levels upstream to traverse (0 = primary only)
        max_per_type_per_level: Max tasks per task_name per depth level before grouping
        max_total_nodes: Hard cap on total nodes returned
    """
    if not primary_task_pks:
        return TaskGraphExtendedResponse(
            nodes=[], edges=[], groups=[], truncated=False, total_upstream_count=0
        )

    traversed = await _traverse_bfs(
        db, environment_id, primary_task_pks, upstream_depth
    )

    total_upstream_count = sum(1 for t in traversed if t.depth > 0)

    # Group by (depth, task_name) for batching
    groups_by_key: dict[tuple[int, str, str], list[_TraversedTask]] = {}
    for t in traversed:
        key = (t.depth, t.task_name, t.task_namespace)
        groups_by_key.setdefault(key, []).append(t)

    # Decide which tasks to include individually vs group
    included_task_pks: list[UUID] = []
    included_tasks: list[_TraversedTask] = []
    groups: list[GroupSummary] = []
    grouped_task_pks: set[UUID] = set()
    pk_to_group: dict[UUID, str] = {}

    for (depth, task_name, task_namespace), tasks_in_group in groups_by_key.items():
        if len(tasks_in_group) <= max_per_type_per_level:
            for t in tasks_in_group:
                included_task_pks.append(t.id)
                included_tasks.append(t)
        else:
            kept = tasks_in_group[:max_per_type_per_level]
            for t in kept:
                included_task_pks.append(t.id)
                included_tasks.append(t)

            group_id = f"group-{depth}-{task_name}-{task_namespace}"
            overflow_pks = [t.id for t in tasks_in_group[max_per_type_per_level:]]
            grouped_task_pks.update(overflow_pks)
            for pk in overflow_pks:
                pk_to_group[pk] = group_id

            groups.append(
                GroupSummary(
                    group_id=group_id,
                    task_name=task_name,
                    task_namespace=task_namespace,
                    count=len(tasks_in_group),
                    sample_task_ids=[t.task_id for t in tasks_in_group[:5]],
                    depth=depth,
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
    primary_pk_set = set(primary_task_pks)

    # Fetch edges between all relevant tasks
    if all_relevant_pks:
        edge_result = await db.execute(
            select(
                TaskDependency.upstream_task_id, TaskDependency.downstream_task_id
            ).where(
                TaskDependency.upstream_task_id.in_(all_relevant_pks),
                TaskDependency.downstream_task_id.in_(all_relevant_pks),
            )
        )
        raw_edges = edge_result.all()
    else:
        raw_edges = []

    # Build edges, collapsing grouped task edges to group nodes
    included_pk_set = set(included_task_pks)
    edges: list[TaskEdgeExtended] = []
    group_downstream_pks: dict[str, set[UUID]] = {g.group_id: set() for g in groups}

    for edge in raw_edges:
        source = edge.upstream_task_id
        target = edge.downstream_task_id

        if source in grouped_task_pks:
            group_id = pk_to_group[source]
            if target in included_pk_set:
                group_downstream_pks[group_id].add(target)
            continue
        if target in grouped_task_pks:
            continue

        if source in included_pk_set and target in included_pk_set:
            edges.append(TaskEdgeExtended(source=str(source), target=str(target)))

    for group in groups:
        downstream_pks = group_downstream_pks.get(group.group_id, set())
        group.downstream_task_pks = [str(pk) for pk in downstream_pks]
        for pk in downstream_pks:
            edges.append(TaskEdgeExtended(source=group.group_id, target=str(pk)))

    # Fetch statuses for all included tasks
    statuses = await get_all_task_global_statuses(db, included_task_pks)

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
        status_tuple = statuses.get(
            task_info.id, (TaskStatus.PENDING, None, None, None, None, False)
        )
        status = status_tuple[0]

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
            )
        )

    return TaskGraphExtendedResponse(
        nodes=nodes,
        edges=edges,
        groups=groups,
        truncated=truncated,
        total_upstream_count=total_upstream_count,
    )
