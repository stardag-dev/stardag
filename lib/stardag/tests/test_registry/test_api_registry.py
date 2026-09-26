"""The v2 HTTP client (``APIRegistry``): every route is sent to ``/api/v2``
with the server's body shape, answers parse into the SDK's models, and the
server's refusals surface with their ``detail.code``.

The client is driven through ``httpx.MockTransport``; the server contract
itself (``app/stardag-api/src/stardag_api/schemas_v2.py``) is what the
bodies here mirror.
"""

from __future__ import annotations

import gzip
import json
import typing
from datetime import datetime, timezone
from uuid import UUID, uuid4

import httpx
import pytest

from stardag.exceptions import APIError, NotFoundError, execution_not_wanted
from stardag.registry import APIRegistry, RegistrationItem
from stardag.registry import _api_http
from stardag.registry._api_http import (
    _GZIP_REQUEST_THRESHOLD_BYTES,
    gzip_json_body,
    transport_retry_counts,
)

Handler = typing.Callable[[httpx.Request], httpx.Response]

NOW = datetime(2026, 9, 24, tzinfo=timezone.utc)


class _Recorder:
    def __init__(self, responses: dict[tuple[str, str], typing.Any] | None = None):
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        body = self.responses.get(key, {})
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)

    def body(self, index: int = -1) -> typing.Any:
        request = self.requests[index]
        content = request.content
        if request.headers.get("Content-Encoding") == "gzip":
            content = gzip.decompress(content)
        return json.loads(content) if content else None


def _registry(handler: Handler) -> APIRegistry:
    registry = APIRegistry(api_url="https://registry.test", api_key="sk-test")
    registry._client = httpx.Client(transport=httpx.MockTransport(handler))
    return registry


def _item(
    task_id: str = "t1", *, upstreams: list[str] | None = None
) -> RegistrationItem:
    return RegistrationItem(
        task_id=task_id,
        task_namespace="ns",
        task_name="T",
        version="1",
        output_uri="memory://x",
        instance_hash=f"h-{task_id}",
        body={"__namespace": "ns", "__name": "T"},
        declared_upstreams=upstreams,
        observed_complete=False,
        observed_at=NOW,
    )


BUILD = {"id": str(uuid4()), "name": "b", "status": "running", "root_task_ids": ["t1"]}
PLAN = {
    "id": str(uuid4()),
    "build_id": BUILD["id"],
    "deployment_id": str(uuid4()),
    "settings_hash": "abc",
    "generation": 1,
    "created": True,
}
TRANSITION = {"applied": True, "status": "running", "execution_id": str(uuid4())}


class TestRoutes:
    def test_build_create_sends_the_request_and_a_client_minted_id(self):
        recorder = _Recorder({("POST", "/api/v2/builds"): BUILD})
        registry = _registry(recorder)
        build_id = uuid4()
        info = registry.build_create(
            root_task_ids=["t2", "t1", "t1"],
            build_id=build_id,
            description="d",
            executor_metadata={"kind": "modal"},
        )
        assert info.id == UUID(BUILD["id"])
        assert recorder.body() == {
            "id": str(build_id),
            "description": "d",
            "root_task_ids": ["t1", "t2"],
            "executor_metadata": {"kind": "modal"},
        }

    def test_plan_create_carries_the_scope_and_the_roots(self):
        recorder = _Recorder({("POST", f"/api/v2/builds/{BUILD['id']}/plans"): PLAN})
        registry = _registry(recorder)
        plan_id, deployment_id = uuid4(), uuid4()
        plan = registry.plan_create(
            UUID(BUILD["id"]),
            plan_id=plan_id,
            deployment_id=deployment_id,
            settings={"A": "1"},
            roots=[_item()],
        )
        assert plan.created and plan.generation == 1
        body = recorder.body()
        assert body["plan_id"] == str(plan_id)
        assert body["deployment_id"] == str(deployment_id)
        assert body["settings"] == {"A": "1"}
        (root,) = body["roots"]
        assert root["declared_upstreams"] is None
        assert root["observed_at"] == "2026-09-24T00:00:00Z"
        # The item carries exactly the server's fields (it forbids others).
        assert set(root) == {
            "task_id",
            "task_namespace",
            "task_name",
            "version",
            "output_uri",
            "instance_hash",
            "body",
            "declared_upstreams",
            "observed_complete",
            "observed_at",
        }

    def test_members_seal_and_frontier(self):
        plan_id, build_id = uuid4(), uuid4()
        recorder = _Recorder(
            {
                ("POST", f"/api/v2/plans/{plan_id}/members"): {"tasks_created": 2},
                ("POST", f"/api/v2/plans/{plan_id}/seal"): PLAN,
                ("GET", f"/api/v2/builds/{build_id}/frontier"): {
                    "build_id": str(build_id),
                    "plan_id": str(plan_id),
                    "sealed": True,
                    "runnable": [
                        {
                            "task_id": "t1",
                            "instance_hash": "h",
                            "status": "interrupted",
                            "attempts": 3,
                            "interruptions": 2,
                        }
                    ],
                    "future_field": "ignored",
                },
            }
        )
        registry = _registry(recorder)
        result = registry.plan_register_members(
            plan_id, [_item(), _item("t2", upstreams=["h-t1"])]
        )
        assert result.tasks_created == 2
        assert [i["task_id"] for i in recorder.body()["items"]] == ["t1", "t2"]
        registry.plan_seal(plan_id)
        frontier = registry.build_get_frontier(build_id)
        assert frontier.sealed and frontier.runnable[0].task_id == "t1"
        # The ledger counts the tick's interruption budget reads (D9).
        assert (frontier.runnable[0].attempts, frontier.runnable[0].interruptions) == (
            3,
            2,
        )

    def test_a_claiming_start_and_a_non_claiming_one(self):
        plan_id = uuid4()
        path = f"/api/v2/plans/{plan_id}/members/t1/start"
        recorder = _Recorder({("POST", path): TRANSITION})
        registry = _registry(recorder)
        execution_id = uuid4()
        registry.member_start(
            plan_id,
            "t1",
            execution_id=execution_id,
            claim_ttl_seconds=900,
            executor_metadata={"kind": "modal"},
            limit_keys=["b", "a", "a"],
        )
        assert recorder.body() == {
            "execution_id": str(execution_id),
            "claim": True,
            "claim_ttl_seconds": 900,
            "executor_metadata": {"kind": "modal"},
            "limit_keys": ["a", "b"],
        }
        registry.member_start(
            plan_id,
            "t1",
            execution_id=execution_id,
            claim=False,
            executor="modal",
            executor_ref="fc-1",
        )
        assert recorder.body() == {
            "execution_id": str(execution_id),
            "claim": False,
            "executor": "modal",
            "executor_ref": "fc-1",
        }

    def test_reports_name_their_execution(self):
        plan_id, execution_id = uuid4(), uuid4()
        recorder = _Recorder(
            {
                ("POST", f"/api/v2/plans/{plan_id}/members/t1/complete"): TRANSITION,
                ("POST", f"/api/v2/plans/{plan_id}/members/t1/fail"): TRANSITION,
            }
        )
        registry = _registry(recorder)
        registry.member_complete(plan_id, "t1", execution_id=execution_id)
        assert recorder.body() == {"execution_id": str(execution_id)}
        registry.member_fail(
            plan_id, "t1", execution_id=execution_id, error_message="x"
        )
        assert recorder.body() == {
            "execution_id": str(execution_id),
            "error_message": "x",
        }

    def test_a_yield_carries_its_batch_and_its_deployment(self):
        plan_id, execution_id, deployment_id, batch_id = (
            uuid4(),
            uuid4(),
            uuid4(),
            uuid4(),
        )
        recorder = _Recorder(
            {
                ("POST", f"/api/v2/plans/{plan_id}/members/p/yield"): {
                    "members": {"members_admitted": 1},
                    "dynamic_edges_created": 1,
                    "status": "suspended",
                }
            }
        )
        result = _registry(recorder).member_yield(
            plan_id,
            "p",
            execution_id=execution_id,
            deployment_id=deployment_id,
            batch_id=batch_id,
            items=[_item()],
            yielded=["h-t1"],
            suspend=True,
        )
        assert result.status == "suspended" and result.dynamic_edges_created == 1
        body = recorder.body()
        assert (body["batch_id"], body["deployment_id"], body["suspend"]) == (
            str(batch_id),
            str(deployment_id),
            True,
        )
        assert body["yielded"] == ["h-t1"]

    def test_claim_renewal(self):
        execution_id = uuid4()
        recorder = _Recorder({("POST", "/api/v2/tasks/t1/claim/renew"): TRANSITION})
        _registry(recorder).claim_renew(
            "t1", execution_id=execution_id, claim_ttl_seconds=120
        )
        assert recorder.body() == {
            "execution_id": str(execution_id),
            "claim_ttl_seconds": 120,
        }

    def test_a_task_read_carries_its_claim_state(self):
        """``GET /tasks/{id}``: the server's ``TaskResponse`` — status,
        timestamps and the execution the claim names — parses whole."""
        execution_id = uuid4()
        recorder = _Recorder(
            {
                ("GET", "/api/v2/tasks/t1"): {
                    "task_id": "t1",
                    "task_namespace": "ns",
                    "task_name": "T",
                    "version": "1",
                    "output_uri": "memory://x",
                    "status": "running",
                    "status_at": NOW.isoformat(),
                    "started_at": NOW.isoformat(),
                    "completed_at": None,
                    "error_message": None,
                    "claim_expires_at": NOW.isoformat(),
                    "execution_id": str(execution_id),
                    "instances": [],
                }
            }
        )
        task = _registry(recorder).task_get("t1")
        assert (task.status, task.execution_id) == ("running", execution_id)
        assert task.started_at == NOW and task.claim_expires_at == NOW

    def test_concurrency_limits(self):
        recorder = _Recorder(
            {
                ("DELETE", "/api/v2/concurrency-limits/gpu"): httpx.Response(204),
                ("GET", "/api/v2/concurrency-limits"): {
                    "limits": [{"key": "gpu", "max_concurrent": 2}]
                },
            }
        )
        registry = _registry(recorder)
        registry.concurrency_limit_set("gpu", 2)
        assert recorder.requests[-1].method == "PUT"
        assert recorder.body() == {"max_concurrent": 2}
        assert registry.concurrency_limit_list() == {"gpu": 2}
        registry.concurrency_limit_delete("gpu")
        assert recorder.requests[-1].url.path == "/api/v2/concurrency-limits/gpu"

    def test_concurrency_limits_detailed(self):
        """``concurrency_limit_list_detailed`` sends ``include_holders``
        only when asked, and parses ``in_use``/``holders`` into the client
        models — the seam ``stardag concurrency-limits list``/``holders``
        run on, which the CLI's own tests exercise against the in-memory
        registry rather than this HTTP layer."""
        build_id, plan_id, execution_id = uuid4(), uuid4(), uuid4()
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            with_holders = request.url.params.get("include_holders") == "true"
            holders = (
                [
                    {
                        "task_id": "t1",
                        "task_name": "T",
                        "build_id": str(build_id),
                        "plan_id": str(plan_id),
                        "execution_id": str(execution_id),
                        "started_at": NOW.isoformat(),
                    }
                ]
                if with_holders
                else None
            )
            return httpx.Response(
                200,
                json={
                    "limits": [
                        {
                            "key": "gpu",
                            "max_concurrent": 2,
                            "in_use": 1,
                            "holders": holders,
                        }
                    ]
                },
            )

        registry = _registry(handler)

        bare = registry.concurrency_limit_list_detailed()
        assert "include_holders" not in requests[-1].url.params
        (limit,) = bare
        assert (limit.key, limit.max_concurrent, limit.in_use) == ("gpu", 2, 1)
        assert limit.holders is None

        detailed = registry.concurrency_limit_list_detailed(include_holders=True)
        assert requests[-1].url.params.get("include_holders") == "true"
        (limit,) = detailed
        assert limit.holders is not None
        (holder,) = limit.holders
        assert holder.task_id == "t1"
        assert holder.task_name == "T"
        assert holder.build_id == build_id
        assert holder.plan_id == plan_id
        assert holder.execution_id == execution_id
        assert holder.started_at == NOW

    def test_deployments(self):
        deployment_id = uuid4()
        row = {
            "id": str(deployment_id),
            "kind": "modal",
            "app_name": "app",
            "code_id": "sha",
            "generation": 3,
            "is_current": True,
        }
        recorder = _Recorder(
            {
                ("POST", "/api/v2/deployments"): row,
                ("POST", f"/api/v2/deployments/{deployment_id}/activate"): row,
                ("GET", "/api/v2/deployments"): {"deployments": [row]},
            }
        )
        registry = _registry(recorder)
        registry.deployment_create(
            kind="modal", code_id="sha", deployment_id=deployment_id, app_name="app"
        )
        assert recorder.body() == {
            "id": str(deployment_id),
            "kind": "modal",
            "app_name": "app",
            "code_id": "sha",
        }
        registry.deployment_activate(deployment_id, modal_app_id="ap-1")
        assert recorder.body() == {"modal_app_id": "ap-1"}
        registry.deployment_activate(deployment_id)
        assert recorder.body() in (None, {})
        (listed,) = registry.deployment_list(kind="modal", app_name="app", current=True)
        assert listed.generation == 3 and listed.is_current
        assert recorder.requests[-1].url.params["current"] == "true"

    def test_resume_names_the_scope(self):
        build_id, deployment_id = uuid4(), uuid4()
        recorder = _Recorder(
            {
                ("POST", f"/api/v2/builds/{build_id}/resume"): {
                    "build": BUILD,
                    "plan": None,
                }
            }
        )
        result = _registry(recorder).build_resume(
            build_id, deployment_id=deployment_id, settings={"A": "1"}
        )
        assert result.plan is None
        assert recorder.body() == {
            "deployment_id": str(deployment_id),
            "settings": {"A": "1"},
        }

    def test_the_scheduler_lease_and_notify(self):
        build_id = uuid4()
        lease = f"/api/v2/builds/{build_id}/scheduler-lease"
        notify = f"/api/v2/builds/{build_id}/notify"
        recorder = _Recorder(
            {
                ("POST", lease): {"build_id": str(build_id), "held": True},
                ("DELETE", lease): {"build_id": str(build_id), "held": False},
                ("POST", notify): {
                    "build_id": str(build_id),
                    "needs_tick": True,
                    "scheduler_live": True,
                },
            }
        )
        registry = _registry(recorder)
        assert registry.scheduler_lease_acquire(
            build_id, owner_id="o", ttl_seconds=60
        ).held
        assert dict(recorder.requests[-1].url.params) == {
            "owner_id": "o",
            "ttl_seconds": "60",
        }
        # The release answers whether the caller held it (the server's
        # LeaseResponse): a lost tick's release is visibly a no-op.
        assert registry.scheduler_lease_release(build_id, owner_id="o").held is False
        assert registry.build_notify(build_id, can_spawn=False).scheduler_live is True
        assert recorder.requests[-1].url.params["can_spawn"] == "false"

    def test_the_exclusions_skip_blocked_and_the_execution_ledger(self):
        plan_id, build_id, execution_id = uuid4(), uuid4(), uuid4()
        exclusion = {
            "plan_id": str(plan_id),
            "excluded": ["t1", "t2"],
            "build_failed": False,
        }
        recorder = _Recorder(
            {
                (
                    "POST",
                    f"/api/v2/plans/{plan_id}/members/t1/discovery-failed",
                ): exclusion,
                ("POST", f"/api/v2/plans/{plan_id}/members/t1/exclude"): exclusion,
                ("POST", f"/api/v2/builds/{build_id}/skip-blocked"): {
                    "plan_id": str(plan_id),
                    "skipped": ["t3"],
                },
                ("GET", f"/api/v2/builds/{build_id}/executions"): {
                    "build_id": str(build_id),
                    "executions": [
                        {
                            "id": str(execution_id),
                            "task_id": "t1",
                            "plan_id": str(plan_id),
                            "instance_id": str(uuid4()),
                            "executor": "modal",
                            "executor_ref": "fc-1",
                            "executor_metadata": None,
                            "started_at": NOW.isoformat(),
                            "claim_released_at": None,
                            "claim_outcome": None,
                            "ended_at": None,
                            "outcome": None,
                            "in_current_plan": False,
                        }
                    ],
                },
                ("POST", f"/api/v2/executions/{execution_id}/stopped"): TRANSITION,
            }
        )
        registry = _registry(recorder)
        result = registry.member_discovery_failed(plan_id, "t1", error="boom")
        assert result.excluded == ["t1", "t2"]
        assert recorder.body() == {"error": "boom"}
        registry.member_exclude(plan_id, "t1")
        assert recorder.body() == {}
        assert registry.build_skip_blocked(build_id) == ["t3"]
        (execution,) = registry.build_list_executions(
            build_id, not_in_current_plan=True
        )
        assert execution.still_wanted and not execution.in_current_plan
        assert recorder.requests[-1].url.params["not_in_current_plan"] == "true"
        registry.build_list_executions(build_id, include_ended=True)
        assert recorder.requests[-1].url.params["include_ended"] == "true"
        assert "not_in_current_plan" not in recorder.requests[-1].url.params
        registry.execution_report_stopped(execution_id)
        assert recorder.body() == {"outcome": "stopped"}
        registry.execution_report_stopped(execution_id, outcome="lost")
        assert recorder.body() == {"outcome": "lost"}

    def test_the_scheduling_decisions_carry_no_execution(self):
        plan_id = uuid4()
        recorder = _Recorder(
            {
                ("POST", f"/api/v2/plans/{plan_id}/members/t1/{action}"): TRANSITION
                for action in ("skip", "cancel", "retry")
            }
        )
        registry = _registry(recorder)
        registry.member_skip(plan_id, "t1")
        registry.member_cancel(plan_id, "t1")
        registry.member_retry(plan_id, "t1")
        assert [r.url.path.rsplit("/", 1)[-1] for r in recorder.requests] == [
            "skip",
            "cancel",
            "retry",
        ]
        assert all(not r.content for r in recorder.requests)


class TestReads:
    """The reads the CLI adds: the build listing, a plan's roots with its
    scope, and a task's artifacts."""

    def test_build_list_filters_and_the_running_listing_uses_it(self):
        build_id = uuid4()
        build = {"id": str(build_id), "status": "running", "root_task_ids": ["t"]}
        recorder = _Recorder({("GET", "/api/v2/builds"): {"builds": [build]}})
        registry = _registry(recorder)
        (listed,) = registry.build_list(status="failed", reactive_app_name="app")
        assert listed.id == build_id
        params = recorder.requests[-1].url.params
        assert (params["status"], params["reactive_app_name"]) == ("failed", "app")
        assert registry.build_list_running() == [build_id]
        assert recorder.requests[-1].url.params["status"] == "running"

    def test_plan_roots_carry_the_scope(self):
        plan_id, build_id, deployment_id = uuid4(), uuid4(), uuid4()
        root = {"task_id": "t", "instance_hash": "h", "status": "pending"}
        recorder = _Recorder(
            {
                ("GET", f"/api/v2/plans/{plan_id}/roots"): {
                    "plan_id": str(plan_id),
                    "build_id": str(build_id),
                    "deployment_id": str(deployment_id),
                    "settings_hash": "s",
                    "roots": [root],
                }
            }
        )
        registry = _registry(recorder)
        info = registry.plan_roots_info(plan_id)
        assert (info.build_id, info.deployment_id) == (build_id, deployment_id)
        assert [r.task_id for r in registry.plan_roots(plan_id)] == ["t"]

    def test_task_artifacts(self):
        artifact = {
            "id": str(uuid4()),
            "task_id": "t",
            "artifact_type": "markdown",
            "name": "report",
            "body": {"content": "# hi"},
            "created_at": NOW.isoformat(),
        }
        recorder = _Recorder(
            {("GET", "/api/v2/tasks/t/artifacts"): {"artifacts": [artifact]}}
        )
        (listed,) = _registry(recorder).task_list_artifacts("t")
        assert (listed.artifact_type, listed.name) == ("markdown", "report")


class TestErrors:
    def _refusing(self, status: int, detail: typing.Any) -> APIRegistry:
        return _registry(
            lambda request: httpx.Response(status, json={"detail": detail})
        )

    def test_a_refusal_carries_its_code(self):
        registry = self._refusing(
            409,
            {"code": "instance_conflict", "message": "two instances", "fields": ["w"]},
        )
        with pytest.raises(APIError) as excinfo:
            registry.plan_register_members(uuid4(), [_item()])
        assert excinfo.value.code == "instance_conflict"
        assert excinfo.value.status_code == 409
        assert (excinfo.value.payload or {})["fields"] == ["w"]
        assert "two instances" in str(excinfo.value)

    def test_a_404_is_a_not_found_with_its_payload(self):
        registry = self._refusing(404, {"code": "unknown_build", "message": "no build"})
        with pytest.raises(NotFoundError) as excinfo:
            registry.build_get(uuid4())
        assert excinfo.value.code == "unknown_build"

    @pytest.mark.parametrize(
        "code,wanted",
        [
            ("execution_not_current", False),
            ("execution_superseded", False),
            ("unknown_execution", False),
            ("not_claim_holder", False),
            ("task_already_running", True),
        ],
    )
    def test_a_report_refused_for_a_moved_claim_means_stop(self, code, wanted):
        registry = self._refusing(409, {"code": code, "message": code})
        with pytest.raises(APIError) as excinfo:
            registry.member_complete(uuid4(), "t", execution_id=uuid4())
        assert execution_not_wanted(excinfo.value) is (not wanted)


class _StalledBody(httpx.SyncByteStream, httpx.AsyncByteStream):
    """A body that fails while it is being read: the headers are in."""

    def __init__(self, error: Exception):
        self.error = error

    def __iter__(self) -> typing.Iterator[bytes]:
        raise self.error

    async def __aiter__(self) -> typing.AsyncIterator[bytes]:
        raise self.error
        yield b""  # pragma: no cover


class _Script:
    """Answer each request with the next step: a response, or an exception."""

    def __init__(self, *steps: httpx.Response | Exception):
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _body_stalls() -> httpx.Response:
    return httpx.Response(
        200, stream=_StalledBody(httpx.ReadTimeout("body read timed out"))
    )


def _body_cut() -> httpx.Response:
    return httpx.Response(
        200,
        stream=_StalledBody(
            httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )
        ),
    )


class TestLostExchange:
    """An exchange that got no complete answer is retried, whole."""

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(_api_http, "_TRANSIENT_BACKOFF_SECONDS", 0.0)

    @pytest.mark.parametrize(
        "lost",
        [
            pytest.param(_body_stalls, id="body-stalls"),
            pytest.param(_body_cut, id="body-cut-short"),
            pytest.param(lambda: httpx.ReadTimeout("no headers"), id="no-headers"),
            pytest.param(lambda: httpx.ConnectError("refused"), id="no-connection"),
            pytest.param(lambda: httpx.Response(503), id="gateway-503"),
            pytest.param(
                lambda: httpx.Response(
                    500,
                    text="modal-http: internal error: status InternalFailure: "
                    "Server has lost track of input",
                ),
                id="proxy-500",
            ),
        ],
    )
    def test_a_lost_exchange_is_sent_again(self, lost, caplog):
        script = _Script(lost(), httpx.Response(200, json=BUILD))
        before = sum(transport_retry_counts().values())
        with caplog.at_level("WARNING", logger=_api_http.__name__):
            build = _registry(script).build_get(UUID(BUILD["id"]))
        assert str(build.id) == BUILD["id"]
        assert len(script.requests) == 2
        assert sum(transport_retry_counts().values()) == before + 1
        (record,) = caplog.records
        assert f"GET /builds/{BUILD['id']}" in record.getMessage()
        assert "retry 1 of 3" in record.getMessage()

    def test_a_post_is_retried_with_the_same_body(self):
        script = _Script(_body_stalls(), httpx.Response(200, json=BUILD))
        _registry(script).build_create(name="b", root_task_ids=["t1"])
        first, second = script.requests
        assert first.method == second.method == "POST"
        assert first.content == second.content

    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param(httpx.Response(500, json={"detail": "boom"}), id="app-500"),
            pytest.param(
                httpx.Response(409, json={"detail": {"code": "x"}}), id="refusal"
            ),
            pytest.param(httpx.Response(404, json={"detail": "nope"}), id="404"),
        ],
    )
    def test_an_answer_the_app_wrote_is_not_retried(self, answer):
        script = _Script(answer)
        with pytest.raises(APIError):
            _registry(script).build_get(uuid4())
        assert len(script.requests) == 1

    def test_the_retries_are_bounded_and_the_last_fault_raised(self):
        script = _Script(*(_body_stalls() for _ in range(4)))
        with pytest.raises(httpx.ReadTimeout):
            _registry(script).build_get(uuid4())
        assert len(script.requests) == 4

    def test_a_gateway_error_that_persists_surfaces_as_an_error(self):
        script = _Script(*(httpx.Response(502) for _ in range(4)))
        with pytest.raises(APIError) as excinfo:
            _registry(script).build_get(uuid4())
        assert excinfo.value.status_code == 502
        assert len(script.requests) == 4

    async def test_the_async_path_retries_a_stalled_body(self):
        import asyncio

        script = _Script(_body_stalls(), httpx.Response(200, json=BUILD))
        registry = APIRegistry(api_url="https://registry.test", api_key="sk-test")
        registry._async_client = httpx.AsyncClient(
            transport=httpx.MockTransport(script)
        )
        registry._async_client_loop = asyncio.get_running_loop()
        build = await registry.build_get_aio(UUID(BUILD["id"]))
        assert str(build.id) == BUILD["id"]
        assert len(script.requests) == 2


class TestGzip:
    def test_small_bodies_are_plain_and_large_ones_gzipped(self):
        content, headers = gzip_json_body({"a": 1})
        assert headers == {"Content-Type": "application/json"}
        assert json.loads(content or b"") == {"a": 1}
        large = {"items": [{"k": "x" * 40, "i": i} for i in range(100)]}
        content, headers = gzip_json_body(large)
        assert headers["Content-Encoding"] == "gzip"
        assert json.loads(gzip.decompress(content or b"")) == large
        assert _GZIP_REQUEST_THRESHOLD_BYTES == 1024

    def test_a_large_chunk_goes_out_gzipped(self):
        plan_id = uuid4()
        recorder = _Recorder({("POST", f"/api/v2/plans/{plan_id}/members"): {}})
        _registry(recorder).plan_register_members(
            plan_id, [_item(f"t{i}") for i in range(50)]
        )
        assert recorder.requests[-1].headers["Content-Encoding"] == "gzip"
        assert len(recorder.body()["items"]) == 50


async def test_the_async_methods_send_the_same_requests():
    plan_id = uuid4()
    recorder = _Recorder(
        {("POST", f"/api/v2/plans/{plan_id}/members/t1/complete"): TRANSITION}
    )
    registry = APIRegistry(api_url="https://registry.test", api_key="sk-test")
    import asyncio

    registry._async_client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    registry._async_client_loop = asyncio.get_running_loop()
    execution_id = uuid4()
    await registry.member_complete_aio(plan_id, "t1", execution_id=execution_id)
    assert recorder.body() == {"execution_id": str(execution_id)}
    assert (
        recorder.requests[-1].url.path == f"/api/v2/plans/{plan_id}/members/t1/complete"
    )
