"""Reading one instance: ``task_instance_get`` over HTTP (the
``GET /instances/{id}`` route, and the fallback through ``GET /tasks/{id}``
against a registry that predates it), the local instance-body cache it
reads through, and the pinned ``from_registry(..., instance_id=...)`` built
on both.

The cache directory is per test (``isolated_registry_cache`` in the root
conftest), and no test here reaches a real registry: every
``APIRegistry`` runs on an ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import typing
from pathlib import Path
from typing import Annotated
from uuid import UUID, uuid4

import httpx
import pytest

import stardag as sd
from stardag.config import StardagConfig, config_provider
from stardag.exceptions import NotFoundError
from stardag.registry import APIRegistry, TaskInstanceInfo
from stardag.registry import _instance_cache
from stardag.target import InMemoryFileTarget

sd.auto_namespace(__name__)

API = "https://registry.test"


class TrainedModel(sd.Task[str]):
    learning_rate: float
    notes: Annotated[str, sd.StardagField(significant=False)] = ""

    def run(self):
        self._save(f"model@{self.learning_rate}")


def _instance_payload(
    task: sd.BaseTask, instance_id: UUID, *, with_task_id: bool = True
) -> dict[str, typing.Any]:
    payload: dict[str, typing.Any] = {
        "id": str(instance_id),
        "deployment_id": str(uuid4()),
        "settings_hash": str(uuid4()),
        "instance_hash": "h",
        "body": task.instance_body(),
        "expanded_at": None,
        "created_at": "2026-10-27T00:00:00Z",
    }
    if with_task_id:
        payload["task_id"] = str(task.id)
    return payload


def _task_payload(task_id: str, instances: list[dict]) -> dict[str, typing.Any]:
    return {"task_id": task_id, "status": "completed", "instances": instances}


def _route_missing() -> httpx.Response:
    """FastAPI's answer for a path no route serves (a registry that
    predates ``GET /instances/{id}``). A fresh one per use: a response's
    stream is consumed once."""
    return httpx.Response(404, json={"detail": "Not Found"})


class _Server:
    """A recording handler: a response per ``(method, path)``. A path with
    no response fails the test (an ``AssertionError`` is not retried by the
    transport): ``_Server()`` stands in for being offline."""

    def __init__(self, responses: dict[tuple[str, str], typing.Any] | None = None):
        self.responses = responses or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.path)
        if key not in self.responses:
            raise AssertionError(f"offline: unexpected {key}")
        body = self.responses[key]
        if isinstance(body, httpx.Response):
            return body
        return httpx.Response(200, json=body)


def _registry(
    server: _Server,
    *,
    api_key: str | None = "sk-one",
    environment_id: str | None = None,
) -> APIRegistry:
    registry = APIRegistry(api_url=API, api_key=api_key, environment_id=environment_id)
    registry._client = httpx.Client(transport=httpx.MockTransport(server))
    return registry


def _use_async_transport(registry: APIRegistry, server: _Server) -> None:
    """Call from inside the running loop: the async client is per loop."""
    registry._async_client = httpx.AsyncClient(transport=httpx.MockTransport(server))
    registry._async_client_loop = asyncio.get_running_loop()


@pytest.fixture
def model(default_in_memory_fs_target: typing.Type[InMemoryFileTarget]):
    return TrainedModel(learning_rate=0.1, notes="first run")


# -- the cache itself -----------------------------------------------------------


class TestCacheEntries:
    def _info(self, task_id: str = "t") -> TaskInstanceInfo:
        return TaskInstanceInfo(
            id=uuid4(),
            task_id=task_id,
            deployment_id=uuid4(),
            settings_hash="s",
            instance_hash="h",
            body={"__namespace": "", "__name": "X"},
        )

    def test_round_trip(self, isolated_registry_cache: Path):
        info = self._info()
        _instance_cache.write("p", info)
        assert _instance_cache.read("p", info.id) == info
        assert _instance_cache.read("p", str(info.id)) == info
        # Only the entry: the temporary file was renamed into place.
        assert [p.name for p in (isolated_registry_cache / "p").iterdir()] == [
            f"{info.id}.json"
        ]

    def test_partitions_do_not_share_entries(self):
        info = self._info()
        _instance_cache.write("p", info)
        assert _instance_cache.read("q", info.id) is None

    @pytest.mark.parametrize(
        "content",
        [
            b"{not json",
            b'{"version": 99, "instance": {}}',
            b'{"version": 1}',
            b"[]",
            b"\xff\xfe not utf-8",
        ],
    )
    def test_a_corrupt_entry_is_discarded_as_a_miss(
        self, isolated_registry_cache: Path, content: bytes
    ):
        info = self._info()
        _instance_cache.write("p", info)
        path = isolated_registry_cache / "p" / f"{info.id}.json"
        path.write_bytes(content)
        assert _instance_cache.read("p", info.id) is None
        assert not path.exists()

    def test_an_entry_naming_another_instance_is_discarded(
        self, isolated_registry_cache: Path
    ):
        info, other = self._info(), uuid4()
        _instance_cache.write("p", info)
        (isolated_registry_cache / "p" / f"{info.id}.json").rename(
            isolated_registry_cache / "p" / f"{other}.json"
        )
        assert _instance_cache.read("p", other) is None
        assert not (isolated_registry_cache / "p" / f"{other}.json").exists()

    def test_expanded_at_is_not_cached(self):
        """The one mutable column: a hit reports it as unknown rather than
        freezing the value of the first read."""
        from datetime import datetime, timezone

        info = self._info().model_copy(
            update={"expanded_at": datetime(2026, 10, 27, tzinfo=timezone.utc)}
        )
        _instance_cache.write("p", info)
        hit = _instance_cache.read("p", info.id)
        assert hit is not None and hit.expanded_at is None
        assert hit.body == info.body

    def test_an_instance_without_task_id_is_not_cached(self):
        info = self._info().model_copy(update={"task_id": None})
        _instance_cache.write("p", info)
        assert _instance_cache.read("p", info.id) is None

    def test_a_non_uuid_id_never_reaches_the_path(self):
        with pytest.raises(ValueError):
            _instance_cache.read("p", "../../etc/passwd")
        with pytest.raises(ValueError):
            _instance_cache.read("../elsewhere", uuid4())

    def test_clear_one_partition_or_all(self):
        for partition in ("p", "p", "q"):
            _instance_cache.write(partition, self._info())
        assert _instance_cache.clear("p") == 2
        assert _instance_cache.clear("p") == 0
        assert _instance_cache.clear() == 1


class TestPartition:
    def test_api_key_registries_partition_by_key(self):
        one = _registry(_Server(), api_key="sk-one")
        same = _registry(_Server(), api_key="sk-one")
        other = _registry(_Server(), api_key="sk-two")
        assert one.instance_cache_partition() == same.instance_cache_partition()
        assert one.instance_cache_partition() != other.instance_cache_partition()

    def test_the_raw_key_is_not_in_the_partition(self):
        partition = _registry(_Server(), api_key="sk-secret").instance_cache_partition()
        assert partition is not None
        assert "sk-secret" not in partition
        assert partition.startswith("registry.test-")

    def test_environments_and_urls_partition_apart(self):
        def partition(url: str, env: str) -> str | None:
            registry = APIRegistry(api_url=url, environment_id=env)
            return registry.instance_cache_partition()

        assert partition(API, "env-a") != partition(API, "env-b")
        assert partition(API, "env-a") != partition("https://other.test", "env-a")
        assert partition(API, "env-a") == partition(API + "/", "env-a")

    def test_no_partition_without_a_known_environment(self):
        registry = APIRegistry(api_url=API)
        assert registry.instance_cache_partition() is None

    def test_turned_off_by_config(self):
        registry = _registry(_Server())
        assert _instance_cache.registry_partition(registry) is not None
        with config_provider.override(StardagConfig(registry_cache=False)):
            assert _instance_cache.registry_partition(registry) is None

    def test_turned_off_by_env_var(self, monkeypatch: pytest.MonkeyPatch):
        from stardag.config import load_config

        monkeypatch.setenv("STARDAG_NO_REGISTRY", "1")
        assert load_config().registry_cache is True
        monkeypatch.setenv("STARDAG_REGISTRY_CACHE", "0")
        assert load_config().registry_cache is False


# -- task_instance_get ------------------------------------------------------------


class TestTaskInstanceGet:
    def test_reads_the_route_and_serves_a_repeat_from_the_cache(self, model):
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)

        first = registry.task_instance_get(iid)
        again = registry.task_instance_get(str(iid))

        assert first.task_id == str(model.id)
        assert first.body == model.instance_body()
        assert again == first
        assert len(server.requests) == 1

    def test_a_fresh_client_reads_the_cache_with_no_request(self, model):
        iid = uuid4()
        online = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        _registry(online).task_instance_get(iid)

        offline = _Server()
        info = _registry(offline).task_instance_get(iid, task_id=str(model.id))

        assert info.task_id == str(model.id)
        assert offline.requests == []

    def test_another_profile_does_not_see_the_entry(self, model):
        iid = uuid4()
        route = {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        _registry(_Server(route), api_key="sk-one").task_instance_get(iid)

        other = _Server(route)
        _registry(other, api_key="sk-two").task_instance_get(iid)
        assert len(other.requests) == 1  # asked its own registry

    def test_a_hit_compares_task_ids_as_uuids(self, model):
        """A differently formatted task id of the same task is still a hit."""
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)
        registry.task_instance_get(iid)
        registry.task_instance_get(iid, task_id=str(model.id).upper())
        assert len(server.requests) == 1

    def test_a_hit_for_another_task_id_is_re_read(self, model):
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)
        registry.task_instance_get(iid)

        info = registry.task_instance_get(iid, task_id="some-other-task")

        assert len(server.requests) == 2
        assert info.task_id == str(model.id)  # not checked here: the caller does

    def test_falls_back_to_the_task_against_an_older_registry(self, model):
        iid = uuid4()
        newest = _instance_payload(model, uuid4(), with_task_id=False)
        pinned = _instance_payload(model, iid, with_task_id=False)
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): _route_missing(),
                ("GET", f"/api/v2/tasks/{model.id}"): _task_payload(
                    str(model.id), [newest, pinned]
                ),
            }
        )
        registry = _registry(server)

        info = registry.task_instance_get(iid, task_id=str(model.id))

        assert info.id == iid
        assert info.task_id == str(model.id)  # filled in from the task
        assert server.requests[-1].url.params["limit"] == "500"
        # ... and cached like any other read.
        registry.task_instance_get(iid, task_id=str(model.id))
        assert len(server.requests) == 2

    def test_the_fallback_misses_an_instance_the_task_does_not_list(self, model):
        iid = uuid4()
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): _route_missing(),
                ("GET", f"/api/v2/tasks/{model.id}"): _task_payload(str(model.id), []),
            }
        )
        with pytest.raises(NotFoundError, match="has no instance"):
            _registry(server).task_instance_get(iid, task_id=str(model.id))

    def test_an_older_registry_without_a_task_id_says_what_to_pass(self):
        iid = uuid4()
        server = _Server({("GET", f"/api/v2/instances/{iid}"): _route_missing()})
        with pytest.raises(NotFoundError, match="pass task_id="):
            _registry(server).task_instance_get(iid)

    def test_an_unknown_instance_on_a_newer_registry_does_not_fall_back(self, model):
        iid = uuid4()
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): httpx.Response(
                    404, json={"detail": {"code": "unknown_instance", "message": "no"}}
                )
            }
        )
        with pytest.raises(NotFoundError) as info:
            _registry(server).task_instance_get(iid, task_id=str(model.id))
        assert info.value.code == "unknown_instance"
        assert len(server.requests) == 1

    def test_a_structured_not_found_message_does_not_fall_back(self, model):
        """Only FastAPI's bare missing-route body falls back: a structured
        refusal is the route answering, whatever its message says."""
        iid = uuid4()
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): httpx.Response(
                    404,
                    json={
                        "detail": {"code": "unknown_instance", "message": "Not Found"}
                    },
                )
            }
        )
        with pytest.raises(NotFoundError) as info:
            _registry(server).task_instance_get(iid, task_id=str(model.id))
        assert info.value.code == "unknown_instance"
        assert len(server.requests) == 1

    def test_no_cache_when_turned_off(self, model, isolated_registry_cache: Path):
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)
        with config_provider.override(StardagConfig(registry_cache=False)):
            registry.task_instance_get(iid)
            registry.task_instance_get(iid)
        assert len(server.requests) == 2
        assert not any(isolated_registry_cache.rglob("*.json"))

    async def test_aio_reads_through_the_same_cache(
        self, model, monkeypatch: pytest.MonkeyPatch
    ):
        iid = uuid4()
        newest = _instance_payload(model, iid, with_task_id=False)
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): _route_missing(),
                ("GET", f"/api/v2/tasks/{model.id}"): _task_payload(
                    str(model.id), [newest]
                ),
            }
        )
        registry = _registry(server)

        _use_async_transport(registry, server)
        loop_thread = threading.get_ident()
        cache_threads: list[int] = []
        for name in ("read", "write"):
            original = getattr(_instance_cache, name)

            def spy(*args, _original=original, **kwargs):
                cache_threads.append(threading.get_ident())
                return _original(*args, **kwargs)

            monkeypatch.setattr(_instance_cache, name, spy)
        first = await registry.task_instance_get_aio(iid, task_id=str(model.id))
        again = await registry.task_instance_get_aio(iid, task_id=str(model.id))
        # The cache's file I/O ran off the event loop's thread.
        assert len(cache_threads) == 3  # miss, write, hit
        assert loop_thread not in cache_threads
        assert first == again and first.task_id == str(model.id)
        assert len(server.requests) == 2  # route + fallback, then the cache


# -- from_registry, pinned ---------------------------------------------------------


class TestFromRegistryPinned:
    def test_rehydrates_offline_after_one_online_read(self, model):
        iid = uuid4()
        online = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        loaded = TrainedModel.from_registry(
            model.id, registry=_registry(online), instance_id=iid
        )
        assert isinstance(loaded, TrainedModel)
        assert loaded == model and loaded.notes == "first run"

        offline = _Server()
        again = TrainedModel.from_registry(
            str(model.id), registry=_registry(offline), instance_id=str(iid)
        )
        assert again == model
        assert offline.requests == []

    def test_pins_the_instance_not_the_newest(self, model):
        iid = uuid4()
        newer = model.model_copy(update={"notes": "a later run"})
        assert newer.id == model.id  # non-significant: same completion
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid),
                ("GET", f"/api/v2/tasks/{model.id}"): _task_payload(
                    str(model.id), [_instance_payload(newer, uuid4())]
                ),
            }
        )
        registry = _registry(server)
        newest = TrainedModel.from_registry(model.id, registry=registry)
        pinned = TrainedModel.from_registry(
            model.id, registry=registry, instance_id=iid
        )
        assert isinstance(newest, TrainedModel) and isinstance(pinned, TrainedModel)
        assert newest.notes == "a later run"
        assert pinned.notes == "first run"

    def test_works_against_an_older_registry(self, model):
        iid = uuid4()
        server = _Server(
            {
                ("GET", f"/api/v2/instances/{iid}"): _route_missing(),
                ("GET", f"/api/v2/tasks/{model.id}"): _task_payload(
                    str(model.id), [_instance_payload(model, iid, with_task_id=False)]
                ),
            }
        )
        loaded = TrainedModel.from_registry(
            model.id, registry=_registry(server), instance_id=iid
        )
        assert loaded == model

    def test_accepts_a_differently_formatted_task_id(self, model):
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        loaded = TrainedModel.from_registry(
            str(model.id).upper(), registry=_registry(server), instance_id=iid
        )
        assert loaded == model

    def test_refuses_an_instance_of_another_task(self, model):
        other = TrainedModel(learning_rate=0.2)
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(other, iid)}
        )
        with pytest.raises(sd.TaskRehydrationError, match="belongs to task"):
            TrainedModel.from_registry(
                model.id, registry=_registry(server), instance_id=iid
            )

    def test_a_cached_body_that_no_longer_matches_is_re_read(
        self, model, isolated_registry_cache: Path
    ):
        """A cached copy that parses but recomputes to another task id is
        discarded and read once more from the registry, not trusted."""
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)
        registry.task_instance_get(iid)
        (entry,) = isolated_registry_cache.rglob(f"{iid}.json")
        payload = json.loads(entry.read_text())
        payload["instance"]["body"]["learning_rate"] = 0.5
        entry.write_text(json.dumps(payload))

        loaded = TrainedModel.from_registry(
            model.id, registry=registry, instance_id=iid
        )

        assert loaded == model
        assert len(server.requests) == 2

    def test_a_cached_body_that_does_not_validate_is_re_read(
        self, model, isolated_registry_cache: Path
    ):
        iid = uuid4()
        server = _Server(
            {("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid)}
        )
        registry = _registry(server)
        registry.task_instance_get(iid)
        (entry,) = isolated_registry_cache.rglob(f"{iid}.json")
        payload = json.loads(entry.read_text())
        payload["instance"]["body"]["learning_rate"] = "not a number"
        entry.write_text(json.dumps(payload))

        loaded = TrainedModel.from_registry(
            model.id, registry=registry, instance_id=iid
        )

        assert loaded == model
        assert len(server.requests) == 2

    def test_a_body_that_does_not_validate_fails_and_is_not_kept(
        self, model, isolated_registry_cache: Path
    ):
        iid = uuid4()
        body = _instance_payload(model, iid)
        body["body"]["learning_rate"] = "not a number"
        server = _Server({("GET", f"/api/v2/instances/{iid}"): body})
        registry = _registry(server)
        with pytest.raises(sd.TaskRehydrationError, match="Failed to rehydrate"):
            TrainedModel.from_registry(model.id, registry=registry, instance_id=iid)
        # Fresh from the registry, so not read twice: it would come back the same.
        assert len(server.requests) == 1
        assert not any(isolated_registry_cache.rglob("*.json"))

    def test_a_body_that_does_not_match_fails_and_is_not_kept(
        self, model, isolated_registry_cache: Path
    ):
        iid = uuid4()
        body = _instance_payload(model, iid)
        body["body"]["learning_rate"] = 0.5  # recomputes to another task id
        server = _Server({("GET", f"/api/v2/instances/{iid}"): body})
        registry = _registry(server)
        with pytest.raises(sd.TaskRehydrationError, match="does not match"):
            TrainedModel.from_registry(model.id, registry=registry, instance_id=iid)
        # Fresh from the registry, so not read twice: it would come back the same.
        assert len(server.requests) == 1
        assert not any(isolated_registry_cache.rglob("*.json"))


def test_alias_task_records_the_pinned_instance_body(model):
    iid = uuid4()
    newer = model.model_copy(update={"notes": "a later run"})
    task = _task_payload(str(model.id), [_instance_payload(newer, uuid4())])
    task["output_uri"] = "memory://model"
    server = _Server(
        {
            ("GET", f"/api/v2/instances/{iid}"): _instance_payload(model, iid),
            ("GET", f"/api/v2/tasks/{model.id}"): task,
        }
    )
    alias = sd.AliasTask[str].from_registry(
        model.id, registry=_registry(server), instance_id=iid
    )
    assert alias.aliased.body is not None
    assert alias.aliased.body["notes"] == "first run"


def test_alias_task_compares_task_ids_as_uuids(model):
    iid = uuid4()
    payload = _instance_payload(model, iid)
    payload["task_id"] = str(model.id).upper()
    task = _task_payload(str(model.id), [])
    task["output_uri"] = "memory://model"
    server = _Server(
        {
            ("GET", f"/api/v2/instances/{iid}"): payload,
            ("GET", f"/api/v2/tasks/{model.id}"): task,
        }
    )
    alias = sd.AliasTask[str].from_registry(
        model.id, registry=_registry(server), instance_id=iid
    )
    assert alias.aliased.id == model.id
