"""Task annotations, the task search filters and the instance read, over
HTTP (``api-pg`` tier, STA-149).

- ``PATCH /tasks/{task_id}``: omitted fields unchanged, tags replaced as a
  normalised set, description cleared by ``null``/``""``; refusals are 422;
  counted against the write rate limit; outside registration and the
  listing order.
- ``GET /tasks`` filters: ``namespace``, ``name``, ``tag`` (all-of),
  ``id_prefix``, ``q``, combined with each other and with the cursor.
- ``GET /instances/{instance_id}``: the contract shape, 404 for an unknown
  id and for another environment's instance.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from stardag_api.config import limits_settings
from stardag_api.limits import _rate_limiter
from stardag_api.schemas_v2 import RegistrationItem
from tests.v2_support import Harness, item


@pytest.fixture
def h(session_factory) -> Harness:
    return Harness(session_factory)


async def _get(client: AsyncClient, path: str, **params: Any) -> Any:
    response = await client.get(f"/api/v2{path}", params=params)
    assert response.status_code == 200, response.text
    return response.json()


async def _patch(client: AsyncClient, task_id: str, body: dict[str, Any]) -> Any:
    response = await client.patch(f"/api/v2/tasks/{task_id}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(page: dict[str, Any]) -> list[str]:
    return [t["task_id"] for t in page["tasks"]]


def _namespaced(it: RegistrationItem, namespace: str) -> RegistrationItem:
    return it.model_copy(update={"task_namespace": namespace})


# -- PATCH /tasks/{task_id} --------------------------------------------------


async def test_annotations_round_trip_and_omitted_fields_are_unchanged(
    client: AsyncClient, h: Harness
):
    t = item("T")
    await h.planned([t], [t])

    fresh = await _get(client, f"/tasks/{t.task_id}")
    assert fresh["tags"] == [] and fresh["description"] is None

    both = await _patch(
        client,
        t.task_id,
        {"tags": [" best ", "baseline", "best", "v2"], "description": "  A model. "},
    )
    assert both["tags"] == ["baseline", "best", "v2"]  # trimmed, deduped, sorted
    assert both["description"] == "A model."
    assert both["task_id"] == t.task_id and "instances" not in both

    # Omitted = unchanged, field by field.
    only_description = await _patch(client, t.task_id, {"description": "Better."})
    assert only_description["tags"] == ["baseline", "best", "v2"]
    only_tags = await _patch(client, t.task_id, {"tags": ["x"]})
    assert only_tags["description"] == "Better."
    nothing = await _patch(client, t.task_id, {})
    assert (nothing["tags"], nothing["description"]) == (["x"], "Better.")

    detail = await _get(client, f"/tasks/{t.task_id}")
    assert (detail["tags"], detail["description"]) == (["x"], "Better.")
    assert len(detail["instances"]) == 1

    # Clearing: [] for tags, null or "" for the description.
    cleared = await _patch(client, t.task_id, {"tags": [], "description": None})
    assert (cleared["tags"], cleared["description"]) == ([], None)
    await _patch(client, t.task_id, {"description": "again"})
    assert (await _patch(client, t.task_id, {"description": "  "}))[
        "description"
    ] is None


@pytest.mark.parametrize(
    "body",
    [
        {"tags": None},
        {"tags": [""]},
        {"tags": ["   "]},
        {"tags": ["x" * 65]},
        {"tags": ["bad\ttag"]},
        {"tags": [f"t{i}" for i in range(33)]},
        {"tags": "single"},
        {"description": "d" * 4097},
        {"labels": ["x"]},
    ],
)
async def test_invalid_annotations_are_refused(
    client: AsyncClient, h: Harness, body: dict[str, Any]
):
    t = item("T")
    await h.planned([t], [t])
    await _patch(client, t.task_id, {"tags": ["keep"], "description": "keep"})

    response = await client.patch(f"/api/v2/tasks/{t.task_id}", json=body)
    assert response.status_code == 422, response.text
    detail = await _get(client, f"/tasks/{t.task_id}")
    assert (detail["tags"], detail["description"]) == (["keep"], "keep")


async def test_the_bounds_are_inclusive_and_duplicates_count_once(
    client: AsyncClient, h: Harness
):
    t = item("T")
    await h.planned([t], [t])
    tags = [f"t{i:02d}" for i in range(32)]
    body = {"tags": tags + tags, "description": "d" * 4096}
    annotated = await _patch(client, t.task_id, body)
    assert annotated["tags"] == tags and len(annotated["description"]) == 4096
    assert (await _patch(client, t.task_id, {"tags": ["x" * 64]}))["tags"] == ["x" * 64]


async def test_annotating_an_unknown_task_is_404(client: AsyncClient):
    for body in ({"tags": ["x"]}, {}):
        response = await client.patch("/api/v2/tasks/nope", json=body)
        assert response.status_code == 404
        assert response.json()["detail"]["code"] == "unknown_task"


async def test_annotations_survive_registration_and_keep_the_listing_order(
    client: AsyncClient, h: Harness
):
    """Registration never names the annotations, so re-registering the task
    (here under a new build and a new deployment) keeps them; annotating
    does not move ``status_at``, so the listing order is unchanged."""
    a, b = item("A"), item("B")
    await h.planned([a, b], [a, b])
    before = _ids(await _get(client, "/tasks"))
    status_at = (await _get(client, f"/tasks/{a.task_id}"))["status_at"]

    await _patch(client, a.task_id, {"tags": ["kept"], "description": "kept"})
    assert _ids(await _get(client, "/tasks")) == before
    await h.planned([a], [a], deployment_id=await h.new_deployment())

    detail = await _get(client, f"/tasks/{a.task_id}")
    assert (detail["tags"], detail["description"]) == (["kept"], "kept")
    assert detail["status_at"] == status_at
    assert len(detail["instances"]) == 2


async def test_annotations_are_environment_scoped(
    client: AsyncClient, as_environment_b, h: Harness
):
    t = item("T")
    await h.planned([t], [t])
    with as_environment_b():
        response = await client.patch(
            f"/api/v2/tasks/{t.task_id}", json={"tags": ["x"]}
        )
    assert response.status_code == 404
    assert (await _get(client, f"/tasks/{t.task_id}"))["tags"] == []


@pytest.fixture
def rate_limit(monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    monkeypatch.setattr(limits_settings, "max_requests_per_minute", 2)
    _rate_limiter.clear()
    yield 2
    _rate_limiter.clear()


async def test_annotating_counts_against_the_write_rate_limit(
    client: AsyncClient, h: Harness, rate_limit: int
):
    """An annotation is a user's new write, not one that finishes running
    work, so it is in the workspace's ordinary window (STA-149)."""
    from stardag_api.routes.registry_v2.guard import is_exempt

    assert not is_exempt("PATCH", "/api/v2/tasks/{task_id}")
    t = item("T")
    await h.planned([t], [t])  # services directly: the window is untouched
    for _ in range(rate_limit):
        await _patch(client, t.task_id, {"tags": ["x"]})
    limited = await client.patch(f"/api/v2/tasks/{t.task_id}", json={"tags": ["y"]})
    assert limited.status_code == 429
    assert limited.json()["detail"]["code"] == "rate_limited"
    # Reads stay unlimited.
    assert (await _get(client, f"/tasks/{t.task_id}"))["tags"] == ["x"]


# -- GET /tasks filters --------------------------------------------------------


async def test_task_list_filters(client: AsyncClient, h: Harness):
    train = _namespaced(item("Train", params={"lr": 1}), "ml")
    train2 = _namespaced(item("Train", params={"lr": 2}), "ml")
    evaluate = _namespaced(item("Evaluate"), "ml")
    load = item("Load_100%")
    tasks = [train, train2, evaluate, load]
    await h.planned(tasks, tasks)
    await _patch(
        client,
        train.task_id,
        {"tags": ["best", "prod"], "description": "The tuned Baseline run"},
    )
    await _patch(client, train2.task_id, {"tags": ["best"]})
    await _patch(client, load.task_id, {"description": "raw dump of ml data"})

    def ids(*its: RegistrationItem) -> set[str]:
        return {i.task_id for i in its}

    async def listed(**params: Any) -> set[str]:
        return set(_ids(await _get(client, "/tasks", **params)))

    assert await listed(namespace="ml") == ids(train, train2, evaluate)
    assert await listed(namespace="") == ids(load)
    assert await listed(name="Train") == ids(train, train2)
    assert await listed(name="train") == set()  # exact, case-sensitive
    assert await listed(namespace="ml", name="Evaluate") == ids(evaluate)

    # Tags: all-of, normalised like the PATCH.
    assert await listed(tag="best") == ids(train, train2)
    assert await listed(tag=["best", "prod"]) == ids(train)
    assert await listed(tag=[" prod ", "prod"]) == ids(train)
    assert await listed(tag=["best", "missing"]) == set()

    # Task-id prefix; LIKE wildcards match themselves.
    assert await listed(id_prefix=train.task_id[:12]) == ids(train)
    assert await listed(id_prefix="%") == set()

    # Free text: case-insensitive, over name, namespace and description.
    assert await listed(q="baseline") == ids(train)
    assert await listed(q="EVAL") == ids(evaluate)
    assert await listed(q="ml") == ids(train, train2, evaluate, load)
    assert await listed(q="100%") == ids(load)
    assert await listed(q="o_d") == set()  # "_" is not a wildcard ("Load")
    assert await listed(q="ml", tag="best", status="pending") == ids(train, train2)
    assert await listed(q="ml", status="completed") == set()

    item_ = (await _get(client, "/tasks", tag="prod"))["tasks"][0]
    assert item_["tags"] == ["best", "prod"]
    assert item_["description"] == "The tuned Baseline run"
    assert "instances" not in item_


@pytest.mark.parametrize(
    "params",
    [
        {"tag": ""},
        {"tag": "x" * 65},
        {"q": ""},
        {"id_prefix": ""},
        {"name": "n" * 256},
    ],
)
async def test_invalid_filters_are_refused(client: AsyncClient, params: dict):
    response = await client.get("/api/v2/tasks", params=params)
    assert response.status_code in (400, 422), response.text


async def test_filtered_listing_pages_without_gaps_or_repeats(
    client: AsyncClient, h: Harness
):
    tagged = [item(f"T{i}") for i in range(5)]
    untagged = [item(f"U{i}") for i in range(3)]
    await h.planned(tagged + untagged, tagged + untagged)
    for t in tagged:
        await _patch(client, t.task_id, {"tags": ["batch"]})

    seen: list[str] = []
    cursor = None
    for expected in (2, 2, 1):
        params: dict[str, Any] = {"tag": "batch", "limit": 2}
        if cursor:
            params["cursor"] = cursor
        page = await _get(client, "/tasks", **params)
        assert len(page["tasks"]) == expected
        seen += _ids(page)
        cursor = page["next_cursor"]
    assert cursor is None
    assert sorted(seen) == sorted(t.task_id for t in tagged)


# -- GET /instances/{instance_id} ---------------------------------------------


async def test_an_instance_by_id(client: AsyncClient, h: Harness):
    t = item("T", extra={"threads": 4})
    _, plan = await h.planned([t], [t])
    (listed,) = (await _get(client, f"/tasks/{t.task_id}"))["instances"]

    instance = await _get(client, f"/instances/{listed['id']}")
    assert set(instance) == {
        "id",
        "task_id",
        "deployment_id",
        "settings_hash",
        "instance_hash",
        "body",
        "created_at",
        "expanded_at",
    }
    assert instance["id"] == listed["id"]
    assert instance["task_id"] == t.task_id
    assert instance["deployment_id"] == str(plan.deployment_id)
    assert instance["settings_hash"] == listed["settings_hash"]
    assert instance["instance_hash"] == t.instance_hash
    assert instance["body"] == t.body
    assert instance["created_at"] == listed["created_at"]
    assert instance["expanded_at"] == listed["expanded_at"]


async def test_an_unknown_or_foreign_instance_is_404(
    client: AsyncClient, as_environment_b, h: Harness
):
    t = item("T")
    await h.planned([t], [t])
    (listed,) = (await _get(client, f"/tasks/{t.task_id}"))["instances"]

    unknown = await client.get(f"/api/v2/instances/{uuid4()}")
    assert unknown.status_code == 404
    assert unknown.json()["detail"]["code"] == "unknown_instance"
    assert (await client.get("/api/v2/instances/not-a-uuid")).status_code == 422
    with as_environment_b():
        foreign = await client.get(f"/api/v2/instances/{listed['id']}")
    assert foreign.status_code == 404
    assert foreign.json()["detail"]["code"] == "unknown_instance"
