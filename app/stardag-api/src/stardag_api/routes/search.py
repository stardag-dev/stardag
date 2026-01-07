"""Task search routes - advanced filtering and autocomplete."""

import re
from collections import Counter
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from stardag_api.auth import SdkAuth, require_sdk_auth
from stardag_api.db import get_db
from stardag_api.models import Build, Event, Task, TaskRegistryAsset
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
from stardag_api.services.status import get_all_task_statuses_in_build

router = APIRouter(prefix="/tasks/search", tags=["search"])


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


def parse_filter_string(filter_str: str) -> list[tuple[str, str, str]]:
    """Parse filter string into list of (key, operator, value) tuples.

    Format: key:op:value,key:op:value,...
    Examples:
        - task_name:=:training
        - param.lr:>:0.01
        - task_namespace:~:ml
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
                filters.append((key.strip(), op, value.strip()))

    return filters


def build_jsonb_condition(
    key: str, operator: str, value: str, task_alias: str = "tasks"
) -> str | None:
    """Build a SQL condition for JSONB filtering.

    Handles:
    - Core fields (task_name, task_namespace, etc.)
    - param.* fields (task_data JSONB)
    - status (from latest event)
    """
    sql_op = OPERATORS.get(operator)
    if not sql_op:
        return None

    # Core fields - direct column access
    core_fields = {
        "task_name",
        "task_namespace",
        "task_id",
        "created_at",
        "version",
    }

    if key in core_fields:
        if sql_op == "ILIKE":
            return f"{task_alias}.{key} ILIKE '%' || :filter_{key} || '%'"
        return f"{task_alias}.{key} {sql_op} :filter_{key}"

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

        safe_key = key.replace(".", "_").replace("[", "_").replace("]", "_")

        if sql_op == "ILIKE":
            return f"({jsonb_path}) ILIKE '%' || :filter_{safe_key} || '%'"
        elif operator in (">", "<", ">=", "<="):
            # Numeric comparison - cast both sides to float
            # Use CAST() syntax to avoid SQLAlchemy misinterpreting ::float as part of param name
            return f"CAST({jsonb_path} AS DOUBLE PRECISION) {sql_op} CAST(:filter_{safe_key} AS DOUBLE PRECISION)"
        else:
            return f"({jsonb_path}) {sql_op} :filter_{safe_key}"

    return None


@router.get("", response_model=TaskSearchResponse)
async def search_tasks(
    db: Annotated[AsyncSession, Depends(get_db)],
    auth: Annotated[SdkAuth, Depends(require_sdk_auth)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
    filter: str | None = None,
    q: str | None = None,  # Text search
    sort: str = "created_at:desc",
):
    """Search tasks with advanced filtering.

    Query parameters:
    - filter: Comma-separated filters (e.g., "task_name:~:train,param.lr:>:0.01")
    - q: Text search across task name and namespace
    - sort: Sort field and direction (e.g., "created_at:desc")
    """
    workspace_id = auth.workspace_id

    # Build base query
    query = select(Task).where(Task.workspace_id == workspace_id)
    count_query = (
        select(func.count()).select_from(Task).where(Task.workspace_id == workspace_id)
    )

    # Parse and apply filters
    filter_params: dict[str, str] = {}
    conditions: list[str] = []

    if filter:
        parsed_filters = parse_filter_string(filter)
        for key, op, value in parsed_filters:
            condition = build_jsonb_condition(key, op, value, "tasks")
            if condition:
                conditions.append(condition)
                safe_key = key.replace(".", "_").replace("[", "_").replace("]", "_")
                filter_params[f"filter_{safe_key}"] = value

    # Text search across name and namespace
    if q:
        q_lower = f"%{q.lower()}%"
        conditions.append(
            "(LOWER(tasks.task_name) LIKE :q_param OR LOWER(tasks.task_namespace) LIKE :q_param)"
        )
        filter_params["q_param"] = q_lower

    # Apply conditions using raw SQL for JSONB
    if conditions:
        combined_condition = " AND ".join(conditions)
        query = query.where(text(combined_condition).bindparams(**filter_params))
        count_query = count_query.where(
            text(combined_condition).bindparams(**filter_params)
        )

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

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
    sort_column = sort_columns.get(sort_field, Task.created_at)
    if sort_dir == "asc":
        query = query.order_by(sort_column.asc())
    else:
        query = query.order_by(sort_column.desc())

    # Apply pagination
    query = query.offset((page - 1) * page_size).limit(page_size)

    # Execute query
    result = await db.execute(query)
    tasks = result.scalars().all()

    # Get latest build context for each task
    task_ids = [t.id for t in tasks]
    task_build_map: dict[
        int, tuple[str, str, TaskStatus, str | None, str | None, str | None]
    ] = {}

    if task_ids:
        # Get most recent build for each task via events
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

        # Get build info for these events
        build_ids = {e.build_id for e in latest_events_list}
        if build_ids:
            builds_result = await db.execute(
                select(Build).where(Build.id.in_(build_ids))
            )
            builds_map = {b.id: b for b in builds_result.scalars().all()}

            # Get status for each task in its latest build
            for event in latest_events_list:
                build = builds_map.get(event.build_id)
                if build and event.task_id:
                    # Get full status for this task in this build
                    statuses = await get_all_task_statuses_in_build(db, event.build_id)
                    status_info = statuses.get(
                        event.task_id, (TaskStatus.PENDING, None, None, None)
                    )
                    task_build_map[event.task_id] = (
                        build.id,
                        build.name,
                        status_info[0],  # status
                        status_info[1].isoformat()
                        if status_info[1]
                        else None,  # started_at
                        status_info[2].isoformat()
                        if status_info[2]
                        else None,  # completed_at
                        status_info[3],  # error_message
                    )

    # Get asset counts
    asset_counts: dict[int, int] = {}
    if task_ids:
        asset_count_result = await db.execute(
            select(TaskRegistryAsset.task_pk, func.count(TaskRegistryAsset.id))
            .where(TaskRegistryAsset.task_pk.in_(task_ids))
            .group_by(TaskRegistryAsset.task_pk)
        )
        asset_counts = {row[0]: row[1] for row in asset_count_result.all()}

    # Build response
    task_results = []
    for task in tasks:
        build_info = task_build_map.get(task.id)
        task_results.append(
            TaskSearchResult(
                task_id=task.task_id,
                workspace_id=task.workspace_id,
                task_namespace=task.task_namespace,
                task_name=task.task_name,
                task_data=task.task_data,
                version=task.version,
                created_at=task.created_at,
                build_id=build_info[0] if build_info else None,
                build_name=build_info[1] if build_info else None,
                status=build_info[2] if build_info else TaskStatus.PENDING,
                started_at=build_info[3] if build_info else None,  # type: ignore
                completed_at=build_info[4] if build_info else None,  # type: ignore
                error_message=build_info[5] if build_info else None,
                asset_count=asset_counts.get(task.id, 0),
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
):
    """Get key suggestions for autocomplete.

    Returns available filter keys including:
    - Core fields (task_name, task_namespace, etc.)
    - Discovered param.* keys from task_data
    """
    workspace_id = auth.workspace_id

    # Core keys always available
    core_keys = [
        KeySuggestion(key="task_name", type="string"),
        KeySuggestion(key="task_namespace", type="string"),
        KeySuggestion(key="task_id", type="string"),
        KeySuggestion(key="status", type="string"),
        KeySuggestion(key="created_at", type="datetime"),
    ]

    # Filter by prefix
    if prefix and not prefix.startswith("param."):
        core_keys = [k for k in core_keys if k.key.startswith(prefix)]

    # Get param keys from task_data
    # Sample recent tasks to discover common keys
    param_keys: list[KeySuggestion] = []

    if not prefix or prefix.startswith("param"):
        # Get a sample of task_data to discover keys
        sample_query = (
            select(Task.task_data)
            .where(Task.workspace_id == workspace_id)
            .order_by(Task.created_at.desc())
            .limit(100)
        )
        sample_result = await db.execute(sample_query)
        sample_tasks = sample_result.scalars().all()

        # Extract unique keys from task_data
        key_counter: Counter[str] = Counter()
        for task_data in sample_tasks:
            if isinstance(task_data, dict):
                _extract_keys(task_data, "param", key_counter)

        # Filter by prefix and convert to suggestions
        prefix_filter = prefix[6:] if prefix.startswith("param.") else ""
        for key, count in key_counter.most_common(limit):
            if not prefix_filter or key.startswith(f"param.{prefix_filter}"):
                param_keys.append(KeySuggestion(key=key, type="string", count=count))

    all_keys = core_keys + param_keys
    return KeySuggestionsResponse(keys=all_keys[:limit])


def _extract_keys(data: dict, prefix: str, counter: Counter[str], max_depth: int = 3):
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
    workspace_id = auth.workspace_id

    # Handle status specially
    if key == "status":
        values = [
            ValueSuggestion(value="pending"),
            ValueSuggestion(value="running"),
            ValueSuggestion(value="completed"),
            ValueSuggestion(value="failed"),
        ]
        if prefix:
            values = [v for v in values if v.value.startswith(prefix)]
        return ValueSuggestionsResponse(values=values)

    # For core string fields, get distinct values
    core_fields = {"task_name": Task.task_name, "task_namespace": Task.task_namespace}

    if key in core_fields:
        column = core_fields[key]
        query = (
            select(column, func.count(column))
            .where(Task.workspace_id == workspace_id)
            .group_by(column)
            .order_by(func.count(column).desc())
            .limit(limit)
        )
        if prefix:
            query = query.where(column.ilike(f"{prefix}%"))

        result = await db.execute(query)
        values = [
            ValueSuggestion(value=str(row[0]), count=row[1])
            for row in result.all()
            if row[0]
        ]
        return ValueSuggestionsResponse(values=values)

    # For param.* fields, sample from task_data
    if key.startswith("param."):
        json_path = key[6:].split(".")

        # Sample recent tasks
        sample_query = (
            select(Task.task_data)
            .where(Task.workspace_id == workspace_id)
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
                    str_value = str(value)
                    if not prefix or str_value.startswith(prefix):
                        value_counter[str_value] += 1

        values = [
            ValueSuggestion(value=v, count=c)
            for v, c in value_counter.most_common(limit)
        ]
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
):
    """Get all available columns for the results table.

    Returns:
    - Core columns (always available)
    - Param columns (discovered from task_data)
    - Asset columns (discovered from assets)
    """
    workspace_id = auth.workspace_id

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

    # Discover param keys
    sample_query = (
        select(Task.task_data)
        .where(Task.workspace_id == workspace_id)
        .order_by(Task.created_at.desc())
        .limit(100)
    )
    sample_result = await db.execute(sample_query)
    sample_tasks = sample_result.scalars().all()

    key_counter: Counter[str] = Counter()
    for task_data in sample_tasks:
        if isinstance(task_data, dict):
            _extract_keys(task_data, "param", key_counter)

    params = [k for k, _ in key_counter.most_common(50)]

    # Discover asset keys (would need to sample body_json)
    assets: list[str] = []  # Placeholder - would need similar logic for assets

    return AvailableColumnsResponse(core=core, params=params, assets=assets)
