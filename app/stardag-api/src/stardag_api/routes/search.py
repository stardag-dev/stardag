"""Task search routes - advanced filtering and autocomplete."""

import re
import time
from collections import Counter
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Build, Event, Task, TaskArtifact
from stardag_api.models.enums import TaskStatus
from stardag_api.schemas import (
    AvailableColumnsResponse,
    KeySuggestion,
    KeySuggestionsResponse,
    TaskSearchResponse,
    TaskSearchResult,
    ValueSuggestion,
    ValueSuggestionsResponse,
)
from stardag_api.services.status import get_all_task_global_statuses

router = APIRouter(prefix="/tasks/search", tags=["search"])

# Simple in-memory cache for suggestions with TTL (5 minutes)
_suggestions_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL_SECONDS = 300


def _get_cached(key: str) -> Any | None:
    """Get a value from cache if not expired."""
    if key in _suggestions_cache:
        timestamp, value = _suggestions_cache[key]
        if time.time() - timestamp < _CACHE_TTL_SECONDS:
            return value
        # Expired, remove from cache
        del _suggestions_cache[key]
    return None


def _set_cached(key: str, value: Any) -> None:
    """Set a value in cache with current timestamp."""
    _suggestions_cache[key] = (time.time(), value)


# Filter operators and their SQL equivalents
OPERATORS = {
    "=": "=",
    "!=": "!=",
    ">": ">",
    "<": "<",
    ">=": ">=",
    "<=": "<=",
    "~": "ILIKE",  # substring/contains
}


def _validate_filter_value(key: str, op: str, value: str) -> None:
    """Reject filter values that would otherwise crash at SQL execution time.

    Currently only enforces UUID-shape on ``build_id`` for non-substring
    operators. Without this, the SQL ``CAST(:p AS uuid)`` raises an
    asyncpg ``InvalidTextRepresentation`` at execution time and the
    request bubbles up as a 500. Validating in Python lets us return a
    clear 400 with an actionable message instead.

    ``~`` (ILIKE) is exempt — substring match is text-only and any string
    is acceptable input.
    """
    if key == "build_id" and op != "~":
        try:
            UUID(value)
        except (ValueError, AttributeError):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Invalid build_id filter value {value!r}: "
                    "must be a valid UUID for non-substring comparisons"
                ),
            )


def parse_filter_string(filter_str: str) -> list[tuple[str, str, str]]:
    """Parse filter string into list of (key, operator, value) tuples.

    Format: key:op:value,key:op:value,...
    Examples:
        - task_name:=:training
        - param.lr:>:0.01
        - task_namespace:~:ml

    Raises HTTPException(400) if any filter value is rejected by
    ``_validate_filter_value`` (e.g. a malformed UUID for ``build_id``).
    """
    if not filter_str:
        return []

    filters = []
    # Split by comma, but handle escaped commas
    parts = filter_str.split(",")

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Parse key:op:value or key:value (default op is =)
        match = re.match(r"^([^:]+):([=!<>~]+)?:?(.*)$", part)
        if match:
            key, op, value = match.groups()
            op = op or "="
            if op in OPERATORS:
                key = key.strip()
                value = value.strip()
                _validate_filter_value(key, op, value)
                filters.append((key, op, value))

    return filters


def build_jsonb_condition(
    key: str,
    operator: str,
    value: str,
    task_alias: str = "tasks",
    param_suffix: str = "",
) -> tuple[str | None, bool, str | None]:
    """Build a SQL condition for JSONB filtering.

    Handles:
    - Core fields (task_name, task_namespace, etc.)
    - Build fields (build_id, build_name) - requires join
    - param.* fields (task_data JSONB)
    - artifact.* fields (artifact body_json JSONB) - uses EXISTS subquery
    - status (from latest event)

    Returns:
        Tuple of (condition_string, needs_build_join, artifact_name_for_filter)
        artifact_name_for_filter is set when filtering on artifact.* keys
    """
    sql_op = OPERATORS.get(operator)
    if not sql_op:
        return None, False, None

    # Core fields - direct column access on tasks table
    core_fields = {
        "task_name",
        "task_namespace",
        "task_id",
        "created_at",
        "version",
    }

    if key in core_fields:
        param_name = f"filter_{key}{param_suffix}"
        if sql_op == "ILIKE":
            return f"{task_alias}.{key} ILIKE '%' || :{param_name} || '%'", False, None
        return f"{task_alias}.{key} {sql_op} :{param_name}", False, None

    # Build fields - require join to events and builds tables.
    # builds.id is a Postgres UUID column. Bound parameters arrive from the
    # query string as text and asyncpg sends them as character varying; an
    # implicit `uuid = varchar` comparison is rejected by Postgres
    # ("operator does not exist: uuid = character varying"). Use explicit
    # casts: column-to-text for substring match, parameter-to-uuid for any
    # non-substring comparison (=, !=, <, <=, >, >=) so the PK index is
    # still usable. ``_validate_filter_value`` upstream enforces that
    # non-substring values are valid UUIDs, so the cast can't fail at
    # execution time.
    if key == "build_id":
        param_name = f"filter_build_id{param_suffix}"
        if sql_op == "ILIKE":
            return (
                f"builds.id::text ILIKE '%' || :{param_name} || '%'",
                True,
                None,
            )
        return (
            f"builds.id {sql_op} CAST(:{param_name} AS uuid)",
            True,
            None,
        )

    if key == "build_name":
        param_name = f"filter_build_name{param_suffix}"
        if sql_op == "ILIKE":
            return f"builds.name ILIKE '%' || :{param_name} || '%'", True, None
        return f"builds.name {sql_op} :{param_name}", True, None

    # Parameter fields - JSONB access
    if key.startswith("param."):
        json_path = key[6:]  # Remove 'param.' prefix
        path_parts = json_path.split(".")

        # Build JSONB path access
        jsonb_path = f"{task_alias}.task_data"
        for i, part in enumerate(path_parts):
            # Check for array access like items[0]
            array_match = re.match(r"(\w+)\[(\d+)\]", part)
            if array_match:
                field, index = array_match.groups()
                jsonb_path = f"({jsonb_path}->'{field}')->{index}"
            else:
                if i == len(path_parts) - 1:
                    # Last part - use ->> for text extraction
                    jsonb_path = f"{jsonb_path}->>'{part}'"
                else:
                    jsonb_path = f"{jsonb_path}->'{part}'"

        safe_key = (
            key.replace(".", "_").replace("[", "_").replace("]", "_").replace("-", "_")
        )
        param_name = f"filter_{safe_key}{param_suffix}"

        if sql_op == "ILIKE":
            return f"({jsonb_path}) ILIKE '%' || :{param_name} || '%'", False, None
        elif operator in (">", "<", ">=", "<="):
            # Numeric comparison - cast both sides to float
            # Use CAST() syntax to avoid SQLAlchemy misinterpreting ::float as part of param name
            return (
                (
                    f"CAST({jsonb_path} AS DOUBLE PRECISION) {sql_op} "
                    f"CAST(:{param_name} AS DOUBLE PRECISION)"
                ),
                False,
                None,
            )
        else:
            return f"({jsonb_path}) {sql_op} :{param_name}", False, None

    # Artifact fields - EXISTS subquery on task_artifacts table
    # Format: artifact.{artifact_name}.{json_path}
    if key.startswith("artifact."):
        rest = key[9:]  # Remove 'artifact.' prefix
        parts = rest.split(".", 1)
        if len(parts) < 2:
            return None, False, None

        artifact_name = parts[0]
        json_path = parts[1]
        path_parts = json_path.split(".")

        # Build JSONB path access for artifact body_json
        jsonb_path = "artifact_filter.body_json"
        for i, part in enumerate(path_parts):
            array_match = re.match(r"(\w+)\[(\d+)\]", part)
            if array_match:
                field, index = array_match.groups()
                jsonb_path = f"({jsonb_path}->'{field}')->{index}"
            else:
                if i == len(path_parts) - 1:
                    jsonb_path = f"{jsonb_path}->>'{part}'"
                else:
                    jsonb_path = f"{jsonb_path}->'{part}'"

        safe_key = (
            key.replace(".", "_").replace("[", "_").replace("]", "_").replace("-", "_")
        )
        param_name = f"filter_{safe_key}{param_suffix}"
        artifact_name_param = f"filter_artifact_name{param_suffix}"

        # Build EXISTS subquery condition
        if sql_op == "ILIKE":
            value_condition = f"({jsonb_path}) ILIKE '%' || :{param_name} || '%'"
        elif operator in (">", "<", ">=", "<="):
            value_condition = (
                f"CAST({jsonb_path} AS DOUBLE PRECISION) {sql_op} "
                f"CAST(:{param_name} AS DOUBLE PRECISION)"
            )
        else:
            value_condition = f"({jsonb_path}) {sql_op} :{param_name}"

        # EXISTS subquery to check for matching artifact
        condition = (
            f"EXISTS (SELECT 1 FROM task_artifacts artifact_filter "
            f"WHERE artifact_filter.task_pk = {task_alias}.id "
            f"AND artifact_filter.name = :{artifact_name_param} "
            f"AND {value_condition})"
        )
        return condition, False, artifact_name

    return None, False, None


def _apply_task_filters(
    query: Any,
    environment_id: UUID,
    filter_str: str | None,
    q: str | None,
) -> Any:
    """Apply filter and text search conditions to a task query.

    Only applies conditions that work on the tasks table directly
    (core fields, param.* fields, text search). Does not handle
    build joins, status filters, or artifact filters.
    """
    conditions: list[str] = []
    filter_params: dict[str, str] = {}

    if filter_str:
        parsed_filters = parse_filter_string(filter_str)
        for i, (key, op, value) in enumerate(parsed_filters):
            # Skip status/build/artifact filters (need joins)
            if key in ("status", "build_id", "build_name") or key.startswith(
                "artifact."
            ):
                continue
            param_suffix = f"_{i}"
            condition, requires_build, _ = build_jsonb_condition(
                key, op, value, "tasks", param_suffix
            )
            if condition and not requires_build:
                conditions.append(condition)
                safe_key = (
                    key.replace(".", "_")
                    .replace("[", "_")
                    .replace("]", "_")
                    .replace("-", "_")
                )
                filter_params[f"filter_{safe_key}{param_suffix}"] = value

    if q:
        q_lower = f"%{q.lower()}%"
        conditions.append(
            "(LOWER(tasks.task_name) LIKE :q_param "
            "OR LOWER(tasks.task_namespace) LIKE :q_param)"
        )
        filter_params["q_param"] = q_lower

    if conditions:
        combined_condition = " AND ".join(conditions)
        query = query.where(text(combined_condition).bindparams(**filter_params))

    return query


@router.get("", response_model=TaskSearchResponse)
async def search_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    filter: str | None = None,
    q: str | None = None,  # Text search
    sort: str = "created_at:desc",
    include_artifacts: str | None = None,  # Comma-separated artifact names to include
):
    """Search tasks with advanced filtering.

    Query parameters:
    - filter: Comma-separated filters (e.g., "task_name:~:train,param.lr:>:0.01")
    - q: Text search across task name and namespace
    - sort: Sort field and direction (e.g., "created_at:desc")
    - include_artifacts: Comma-separated artifact names to include in results (e.g., "report,metrics")
    """
    environment_id = auth.environment_id

    # Build base query
    query = select(Task).where(Task.environment_id == environment_id)
    count_query = (
        select(func.count())
        .select_from(Task)
        .where(Task.environment_id == environment_id)
    )

    # Parse and apply filters
    filter_params: dict[str, str] = {}
    conditions: list[str] = []
    needs_build_join = False
    # Status filters are applied post-query since status is derived from events
    status_filters: list[tuple[str, str]] = []  # (operator, value) pairs

    if filter:
        parsed_filters = parse_filter_string(filter)
        for i, (key, op, value) in enumerate(parsed_filters):
            # Handle status filter separately (post-query)
            if key == "status":
                status_filters.append((op, value.lower()))
                continue

            # Use index suffix to ensure unique parameter names for range queries
            # e.g., param.x:>:5 and param.x:<:100 need different param names
            param_suffix = f"_{i}"
            condition, requires_build, asset_name = build_jsonb_condition(
                key, op, value, "tasks", param_suffix
            )
            if condition:
                conditions.append(condition)
                safe_key = (
                    key.replace(".", "_")
                    .replace("[", "_")
                    .replace("]", "_")
                    .replace("-", "_")
                )
                filter_params[f"filter_{safe_key}{param_suffix}"] = value
                if requires_build:
                    needs_build_join = True
                # Add artifact name parameter for artifact.* filters
                if asset_name:
                    filter_params[f"filter_artifact_name{param_suffix}"] = asset_name

    # Text search across name and namespace
    if q:
        q_lower = f"%{q.lower()}%"
        conditions.append(
            "(LOWER(tasks.task_name) LIKE :q_param OR LOWER(tasks.task_namespace) LIKE :q_param)"
        )
        filter_params["q_param"] = q_lower

    # Add build join if needed for build_id/build_name filtering
    if needs_build_join:
        # Join tasks -> events -> builds to filter by build
        # Using a correlated subquery to get the latest event time per task
        latest_event_time_subquery = (
            select(func.max(Event.created_at))
            .where(Event.task_id == Task.id)
            .correlate(Task)
            .scalar_subquery()
        )
        query = query.join(
            Event,
            (Event.task_id == Task.id)
            & (Event.created_at == latest_event_time_subquery),
        ).join(Build, Build.id == Event.build_id)
        count_query = (
            select(func.count())
            .select_from(Task)
            .where(Task.environment_id == environment_id)
            .join(
                Event,
                (Event.task_id == Task.id)
                & (Event.created_at == latest_event_time_subquery),
            )
            .join(Build, Build.id == Event.build_id)
        )

    # Apply conditions using raw SQL for JSONB
    if conditions:
        combined_condition = " AND ".join(conditions)
        query = query.where(text(combined_condition).bindparams(**filter_params))
        count_query = count_query.where(
            text(combined_condition).bindparams(**filter_params)
        )

    # Apply sorting
    sort_parts = sort.split(":")
    sort_field = sort_parts[0] if sort_parts else "created_at"
    sort_dir = sort_parts[1] if len(sort_parts) > 1 else "desc"

    # Map sort field to column
    sort_columns = {
        "created_at": Task.created_at,
        "task_name": Task.task_name,
        "task_namespace": Task.task_namespace,
    }
    sort_column = sort_columns.get(sort_field)

    if sort_column is not None:
        if sort_dir == "asc":
            query = query.order_by(sort_column.asc())
        else:
            query = query.order_by(sort_column.desc())
    elif sort_field.startswith("param.") or sort_field.startswith("artifact."):
        order_dir = "ASC" if sort_dir == "asc" else "DESC"

        if sort_field.startswith("param."):
            # Sort by JSONB field in task_data
            json_key = sort_field[6:]  # Remove "param." prefix
            path_parts = json_key.split(".")
            jsonb_path = "tasks.task_data"
            for i, part in enumerate(path_parts):
                if i == len(path_parts) - 1:
                    jsonb_path = f"{jsonb_path}->>'{part}'"
                else:
                    jsonb_path = f"{jsonb_path}->'{part}'"
        else:
            # Sort by JSONB field in artifact body_json
            # Format: artifact.{name}.{json_path}
            rest = sort_field[9:]  # Remove "artifact." prefix
            parts = rest.split(".", 1)
            artifact_name = parts[0]
            json_path_parts = parts[1].split(".") if len(parts) > 1 else []
            # Build subquery to extract value from artifact
            inner_path = "artifact_sort.body_json"
            for i, part in enumerate(json_path_parts):
                if i == len(json_path_parts) - 1:
                    inner_path = f"{inner_path}->>'{part}'"
                else:
                    inner_path = f"{inner_path}->'{part}'"
            jsonb_path = (
                f"(SELECT {inner_path} FROM task_artifacts artifact_sort "
                f"WHERE artifact_sort.task_pk = tasks.id "
                f"AND artifact_sort.name = '{artifact_name}' LIMIT 1)"
            )

        # Numeric-safe sort: cast to float where possible, fall back to text
        numeric_expr = (
            f"CASE WHEN ({jsonb_path}) ~ '^-?[0-9]+(\\.[0-9]+)?([eE][+-]?[0-9]+)?$' "
            f"THEN CAST({jsonb_path} AS DOUBLE PRECISION) ELSE NULL END"
        )
        text_expr = f"({jsonb_path})"
        query = query.order_by(
            text(f"{numeric_expr} {order_dir} NULLS LAST"),
            text(f"{text_expr} {order_dir} NULLS LAST"),
        )
    else:
        # Unknown sort field - default to created_at
        if sort_dir == "asc":
            query = query.order_by(Task.created_at.asc())
        else:
            query = query.order_by(Task.created_at.desc())

    # If status filter is present, we need to:
    # 1. Fetch all tasks (no SQL pagination) since status is computed post-query
    # 2. Get global statuses for all tasks
    # 3. Filter by status in Python
    # 4. Paginate in Python
    if status_filters:
        # Fetch all matching tasks (limit to 10000 to prevent OOM)
        query = query.limit(10000)
        result = await db.execute(query)
        all_tasks = list(result.scalars().all())

        if all_tasks:
            # Get global statuses for all tasks
            all_task_ids = [t.id for t in all_tasks]
            all_statuses = await get_all_task_global_statuses(db, all_task_ids)

            # Filter by status
            def matches_status_filters(task_id: UUID) -> bool:
                status_info = all_statuses.get(task_id)
                if not status_info:
                    return False
                status_str = status_info[0].value.lower()  # e.g., "completed"
                for op, value in status_filters:
                    if op == "=":
                        if status_str != value:
                            return False
                    elif op == "!=":
                        if status_str == value:
                            return False
                    elif op == "~":  # contains
                        if value not in status_str:
                            return False
                return True

            filtered_tasks = [t for t in all_tasks if matches_status_filters(t.id)]
            total = len(filtered_tasks)

            # Apply pagination in Python
            start_idx = (page - 1) * page_size
            end_idx = start_idx + page_size
            tasks = filtered_tasks[start_idx:end_idx]
        else:
            tasks = []
            total = 0
    else:
        # No status filter - use normal SQL pagination
        total_result = await db.execute(count_query)
        total = total_result.scalar() or 0

        # Apply pagination
        query = query.offset((page - 1) * page_size).limit(page_size)

        # Execute query
        result = await db.execute(query)
        tasks = result.scalars().all()

    # Get global status for each task (considers events across ALL builds)
    task_ids = [t.id for t in tasks]
    task_status_map: dict[
        UUID,
        tuple[UUID | None, str | None, TaskStatus, str | None, str | None, str | None],
    ] = {}

    if task_ids:
        # Get global statuses - this looks at events across all builds
        # to determine the true status (e.g., COMPLETED takes precedence)
        global_statuses = await get_all_task_global_statuses(db, task_ids)

        # Get the most recent build context for display (for build_id/build_name)
        # Using a subquery to find the latest event per task
        latest_event_subquery = (
            select(
                Event.task_id,
                func.max(Event.created_at).label("latest_event_time"),
            )
            .where(Event.task_id.in_(task_ids))
            .group_by(Event.task_id)
            .subquery()
        )

        latest_events = select(Event).join(
            latest_event_subquery,
            (Event.task_id == latest_event_subquery.c.task_id)
            & (Event.created_at == latest_event_subquery.c.latest_event_time),
        )
        events_result = await db.execute(latest_events)
        latest_events_list = events_result.scalars().all()

        # Get build info for display
        build_ids = {e.build_id for e in latest_events_list}
        builds_map: dict[UUID, Build] = {}
        if build_ids:
            builds_result = await db.execute(
                select(Build).where(Build.id.in_(build_ids))
            )
            builds_map = {b.id: b for b in builds_result.scalars().all()}

        # Map tasks to their latest build context and global status
        task_to_latest_build: dict[UUID, Build] = {}
        for event in latest_events_list:
            if event.task_id and event.build_id in builds_map:
                task_to_latest_build[event.task_id] = builds_map[event.build_id]

        # Build final status map using global status but latest build for context
        for task_id in task_ids:
            # Global status: (status, started_at, completed_at, error_message, status_build_id, waiting_for_lock, commit_hash)
            global_status = global_statuses.get(
                task_id, (TaskStatus.PENDING, None, None, None, None, False, None)
            )
            latest_build = task_to_latest_build.get(task_id)

            task_status_map[task_id] = (
                latest_build.id if latest_build else None,  # build_id for display
                latest_build.name if latest_build else None,  # build_name for display
                global_status[0],  # status (global)
                global_status[1].isoformat()
                if global_status[1]
                else None,  # started_at
                global_status[2].isoformat()
                if global_status[2]
                else None,  # completed_at
                global_status[3],  # error_message
            )

    # Get artifact counts
    artifact_counts: dict[UUID, int] = {}
    if task_ids:
        artifact_count_result = await db.execute(
            select(TaskArtifact.task_pk, func.count(TaskArtifact.id))
            .where(TaskArtifact.task_pk.in_(task_ids))
            .group_by(TaskArtifact.task_pk)
        )
        artifact_counts = {row[0]: row[1] for row in artifact_count_result.all()}

    # Get artifact data for requested artifacts
    task_artifact_data: dict[
        UUID, dict[str, dict]
    ] = {}  # task_pk -> artifact_name -> body_json
    if task_ids and include_artifacts:
        artifact_names = [
            name.strip() for name in include_artifacts.split(",") if name.strip()
        ]
        if artifact_names:
            artifact_query = select(
                TaskArtifact.task_pk,
                TaskArtifact.name,
                TaskArtifact.body_json,
            ).where(
                TaskArtifact.task_pk.in_(task_ids),
                TaskArtifact.name.in_(artifact_names),
            )
            artifact_result = await db.execute(artifact_query)
            for task_pk, artifact_name, body_json in artifact_result.all():
                if task_pk not in task_artifact_data:
                    task_artifact_data[task_pk] = {}
                task_artifact_data[task_pk][artifact_name] = body_json or {}

    # Build response
    task_results = []
    for task in tasks:
        status_info = task_status_map.get(task.id)
        task_results.append(
            TaskSearchResult(
                task_id=task.task_id,
                environment_id=task.environment_id,
                task_namespace=task.task_namespace,
                task_name=task.task_name,
                task_data=task.task_data,
                version=task.version,
                output_uri=task.output_uri,
                created_at=task.created_at,
                build_id=status_info[0] if status_info else None,
                build_name=status_info[1] if status_info else None,
                status=status_info[2] if status_info else TaskStatus.PENDING,
                started_at=status_info[3] if status_info else None,  # type: ignore
                completed_at=status_info[4] if status_info else None,  # type: ignore
                error_message=status_info[5] if status_info else None,
                artifact_count=artifact_counts.get(task.id, 0),
                artifact_data=task_artifact_data.get(task.id, {}),
            )
        )

    # Get available columns (core + discovered param keys)
    available_columns = [
        "task_name",
        "task_namespace",
        "status",
        "build_name",
        "created_at",
    ]

    return TaskSearchResponse(
        tasks=task_results,
        total=total,
        page=page,
        page_size=page_size,
        available_columns=available_columns,
    )


@router.get("/keys", response_model=KeySuggestionsResponse)
async def get_key_suggestions(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    prefix: str = "",
    limit: int = 20,
    filter: str | None = None,
    q: str | None = None,
):
    """Get key suggestions for autocomplete.

    Returns available filter keys including:
    - Core fields (task_name, task_namespace, etc.)
    - Discovered param.* keys from task_data
    - Discovered artifact.* keys from artifact body_json

    When filter/q are provided, keys are discovered only from matching tasks.
    """
    environment_id = auth.environment_id
    has_filter = bool(filter or q)

    # Core keys always available
    core_keys = [
        KeySuggestion(key="task_name", type="string"),
        KeySuggestion(key="task_namespace", type="string"),
        KeySuggestion(key="task_id", type="string"),
        KeySuggestion(key="status", type="string"),
        KeySuggestion(key="build_id", type="string"),
        KeySuggestion(key="build_name", type="string"),
        KeySuggestion(key="created_at", type="datetime"),
    ]

    # Filter by prefix
    if (
        prefix
        and not prefix.startswith("param.")
        and not prefix.startswith("artifact.")
    ):
        core_keys = [k for k in core_keys if k.key.startswith(prefix)]

    # Get param keys from task_data
    param_keys: list[KeySuggestion] = []

    if not prefix or prefix.startswith("param"):
        # When filtering, bypass cache and query matching tasks directly
        cache_key = f"keys:{environment_id}"
        cached_param_keys = None if has_filter else _get_cached(cache_key)

        if cached_param_keys is None:
            sample_query = (
                select(Task.task_data)
                .where(Task.environment_id == environment_id)
                .order_by(Task.created_at.desc())
                .limit(500)
            )
            if has_filter:
                sample_query = _apply_task_filters(
                    sample_query, environment_id, filter, q
                )
            sample_result = await db.execute(sample_query)
            sample_tasks = sample_result.scalars().all()

            key_counter: Counter[str] = Counter()
            for task_data in sample_tasks:
                if isinstance(task_data, dict):
                    _extract_keys(task_data, "param", key_counter)

            cached_param_keys = key_counter.most_common()
            if not has_filter:
                _set_cached(cache_key, cached_param_keys)

        # Filter by prefix and convert to suggestions
        prefix_filter = prefix[6:] if prefix.startswith("param.") else ""
        for key, count in cached_param_keys:
            if not prefix_filter or key.startswith(f"param.{prefix_filter}"):
                param_keys.append(KeySuggestion(key=key, type="string", count=count))
            if len(param_keys) >= limit:
                break

    # Get artifact keys from TaskArtifact.body_json
    artifact_keys: list[KeySuggestion] = []

    if not prefix or prefix.startswith("artifact"):
        artifact_cache_key = f"artifact_keys:{environment_id}"
        cached_artifact_keys = None if has_filter else _get_cached(artifact_cache_key)

        if cached_artifact_keys is None:
            # When filtering, get artifact keys only from matching tasks
            artifact_query = (
                select(TaskArtifact.name, TaskArtifact.body_json)
                .where(TaskArtifact.environment_id == environment_id)
                .order_by(TaskArtifact.created_at.desc())
                .limit(500)
            )
            if has_filter:
                # Join to tasks table to apply filters
                matching_task_ids = (
                    select(Task.id)
                    .where(Task.environment_id == environment_id)
                    .limit(500)
                )
                matching_task_ids = _apply_task_filters(
                    matching_task_ids, environment_id, filter, q
                )
                artifact_query = artifact_query.where(
                    TaskArtifact.task_pk.in_(matching_task_ids)
                )

            artifact_result = await db.execute(artifact_query)
            sample_artifacts = artifact_result.all()

            artifact_key_counter: Counter[str] = Counter()
            for artifact_name, body_json in sample_artifacts:
                if isinstance(body_json, dict):
                    _extract_keys(
                        body_json, f"artifact.{artifact_name}", artifact_key_counter
                    )

            cached_artifact_keys = artifact_key_counter.most_common()
            if not has_filter:
                _set_cached(artifact_cache_key, cached_artifact_keys)

        # Filter by prefix and convert to suggestions
        prefix_filter = prefix[9:] if prefix.startswith("artifact.") else ""
        for key, count in cached_artifact_keys:
            if not prefix_filter or key.startswith(f"artifact.{prefix_filter}"):
                artifact_keys.append(KeySuggestion(key=key, type="string", count=count))
            if len(artifact_keys) >= limit:
                break

    all_keys = core_keys + param_keys + artifact_keys
    return KeySuggestionsResponse(keys=all_keys[:limit])


def _extract_keys(data: dict, prefix: str, counter: Counter[str], max_depth: int = 8):
    """Recursively extract keys from nested dict."""
    if max_depth <= 0:
        return

    for key, value in data.items():
        full_key = f"{prefix}.{key}"
        counter[full_key] += 1

        if isinstance(value, dict):
            _extract_keys(value, full_key, counter, max_depth - 1)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            # Sample first element of list
            _extract_keys(value[0], f"{full_key}[0]", counter, max_depth - 1)


@router.get("/values", response_model=ValueSuggestionsResponse)
async def get_value_suggestions(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    key: str,
    prefix: str = "",
    limit: int = 20,
):
    """Get value suggestions for a specific key.

    Returns common values for the specified key.
    """
    environment_id = auth.environment_id

    # Handle status specially (no caching needed - static values).
    # Keep this list in sync with TaskStatus on the API side and the
    # TaskStatus type on the UI side. "unregistered" is intentionally
    # excluded — it's an internal phantom-row marker, not a status users
    # filter on.
    if key == "status":
        values = [
            ValueSuggestion(value="pending"),
            ValueSuggestion(value="running"),
            ValueSuggestion(value="suspended"),
            ValueSuggestion(value="completed"),
            ValueSuggestion(value="failed"),
            ValueSuggestion(value="skipped"),
            ValueSuggestion(value="cancelled"),
        ]
        if prefix:
            values = [v for v in values if v.value.startswith(prefix)]
        return ValueSuggestionsResponse(values=values)

    # Handle build_id and build_name - query from builds table
    if key in ("build_id", "build_name"):
        cache_key = f"values:{environment_id}:{key}"
        cached_values = _get_cached(cache_key)

        if cached_values is None:
            column = Build.id if key == "build_id" else Build.name
            # Get builds in workspace via events
            query = (
                select(column, func.count(column))
                .select_from(Build)
                .join(Event, Event.build_id == Build.id)
                .join(Task, Task.id == Event.task_id)
                .where(Task.environment_id == environment_id)
                .group_by(column)
                .order_by(func.count(column).desc())
                .limit(100)
            )
            result = await db.execute(query)
            cached_values = [(str(row[0]), row[1]) for row in result.all() if row[0]]
            _set_cached(cache_key, cached_values)

        # Filter by prefix
        values = [
            ValueSuggestion(value=v, count=c)
            for v, c in cached_values
            if not prefix or v.lower().startswith(prefix.lower())
        ][:limit]
        return ValueSuggestionsResponse(values=values)

    # For core string fields, get distinct values (cached per workspace+key)
    core_fields = {"task_name": Task.task_name, "task_namespace": Task.task_namespace}

    if key in core_fields:
        cache_key = f"values:{environment_id}:{key}"
        cached_values = _get_cached(cache_key)

        if cached_values is None:
            column = core_fields[key]
            query = (
                select(column, func.count(column))
                .where(Task.environment_id == environment_id)
                .group_by(column)
                .order_by(func.count(column).desc())
                .limit(100)  # Cache more values for filtering
            )
            result = await db.execute(query)
            cached_values = [(str(row[0]), row[1]) for row in result.all() if row[0]]
            _set_cached(cache_key, cached_values)

        # Filter by prefix
        values = [
            ValueSuggestion(value=v, count=c)
            for v, c in cached_values
            if not prefix or v.lower().startswith(prefix.lower())
        ][:limit]
        return ValueSuggestionsResponse(values=values)

    # For param.* fields, sample from task_data (cached per workspace+key)
    if key.startswith("param."):
        cache_key = f"values:{environment_id}:{key}"
        cached_values = _get_cached(cache_key)

        if cached_values is None:
            json_path = key[6:].split(".")

            # Sample recent tasks
            sample_query = (
                select(Task.task_data)
                .where(Task.environment_id == environment_id)
                .order_by(Task.created_at.desc())
                .limit(500)
            )
            sample_result = await db.execute(sample_query)
            sample_tasks = sample_result.scalars().all()

            # Extract values for the specified path
            value_counter: Counter[str] = Counter()
            for task_data in sample_tasks:
                if isinstance(task_data, dict):
                    value = _get_nested_value(task_data, json_path)
                    if value is not None:
                        value_counter[str(value)] += 1

            # Cache all discovered values (up to 100)
            cached_values = list(value_counter.most_common(100))
            _set_cached(cache_key, cached_values)

        # Filter by prefix
        values = [
            ValueSuggestion(value=v, count=c)
            for v, c in cached_values
            if not prefix or v.startswith(prefix)
        ][:limit]
        return ValueSuggestionsResponse(values=values)

    # For artifact.* fields, sample from TaskArtifact.body_json
    if key.startswith("artifact."):
        cache_key = f"values:{environment_id}:{key}"
        cached_values = _get_cached(cache_key)

        if cached_values is None:
            # Parse artifact.{name}.{path} format
            parts = key[9:].split(
                ".", 1
            )  # Remove 'artifact.' prefix, split at first dot
            if len(parts) < 2:
                return ValueSuggestionsResponse(values=[])

            artifact_name = parts[0]
            json_path = parts[1].split(".")

            # Sample artifacts with matching name
            sample_query = (
                select(TaskArtifact.body_json)
                .where(TaskArtifact.environment_id == environment_id)
                .where(TaskArtifact.name == artifact_name)
                .order_by(TaskArtifact.created_at.desc())
                .limit(500)
            )
            sample_result = await db.execute(sample_query)
            sample_artifacts = sample_result.scalars().all()

            # Extract values for the specified path
            value_counter: Counter[str] = Counter()
            for body_json in sample_artifacts:
                if isinstance(body_json, dict):
                    value = _get_nested_value(body_json, json_path)
                    if value is not None:
                        value_counter[str(value)] += 1

            # Cache all discovered values (up to 100)
            cached_values = list(value_counter.most_common(100))
            _set_cached(cache_key, cached_values)

        # Filter by prefix
        values = [
            ValueSuggestion(value=v, count=c)
            for v, c in cached_values
            if not prefix or v.startswith(prefix)
        ][:limit]
        return ValueSuggestionsResponse(values=values)

    return ValueSuggestionsResponse(values=[])


def _get_nested_value(data: dict, path: list[str]) -> str | None:
    """Get a nested value from a dict using a path."""
    current = data
    for part in path:
        # Handle array access
        array_match = re.match(r"(\w+)\[(\d+)\]", part)
        if array_match:
            field, index = array_match.groups()
            if isinstance(current, dict) and field in current:
                current = current[field]
                if isinstance(current, list) and int(index) < len(current):
                    current = current[int(index)]
                else:
                    return None
            else:
                return None
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None

    if isinstance(current, (str, int, float, bool)):
        return str(current)
    return None


@router.get("/columns", response_model=AvailableColumnsResponse)
async def get_available_columns(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    filter: str | None = None,
    q: str | None = None,
):
    """Get all available columns for the results table.

    When filter/q are provided, columns are discovered only from matching tasks.

    Returns:
    - Core columns (always available)
    - Param columns (discovered from task_data)
    - Artifact columns (discovered from artifacts)
    """
    environment_id = auth.environment_id

    core = [
        "task_id",
        "task_name",
        "task_namespace",
        "status",
        "build_id",
        "build_name",
        "created_at",
        "started_at",
        "completed_at",
    ]

    # Discover param keys from matching tasks
    sample_query = (
        select(Task.task_data)
        .where(Task.environment_id == environment_id)
        .order_by(Task.created_at.desc())
        .limit(500)
    )
    if filter or q:
        sample_query = _apply_task_filters(sample_query, environment_id, filter, q)

    sample_result = await db.execute(sample_query)
    sample_tasks = sample_result.scalars().all()

    key_counter: Counter[str] = Counter()
    for task_data in sample_tasks:
        if isinstance(task_data, dict):
            _extract_keys(task_data, "param", key_counter)

    params = [k for k, _ in key_counter.most_common()]

    # Discover artifact keys from matching tasks
    artifact_query = (
        select(TaskArtifact.name, TaskArtifact.body_json)
        .where(TaskArtifact.environment_id == environment_id)
        .order_by(TaskArtifact.created_at.desc())
        .limit(500)
    )
    if filter or q:
        matching_task_ids = (
            select(Task.id).where(Task.environment_id == environment_id).limit(500)
        )
        matching_task_ids = _apply_task_filters(
            matching_task_ids, environment_id, filter, q
        )
        artifact_query = artifact_query.where(
            TaskArtifact.task_pk.in_(matching_task_ids)
        )

    artifact_result = await db.execute(artifact_query)
    sample_artifacts = artifact_result.all()

    artifact_key_counter: Counter[str] = Counter()
    for artifact_name, body_json in sample_artifacts:
        if isinstance(body_json, dict):
            _extract_keys(body_json, f"artifact.{artifact_name}", artifact_key_counter)

    artifacts = [k for k, _ in artifact_key_counter.most_common()]

    return AvailableColumnsResponse(core=core, params=params, artifacts=artifacts)
