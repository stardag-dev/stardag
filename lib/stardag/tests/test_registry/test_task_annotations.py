"""Task annotations and the task-list discovery filters in the SDK
(STA-149): what ``APIRegistry`` sends to ``PATCH /tasks/{id}`` and ``GET
/tasks``, how it reads an older registry, and the in-memory registry's
mirror of the server's semantics."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from stardag.exceptions import APIError
from stardag.registry import TaskInfo
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import TaskRow
from tests.test_registry.test_api_registry import _Recorder, _registry

TASK = {
    "task_id": "t1",
    "task_namespace": "ns",
    "task_name": "T",
    "status": "completed",
    "tags": ["best", "v2"],
    "description": "A model.",
}


# -- APIRegistry: PATCH /tasks/{task_id} ---------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "body"),
    [
        ({"tags": ["v2", "best"]}, {"tags": ["v2", "best"]}),
        ({"description": "A model."}, {"description": "A model."}),
        ({"description": None}, {"description": None}),
        ({"tags": [], "description": ""}, {"tags": [], "description": ""}),
        ({}, {}),
    ],
)
def test_task_annotate_sends_only_what_is_named(kwargs: dict, body: dict):
    recorder = _Recorder({("PATCH", "/api/v2/tasks/t1"): TASK})
    task = _registry(recorder).task_annotate("t1", **kwargs)
    assert recorder.requests[-1].method == "PATCH"
    assert recorder.body() == body
    assert task.tags == ["best", "v2"] and task.description == "A model."


@pytest.mark.parametrize("tags", [None, "single"])
def test_task_annotate_refuses_an_ambiguous_tags_argument(tags):
    """``...`` (the default) is "unchanged" and ``[]`` "clear"; ``None`` or a
    bare string is neither, and is refused before anything is sent."""
    recorder = _Recorder({("PATCH", "/api/v2/tasks/t1"): TASK})
    with pytest.raises(TypeError, match=r"\[\] clears"):
        _registry(recorder).task_annotate("t1", tags=tags)
    assert recorder.requests == []
    with pytest.raises(TypeError):
        InMemoryRegistry().task_annotate("t1", tags=tags)


def test_task_annotate_against_a_registry_without_it():
    recorder = _Recorder(
        {
            ("PATCH", "/api/v2/tasks/t1"): httpx.Response(
                405, json={"detail": "Method Not Allowed"}
            )
        }
    )
    with pytest.raises(APIError, match="does not support task annotations"):
        _registry(recorder).task_annotate("t1", tags=["x"])


def test_task_info_parses_without_annotations():
    """An older registry serves no ``tags``/``description``."""
    task = TaskInfo.model_validate({"task_id": "t1", "task_name": "T"})
    assert task.tags == [] and task.description is None


# -- APIRegistry: GET /tasks filters ---------------------------------------------


def test_task_list_sends_the_filters_with_repeated_tags():
    recorder = _Recorder({("GET", "/api/v2/tasks"): {"tasks": [TASK]}})
    page = _registry(recorder).task_list(
        status="completed",
        namespace="ns",
        name="T",
        tags=["best", "v2"],
        id_prefix="t",
        q="model",
        limit=5,
        cursor="c",
    )
    params = recorder.requests[-1].url.params
    assert params.get_list("tag") == ["best", "v2"]
    assert {k: params[k] for k in params if k != "tag"} == {
        "limit": "5",
        "cursor": "c",
        "status": "completed",
        "namespace": "ns",
        "name": "T",
        "id_prefix": "t",
        "q": "model",
    }
    assert page.tasks[0].tags == ["best", "v2"]


@pytest.mark.parametrize("tags", ["prod", ""])
def test_task_list_refuses_a_bare_string_of_tags(tags):
    """A string is a ``Sequence[str]`` too: ``"prod"`` would filter by four
    one-character tags and ``""`` by none. Refused before anything is sent,
    by the in-memory registry too."""
    recorder = _Recorder({("GET", "/api/v2/tasks"): {"tasks": []}})
    with pytest.raises(TypeError, match="not a bare string"):
        _registry(recorder).task_list(tags=tags)
    assert recorder.requests == []
    with pytest.raises(TypeError, match="not a bare string"):
        InMemoryRegistry().task_list(tags=tags)


def test_task_list_sends_an_empty_namespace():
    recorder = _Recorder({("GET", "/api/v2/tasks"): {"tasks": []}})
    _registry(recorder).task_list(namespace="")
    assert recorder.requests[-1].url.params["namespace"] == ""


def test_an_older_registry_is_refused_rather_than_answered_unfiltered():
    """It ignores the unknown filters and serves no ``tags``."""
    old = {"task_id": "t1", "task_name": "T", "status": "pending"}
    recorder = _Recorder({("GET", "/api/v2/tasks"): {"tasks": [old]}})
    registry = _registry(recorder)
    with pytest.raises(APIError, match="does not support the filters q, tag"):
        registry.task_list(tags=["x"], q="y")
    # Without discovery filters an older registry is read as before.
    assert registry.task_list(status="pending").tasks[0].tags == []


# -- InMemoryRegistry ------------------------------------------------------------


def _seed(registry: InMemoryRegistry) -> None:
    now = datetime.now(timezone.utc)
    for i, (task_id, namespace, name) in enumerate(
        [
            ("aa11", "ml", "Train"),
            ("aa22", "ml", "Train"),
            ("bb33", "ml", "Evaluate"),
            ("cc44", "", "Load"),
        ]
    ):
        registry.tasks[task_id] = TaskRow(
            task_id=task_id,
            task_namespace=namespace,
            task_name=name,
            version="1",
            output_uri=None,
            status_at=now.replace(microsecond=i),
        )


def test_in_memory_annotate_mirrors_the_server():
    registry = InMemoryRegistry()
    _seed(registry)
    status_at = registry.tasks["aa11"].status_at

    task = registry.task_annotate(
        "aa11", tags=[" best ", "v2", "best"], description="  A model. "
    )
    assert (task.tags, task.description) == (["best", "v2"], "A model.")
    assert registry.task_annotate("aa11", description="Better.").tags == [
        "best",
        "v2",
    ]
    assert registry.task_annotate("aa11", tags=["x"]).description == "Better."
    cleared = registry.task_annotate("aa11", tags=[], description="  ")
    assert (cleared.tags, cleared.description) == ([], None)
    assert registry.tasks["aa11"].status_at == status_at

    for bad in ([""], ["x" * 65], ["a\nb"], [f"t{i}" for i in range(33)]):
        with pytest.raises(APIError) as refused:
            registry.task_annotate("aa11", tags=bad)
        assert refused.value.status_code == 422
    with pytest.raises(APIError) as refused:  # 129 raw entries, 1 distinct
        registry.task_annotate("aa11", tags=["x"] * 129)
    assert refused.value.status_code == 422
    assert registry.task_annotate("aa11", tags=["x"] * 128).tags == ["x"]
    for bad_description in ("d" * 4097, "before\x00after", " " * 16385):
        with pytest.raises(APIError) as refused:
            registry.task_annotate("aa11", description=bad_description)
        assert refused.value.status_code == 422
    assert registry.task_annotate("aa11", description="a\nb").description == "a\nb"
    with pytest.raises(APIError):
        registry.task_annotate("missing", tags=["x"])


@pytest.mark.parametrize(
    ("filters", "status"),
    [
        ({"q": ""}, 422),
        ({"q": "ab"}, 422),
        ({"id_prefix": ""}, 422),
        ({"q": "q" * 256}, 422),
        ({"id_prefix": "a" * 65}, 422),
        ({"name": "n" * 256}, 422),
        ({"namespace": "n" * 256}, 422),
        ({"q": "ab\x00"}, 400),
        ({"name": "\x00"}, 400),
        ({"tags": [""]}, 400),
    ],
)
def test_in_memory_task_list_refuses_what_the_route_refuses(filters, status):
    registry = InMemoryRegistry()
    _seed(registry)
    with pytest.raises(APIError) as refused:
        registry.task_list(**filters)
    assert refused.value.status_code == status


def test_in_memory_task_list_filters_mirror_the_server():
    registry = InMemoryRegistry()
    _seed(registry)
    registry.task_annotate("aa11", tags=["best", "prod"], description="Tuned run")
    registry.task_annotate("aa22", tags=["best"])

    def listed(**filters) -> set[str]:
        return {t.task_id for t in registry.task_list(**filters).tasks}

    assert listed(namespace="ml") == {"aa11", "aa22", "bb33"}
    assert listed(namespace="") == {"cc44"}  # the root namespace, not "no filter"
    assert listed(name="Train") == {"aa11", "aa22"}
    assert listed(tags=["best"]) == {"aa11", "aa22"}
    assert listed(tags=["best", "prod"]) == {"aa11"}
    assert listed(id_prefix="aa") == {"aa11", "aa22"}
    assert listed(q="TUNED") == {"aa11"}
    assert listed(q="ml.") == set()
    assert listed(q="TRAIN") == {"aa11", "aa22"}
    assert listed(q="Train", tags=["prod"]) == {"aa11"}
