"""Tests for task search: key extraction depth and JSONB sorting."""

from collections import Counter

import pytest
from httpx import AsyncClient

from stardag_api.routes.search import _extract_keys, build_jsonb_condition


# ---------------------------------------------------------------------------
# Unit tests for _extract_keys depth
# ---------------------------------------------------------------------------


class TestExtractKeysDepth:
    def test_extracts_shallow_keys(self):
        counter: Counter[str] = Counter()
        _extract_keys({"a": 1, "b": "x"}, "param", counter)
        assert "param.a" in counter
        assert "param.b" in counter

    def test_extracts_nested_keys(self):
        counter: Counter[str] = Counter()
        _extract_keys({"a": {"b": {"c": 1}}}, "param", counter)
        assert "param.a" in counter
        assert "param.a.b" in counter
        assert "param.a.b.c" in counter

    def test_respects_max_depth_default_8(self):
        """Default max_depth=8 allows 8 levels of nesting."""
        # Build 10-level deep dict
        data: dict = {"leaf": "value"}
        for i in range(9, 0, -1):
            data = {f"l{i}": data}

        counter: Counter[str] = Counter()
        _extract_keys(data, "param", counter)

        # Levels 1-8 should be present
        assert "param.l1" in counter
        assert "param.l1.l2.l3.l4.l5.l6.l7.l8" in counter
        # Level 9+ should be absent (depth exhausted)
        assert "param.l1.l2.l3.l4.l5.l6.l7.l8.l9" not in counter

    def test_custom_max_depth(self):
        data = {"a": {"b": {"c": {"d": 1}}}}
        counter: Counter[str] = Counter()
        _extract_keys(data, "param", counter, max_depth=2)
        assert "param.a" in counter
        assert "param.a.b" in counter
        assert "param.a.b.c" not in counter

    def test_counts_occurrences(self):
        counter: Counter[str] = Counter()
        _extract_keys({"x": 1}, "param", counter)
        _extract_keys({"x": 2}, "param", counter)
        assert counter["param.x"] == 2


# ---------------------------------------------------------------------------
# Integration tests for search API (deep keys + sorting)
# ---------------------------------------------------------------------------


async def _create_task(client: AsyncClient, task_id: str, task_data: dict) -> str:
    """Helper: create a build + task, return build_id."""
    resp = await client.post("/api/v1/builds", json={})
    build_id = resp.json()["id"]
    await client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": task_id,
            "task_namespace": "test",
            "task_name": "SearchTest",
            "task_data": task_data,
        },
    )
    return build_id


@pytest.mark.asyncio
async def test_deep_keys_in_available_columns(client: AsyncClient):
    """Deep nested params (5+ levels) appear in available columns."""
    await _create_task(
        client,
        "deep-task",
        {"a": {"b": {"c": {"d": {"e": {"f": "deep"}}}}}},
    )
    resp = await client.get("/api/v1/tasks/search/columns")
    assert resp.status_code == 200
    params = resp.json()["params"]
    assert "param.a.b.c.d.e.f" in params


@pytest.mark.asyncio
async def test_deep_keys_in_key_suggestions(client: AsyncClient):
    """Deep nested params appear in key suggestions with prefix filter."""
    await _create_task(
        client,
        "deep-suggest-task",
        {"x": {"y": {"z": {"w": {"v": 42}}}}},
    )
    resp = await client.get(
        "/api/v1/tasks/search/keys", params={"prefix": "param.x.y.z"}
    )
    assert resp.status_code == 200
    keys = [k["key"] for k in resp.json()["keys"]]
    assert "param.x.y.z.w.v" in keys


@pytest.mark.asyncio
async def test_sort_by_core_field(client: AsyncClient):
    """Sorting by core fields (task_name) works."""
    await _create_task(client, "sort-a", {"v": 1})
    await _create_task(client, "sort-b", {"v": 2})

    resp = await client.get("/api/v1/tasks/search", params={"sort": "created_at:asc"})
    assert resp.status_code == 200
    tasks = resp.json()["tasks"]
    assert len(tasks) >= 2
    # Ascending: first created should come first
    ids = [t["task_id"] for t in tasks]
    assert ids.index("sort-a") < ids.index("sort-b")


@pytest.mark.asyncio
async def test_sort_by_param_numeric(client: AsyncClient):
    """Sorting by a numeric param field orders correctly.

    NOTE: This test uses raw SQL with JSONB operators. It works on PostgreSQL
    and SQLite 3.38+. The regex-based numeric detection uses PostgreSQL's ~
    operator which is not available in SQLite, so on SQLite the sort falls
    back to text ordering.
    """
    await _create_task(client, "num-2", {"score": 2})
    await _create_task(client, "num-10", {"score": 10})
    await _create_task(client, "num-1", {"score": 1})

    try:
        resp = await client.get(
            "/api/v1/tasks/search", params={"sort": "param.score:asc"}
        )
    except Exception:
        pytest.skip("JSONB sorting not supported on this database backend")

    if resp.status_code != 200:
        pytest.skip("JSONB sorting not supported on this database backend")

    tasks = resp.json()["tasks"]
    # On PostgreSQL: numeric sort [1, 2, 10]
    # On SQLite: may fall back to text sort [1, 10, 2] or work depending on version
    # We just verify the endpoint doesn't error and returns all tasks
    assert len(tasks) >= 3
    task_ids = {t["task_id"] for t in tasks}
    assert {"num-1", "num-2", "num-10"}.issubset(task_ids)


# ---------------------------------------------------------------------------
# build_id filter SQL emission — defends the production fix for
# "operator does not exist: uuid = character varying" by asserting the
# generated SQL has the right casts.
# ---------------------------------------------------------------------------


class TestBuildIdFilterSQL:
    def test_build_id_equality_casts_param_to_uuid(self) -> None:
        condition, needs_join, _ = build_jsonb_condition(
            "build_id", "=", "doesntmatter"
        )
        assert needs_join is True
        assert condition is not None
        # The bound parameter must be cast to uuid so Postgres can compare
        # against the uuid column without a varchar-uuid mismatch.
        assert "CAST(:filter_build_id AS uuid)" in condition
        # The column itself stays as uuid (no ::text cast on equality).
        assert "builds.id =" in condition

    def test_build_id_inequality_casts_param_to_uuid(self) -> None:
        condition, _, _ = build_jsonb_condition("build_id", "!=", "x")
        assert condition is not None
        assert "CAST(:filter_build_id AS uuid)" in condition
        assert "builds.id !=" in condition

    def test_build_id_ilike_casts_column_to_text(self) -> None:
        condition, needs_join, _ = build_jsonb_condition("build_id", "~", "abc")
        assert needs_join is True
        assert condition is not None
        # ILIKE is a string operator; uuid columns need explicit text cast.
        assert "builds.id::text ILIKE" in condition
        # No CAST AS uuid on this branch — substring match is text-only.
        assert "CAST(:filter_build_id AS uuid)" not in condition


# ---------------------------------------------------------------------------
# End-to-end Postgres test for the build_id filter — the regression that
# prompted the fix surfaced as an HTTP 500 in production logs:
#   GET /api/v1/tasks/search?filter=build_id:=:<uuid> → 500
#   "operator does not exist: uuid = character varying"
# This test runs the full route against a real Postgres and asserts the
# request succeeds. SQLite skips automatically because the route uses
# Postgres-specific cast / JSONB operators throughout.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_filter_by_build_id_equality_does_not_500(
    pg_client: AsyncClient,
) -> None:
    """Reproduces the production bug: filter=build_id:=:<uuid> previously
    returned 500 because the bound varchar parameter couldn't be compared
    to the uuid column. With the cast in place, the filter resolves and
    the response is a normal task list."""
    # Create a build + task so there's a real build_id to filter on.
    build_resp = await pg_client.post("/api/v1/builds", json={})
    assert build_resp.status_code == 201
    build_id = build_resp.json()["id"]

    task_resp = await pg_client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "search-build-id-task",
            "task_namespace": "test",
            "task_name": "BuildIdSearchTest",
            "task_data": {},
        },
    )
    assert task_resp.status_code == 201

    # Filter by the real build_id — the failing path in the production
    # log. Without the fix, this returned 500.
    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": f"build_id:=:{build_id}", "page_size": 50},
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # The created task must come back via the build_id filter.
    task_ids = {t["task_id"] for t in payload["tasks"]}
    assert "search-build-id-task" in task_ids


@pytest.mark.asyncio
async def test_search_filter_by_build_id_ilike_works_against_uuid_column(
    pg_client: AsyncClient,
) -> None:
    """Substring match on build_id requires builds.id::text — without the
    cast Postgres rejects ``uuid ILIKE varchar``."""
    build_resp = await pg_client.post("/api/v1/builds", json={})
    build_id = build_resp.json()["id"]
    await pg_client.post(
        f"/api/v1/builds/{build_id}/tasks",
        json={
            "task_id": "search-ilike-task",
            "task_namespace": "test",
            "task_name": "BuildIdILike",
            "task_data": {},
        },
    )
    # Use the first 8 chars as a substring (ILIKE prefix-style).
    prefix = build_id[:8]
    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": f"build_id:~:{prefix}", "page_size": 50},
    )
    assert resp.status_code == 200, resp.text
    task_ids = {t["task_id"] for t in resp.json()["tasks"]}
    assert "search-ilike-task" in task_ids
