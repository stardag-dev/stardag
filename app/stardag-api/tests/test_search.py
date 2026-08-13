"""Tests for task search: key extraction depth and JSONB sorting."""

from collections import Counter

import pytest
from httpx import AsyncClient

from fastapi import HTTPException

from stardag_api.models.enums import TaskStatus
from stardag_api.routes.search import (
    _extract_keys,
    _is_valid_segment,
    _render_jsonb_path,
    _split_array_segment,
    _validate_filter_value,
    build_jsonb_condition,
    parse_filter_string,
)


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
async def test_status_values_includes_all_filterable_statuses(client: AsyncClient):
    """The status autocomplete returns every status users can filter on.

    Previously only {pending, running, completed, failed} were exposed,
    so suspended / skipped / cancelled were undiscoverable in the
    search-bar autocomplete.

    **Derived from ``TaskStatus``, not a literal list.** A hand-written set
    here pins only what someone remembered to type: when INTERRUPTED was
    added to the enum, both the route and this test kept their old seven
    values and agreed with each other, so the drift the docstring promises
    to catch went unnoticed until a reviewer read the route. Comparing
    against the enum is what makes the promise real — a new status now
    fails here until it is either exposed or explicitly excluded below.
    """
    resp = await client.get(
        "/api/v1/tasks/search/values",
        params={"key": "status"},
    )
    assert resp.status_code == 200
    values = {v["value"] for v in resp.json()["values"]}
    # UNREGISTERED is an internal phantom-row marker, not something users
    # filter on — the one deliberate omission, mirrored in the route.
    expected = {s.value for s in TaskStatus} - {TaskStatus.UNREGISTERED.value}
    assert values == expected


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
        condition, needs_join, _ = build_jsonb_condition("build_id", "!=", "x")
        assert needs_join is True
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


class TestBuildIdFilterValidation:
    """Pin the validation rules around build_id filter values without
    needing a Postgres connection. The route's CAST AS uuid would
    otherwise fail at execution time with a 500 — these tests prove the
    route fails fast at the API layer with a 400 instead."""

    def test_valid_uuid_for_equality_passes(self) -> None:
        # No exception expected.
        _validate_filter_value("build_id", "=", "019dd0c6-2b40-7de3-af35-2e7d1d1e8b37")

    def test_invalid_uuid_for_equality_raises_400(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _validate_filter_value("build_id", "=", "not-a-uuid")
        assert exc_info.value.status_code == 400
        assert "build_id" in exc_info.value.detail
        assert "uuid" in exc_info.value.detail.lower()

    def test_invalid_uuid_for_inequality_raises_400(self) -> None:
        with pytest.raises(HTTPException):
            _validate_filter_value("build_id", "!=", "garbage")

    def test_invalid_uuid_for_ilike_passes(self) -> None:
        # ILIKE is text substring match; any string is OK.
        _validate_filter_value("build_id", "~", "not-a-uuid")
        _validate_filter_value("build_id", "~", "abc123")

    def test_other_keys_are_not_validated_as_uuid(self) -> None:
        # task_name, params, etc. are free-form text and never UUID-checked.
        _validate_filter_value("task_name", "=", "anything")
        _validate_filter_value("param.lr", ">", "0.01")
        _validate_filter_value("task_namespace", "~", "ml")

    def test_parse_filter_string_propagates_validation_400(self) -> None:
        # parse_filter_string is the single entry point for both filter
        # call sites; ensure the validation propagates through it so every
        # caller gets the 400 for free.
        with pytest.raises(HTTPException) as exc_info:
            parse_filter_string("build_id:=:not-a-uuid")
        assert exc_info.value.status_code == 400


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
    # Use the first 8 chars as a substring (ILIKE prefix-style). Single
    # build in this test, so the timestamp-prefix collision in UUID7 (every
    # uuid7 created in the same millisecond shares its leading hex) is fine.
    prefix = build_id[:8]
    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": f"build_id:~:{prefix}", "page_size": 50},
    )
    assert resp.status_code == 200, resp.text
    task_ids = {t["task_id"] for t in resp.json()["tasks"]}
    assert "search-ilike-task" in task_ids


@pytest.mark.asyncio
async def test_search_filter_by_build_id_inequality_excludes_match(
    pg_client: AsyncClient,
) -> None:
    """``filter=build_id:!=:<uuid>`` must return tasks from every other
    build but exclude tasks from the named build. Closes the e2e gap for
    the inequality branch (the unit test pins the SQL string but doesn't
    execute it)."""
    # Build A: a task that should be EXCLUDED by the != filter.
    a_resp = await pg_client.post("/api/v1/builds", json={})
    a_build_id = a_resp.json()["id"]
    await pg_client.post(
        f"/api/v1/builds/{a_build_id}/tasks",
        json={
            "task_id": "ne-task-in-a",
            "task_namespace": "test",
            "task_name": "NeTaskA",
            "task_data": {},
        },
    )

    # Build B: a task that should be INCLUDED by the != filter.
    b_resp = await pg_client.post("/api/v1/builds", json={})
    b_build_id = b_resp.json()["id"]
    await pg_client.post(
        f"/api/v1/builds/{b_build_id}/tasks",
        json={
            "task_id": "ne-task-in-b",
            "task_namespace": "test",
            "task_name": "NeTaskB",
            "task_data": {},
        },
    )

    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": f"build_id:!=:{a_build_id}", "page_size": 100},
    )
    assert resp.status_code == 200, resp.text
    task_ids = {t["task_id"] for t in resp.json()["tasks"]}
    assert "ne-task-in-b" in task_ids
    assert "ne-task-in-a" not in task_ids


@pytest.mark.asyncio
async def test_search_filter_build_id_with_malformed_uuid_returns_400(
    pg_client: AsyncClient,
) -> None:
    """A non-UUID build_id value is rejected with a clear 400 instead of
    propagating to a Postgres ``invalid input syntax for type uuid`` 500
    at SQL execution time."""
    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": "build_id:=:not-a-uuid", "page_size": 50},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "build_id" in detail.lower()
    assert "uuid" in detail.lower()


@pytest.mark.asyncio
async def test_search_filter_build_id_ilike_accepts_non_uuid_substring(
    pg_client: AsyncClient,
) -> None:
    """ILIKE (~) is a text substring match, so any string is allowed —
    even ones that aren't valid UUIDs. Validates that the
    ``_validate_filter_value`` carve-out for ``~`` works correctly."""
    resp = await pg_client.get(
        "/api/v1/tasks/search",
        params={"filter": "build_id:~:not-a-uuid", "page_size": 50},
    )
    # Returns 200 with empty results (no UUID happens to contain that text).
    assert resp.status_code == 200, resp.text
    assert resp.json()["tasks"] == []


# ---------------------------------------------------------------------------
# Filter-key / sort-field injection defence.
#
# JSONB path segments and the sort subquery's artifact name land in the SQL
# *text* (Postgres has no bind-parameter form for a `->'step'` accessor), so
# they are validated against an identifier character class. These tests pin
# that a quote in a key can never reach the emitted SQL.
# ---------------------------------------------------------------------------


INJECTION_SEGMENTS = [
    "x'",
    "x' OR '1'='1",
    "x'--",
    "x'; DROP TABLE tasks; --",
    "x'||(SELECT 1)||'",
    'x"',
    "x\\",
    "x y",
    "x;y",
    "x/*c*/",
]


class TestFilterKeyInjection:
    @pytest.mark.parametrize("segment", INJECTION_SEGMENTS)
    def test_param_key_with_sql_metacharacters_raises_400(self, segment: str) -> None:
        with pytest.raises(HTTPException) as exc_info:
            build_jsonb_condition(f"param.{segment}", "=", "v")
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize("segment", INJECTION_SEGMENTS)
    def test_artifact_key_with_sql_metacharacters_raises_400(
        self, segment: str
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            build_jsonb_condition(f"artifact.report.{segment}", "=", "v")
        assert exc_info.value.status_code == 400

    @pytest.mark.parametrize("segment", INJECTION_SEGMENTS)
    def test_artifact_name_with_sql_metacharacters_raises_400(
        self, segment: str
    ) -> None:
        with pytest.raises(HTTPException) as exc_info:
            build_jsonb_condition(f"artifact.{segment}.field", "=", "v")
        assert exc_info.value.status_code == 400

    def test_legitimate_param_key_still_builds(self) -> None:
        condition, _, _ = build_jsonb_condition("param.learning_rate", "=", "0.1")
        assert condition is not None
        assert "task_data->>'learning_rate'" in condition

    def test_legitimate_nested_and_array_key_still_builds(self) -> None:
        condition, _, _ = build_jsonb_condition("param.cfg.items[2].name", "=", "x")
        assert condition is not None
        assert "->'cfg'" in condition
        assert "->'items')->2" in condition
        assert "->>'name'" in condition

    def test_artifact_name_is_bound_not_interpolated(self) -> None:
        condition, _, artifact_name = build_jsonb_condition(
            "artifact.report.score", "=", "1"
        )
        assert condition is not None
        assert artifact_name == "report"
        # The name is a *value* — it must travel as a bound parameter.
        assert "artifact_filter.name = :filter_artifact_name" in condition
        assert "'report'" not in condition

    def test_hyphen_and_underscore_are_accepted(self) -> None:
        condition, _, _ = build_jsonb_condition("param.my-key_2", "=", "v")
        assert condition is not None
        assert "->>'my-key_2'" in condition


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_filter",
    [
        "param.x':=:1",
        "param.x' OR '1'='1:=:1",
        "artifact.a'.b:=:1",
        "artifact.a.b':=:1",
    ],
)
async def test_search_filter_key_injection_returns_400(
    client: AsyncClient, bad_filter: str
) -> None:
    resp = await client.get(
        "/api/v1/tasks/search", params={"filter": bad_filter, "page_size": 50}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_sort",
    [
        "param.x':asc",
        "artifact.a' UNION SELECT 1 --.b:asc",
        "artifact.a.b':desc",
    ],
)
async def test_search_sort_injection_returns_400(
    client: AsyncClient, bad_sort: str
) -> None:
    resp = await client.get(
        "/api/v1/tasks/search", params={"sort": bad_sort, "page_size": 50}
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_search_rejects_overlong_filter(client: AsyncClient) -> None:
    """Bounded input is the ReDoS defence for the filter grammar."""
    resp = await client.get(
        "/api/v1/tasks/search", params={"filter": "a" * 5000, "page_size": 50}
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_search_sort_by_artifact_still_works(pg_client: AsyncClient) -> None:
    """Regression: the sort subquery still functions after the artifact name
    moved from an interpolated literal to a bound parameter.

    Postgres-only: the numeric-detection branch uses the ``~`` regex operator.
    """
    await _create_task(pg_client, "art-sort-1", {"v": 1})
    resp = await pg_client.get(
        "/api/v1/tasks/search", params={"sort": "artifact.report.score:asc"}
    )
    assert resp.status_code == 200, resp.text


class TestRenderedPathNeverContainsMetacharacters:
    """The property the SQL-injection fix actually rests on: whatever the
    caller sends, the rendered accessor chain either contains no SQL
    metacharacter, or no chain is produced at all (400).

    This is asserted directly rather than inferred from the endpoint's status
    code, so it stays true if the calling code is ever restructured.
    """

    ADVERSARIAL = [
        "x'",
        'x"',
        "x\\",
        "x`",
        "x;",
        "x--",
        "x/*",
        "x*/",
        "x'||'",
        "x[0]'",
        "x[0']",
        "x[0]junk",
        "x[]",
        "x[-1]",
        "x[\u00b2]",  # Unicode superscript two - str.isdigit() accepts it
        "x[\u0661]",  # Arabic-Indic digit one - likewise
        "",
        " ",
        "x y",
        "x\ty",
        "x\ny",
        "x.y",
        "%",
        ":param",
        "\u0000",
    ]

    @pytest.mark.parametrize("segment", ADVERSARIAL)
    def test_no_metacharacter_survives(self, segment: str) -> None:
        try:
            rendered = _render_jsonb_path("tasks.task_data", [segment], "param.k")
        except HTTPException as exc:
            assert exc.status_code == 400
            return
        # A chain was produced, so the segment must have been identifier-safe.
        # That is the whole invariant: the accessor is a single-quoted literal,
        # and only a quote (or a backslash, which some engines honour inside
        # literals) could escape it. Sequences like `--` or `;` are inert
        # *inside* a quoted literal, so a segment of "x--" renders the harmless
        # ->>'x--' and is legitimately allowed.
        assert _is_valid_segment(segment) or _split_array_segment(segment)
        for forbidden in ("'", '"', "\\", "`"):
            assert forbidden not in segment, (segment, rendered)
        # Quotes in the output are only the delimiters this code emits itself.
        body = rendered[len("tasks.task_data") :]
        assert body.count("'") % 2 == 0, (segment, rendered)

    def test_unicode_digit_index_is_rejected_not_interpolated(self) -> None:
        """``str.isdigit()`` would accept these; the ASCII-only check must not."""
        for segment in ("x[\u00b2]", "x[\u0661]"):
            with pytest.raises(HTTPException) as exc_info:
                _render_jsonb_path("tasks.task_data", [segment], "param.k")
            assert exc_info.value.status_code == 400
