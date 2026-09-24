"""The inspecting reads of ``APIRegistry`` (``_api_reads.py``): each is sent
to its ``/api/v2`` route with the server's query parameters, and the
server's body (``schemas_v2_reads.py``) parses into the SDK's model."""

from __future__ import annotations

from uuid import uuid4

import httpx
import pytest

from stardag.exceptions import NotFoundError
from tests.test_registry.test_api_registry import _Recorder, _registry

DEPLOYMENT = {
    "id": str(uuid4()),
    "kind": "modal",
    "app_name": "app",
    "code_id": "c",
    "image_id": None,
    "modal_app_id": "ap-1",
    "generation": 3,
    "deployed_at": "2026-09-24T00:00:00Z",
    "activated_at": "2026-09-24T00:01:00Z",
    "is_current": True,
    "created": False,
}


def _plan_detail(plan_id: str, build_id: str, *, active: bool = True) -> dict:
    return {
        "id": plan_id,
        "build_id": build_id,
        "deployment_id": DEPLOYMENT["id"],
        "deployment": DEPLOYMENT,
        "settings_hash": "abc",
        "generation": 2,
        "created_at": "2026-09-24T00:00:00Z",
        "activated_at": "2026-09-24T00:00:00Z",
        "sealed_at": None,
        "superseded_at": None if active else "2026-09-24T01:00:00Z",
        "is_active": active,
        "member_count": 5,
        "root_count": 1,
        "excluded_count": 1,
        "member_counts": {"completed": 3, "failed": 1},
    }


def test_plan_get_parses_counts_scope_and_deployment():
    plan_id, build_id = str(uuid4()), str(uuid4())
    recorder = _Recorder(
        {("GET", f"/api/v2/plans/{plan_id}"): _plan_detail(plan_id, build_id)}
    )
    plan = _registry(recorder).plan_get(plan_id)  # type: ignore[arg-type]
    assert plan.member_counts == {"completed": 3, "failed": 1}
    assert (plan.member_count, plan.root_count, plan.excluded_count) == (5, 1, 1)
    assert plan.is_active and plan.active
    assert plan.deployment is not None and plan.deployment.generation == 3


def test_build_list_plans():
    build_id = str(uuid4())
    plans = [
        _plan_detail(str(uuid4()), build_id),
        _plan_detail(str(uuid4()), build_id, active=False),
    ]
    recorder = _Recorder(
        {
            ("GET", f"/api/v2/builds/{build_id}/plans"): {
                "build_id": build_id,
                "plans": plans,
            }
        }
    )
    result = _registry(recorder).build_list_plans(build_id)  # type: ignore[arg-type]
    assert [p.is_active for p in result] == [True, False]
    assert result[1].superseded_at is not None


def test_build_list_page_sends_the_cursor_and_parses_total_and_next():
    build = {
        "id": str(uuid4()),
        "name": "b",
        "status": "failed",
        "root_task_ids": ["t"],
        "error_message": "a root was excluded",
    }
    recorder = _Recorder(
        {
            ("GET", "/api/v2/builds"): {
                "builds": [build],
                "total": 7,
                "next_cursor": "c2",
            }
        }
    )
    page = _registry(recorder).build_list_page(
        status="failed", reactive_app_name="app", limit=1, cursor="c1"
    )
    params = dict(recorder.requests[-1].url.params)
    assert params == {
        "limit": "1",
        "cursor": "c1",
        "status": "failed",
        "reactive_app_name": "app",
    }
    assert (page.total, page.next_cursor) == (7, "c2")
    assert page.builds[0].error_message == "a root was excluded"


def test_build_list_is_the_first_page():
    recorder = _Recorder(
        {("GET", "/api/v2/builds"): {"builds": [], "total": 0, "next_cursor": None}}
    )
    assert _registry(recorder).build_list(limit=5) == []
    assert "cursor" not in recorder.requests[-1].url.params


def test_task_list_and_the_claim_holder():
    plan_id, build_id = str(uuid4()), str(uuid4())
    task = {
        "task_id": "t1",
        "task_namespace": "ns",
        "task_name": "T",
        "status": "running",
        "claim_plan_id": plan_id,
        "claim_build_id": build_id,
    }
    recorder = _Recorder(
        {("GET", "/api/v2/tasks"): {"tasks": [task], "next_cursor": "n"}}
    )
    page = _registry(recorder).task_list(status="running", limit=10, cursor="c")
    assert dict(recorder.requests[-1].url.params) == {
        "limit": "10",
        "cursor": "c",
        "status": "running",
    }
    assert page.next_cursor == "n"
    assert str(page.tasks[0].claim_build_id) == build_id
    assert str(page.tasks[0].claim_plan_id) == plan_id


@pytest.mark.parametrize("include_ended", [True, False])
def test_task_list_executions(include_ended: bool):
    execution = {
        "id": str(uuid4()),
        "task_id": "t1",
        "build_id": str(uuid4()),
        "plan_id": str(uuid4()),
        "instance_id": str(uuid4()),
        "started_at": "2026-09-24T00:00:00Z",
        "outcome": "interrupted",
        "in_current_plan": True,
    }
    recorder = _Recorder(
        {
            ("GET", "/api/v2/tasks/t1/executions"): {
                "task_id": "t1",
                "executions": [execution],
            }
        }
    )
    rows = _registry(recorder).task_list_executions("t1", include_ended=include_ended)
    assert recorder.requests[-1].url.params["include_ended"] == (
        "true" if include_ended else "false"
    )
    assert rows[0].outcome == "interrupted"
    assert str(rows[0].build_id) == execution["build_id"]


def test_task_events():
    event = {
        "id": str(uuid4()),
        "event_type": "TASK_STRUCTURE_DIVERGED",
        "created_at": "2026-09-24T00:00:00Z",
        "build_id": None,
        "plan_id": str(uuid4()),
        "execution_id": None,
        "task_id": "t1",
        "report_applied": True,
        "error_message": None,
        "event_metadata": {"added": ["h2"]},
    }
    recorder = _Recorder({("GET", "/api/v2/tasks/t1/events"): {"events": [event]}})
    (row,) = _registry(recorder).task_events("t1", limit=50)
    assert recorder.requests[-1].url.params["limit"] == "50"
    assert row.event_type == "TASK_STRUCTURE_DIVERGED"
    assert row.event_metadata == {"added": ["h2"]}


def test_deployment_get_and_a_missing_one():
    recorder = _Recorder(
        {
            ("GET", f"/api/v2/deployments/{DEPLOYMENT['id']}"): DEPLOYMENT,
            ("GET", "/api/v2/deployments/missing"): httpx.Response(
                404, json={"detail": {"code": "unknown_deployment", "message": "no"}}
            ),
        }
    )
    registry = _registry(recorder)
    assert registry.deployment_get(DEPLOYMENT["id"]).is_current  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        registry.deployment_get("missing")  # type: ignore[arg-type]
