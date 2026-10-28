"""Build-start hydration: every engine replaces each ``TaskRef`` in its DAG
with the hydrated task before discovery, so the registry stores real bodies
and ``run()`` sees real tasks. Driven against the in-memory registry; no
real registry, no Modal."""

from __future__ import annotations

import asyncio
import json

import pytest

import stardag as sd
from stardag._core.task_ref import TaskRef, clear_hydration_memo
from stardag.build import BuildExitStatus
from stardag.build._registration import walk_aio
from stardag.testing import InMemoryRegistry

sd.auto_namespace(__name__)


# Async-only tasks run in the build's own process, so the in-memory target
# root is shared with the test.


class Dataset(sd.Task[list[int]]):
    size: int

    async def run_aio(self):
        await self._save_aio(list(range(self.size)))


class TrainedModel(sd.Task[str]):
    data: sd.TaskLoads[list[int]]
    learning_rate: float = 0.1

    def requires(self):
        return self.data

    async def run_aio(self):
        await self._save_aio(f"model({sum(await self.data.load_aio())})")


class Evaluate(sd.Task[str]):
    model: sd.TaskLoads[str]

    def requires(self):
        return self.model

    async def run_aio(self):
        assert not isinstance(self.model, TaskRef)
        await self._save_aio(f"evaluated {await self.model.load_aio()}")


class Compare(sd.Task[str]):
    models: list[sd.TaskLoads[str]]

    def requires(self):
        return self.models

    async def run_aio(self):
        assert not any(isinstance(m, TaskRef) for m in self.models)
        await self._save_aio(" vs ".join([await m.load_aio() for m in self.models]))


class RefInRequires(sd.Task[str]):
    """Builds its upstream reference inside ``requires()``."""

    model_id: str

    def requires(self):
        return TrainedModel.ref(self.model_id)

    def run(self):
        self._save("ok")


class YieldsRef(sd.Task[str]):
    """Yields a reference as a dynamic dependency."""

    model_id: str

    async def run_aio(self):  # type: ignore[override]
        yield TrainedModel.ref(self.model_id)
        await self._save_aio("after the model")


class YieldsAndLoadsRef(sd.Task[str]):
    """Yields a reference, then loads it once resumed."""

    model_id: str

    async def run_aio(self):  # type: ignore[override]
        model = TrainedModel.ref(self.model_id)
        yield model
        await self._save_aio(f"after {await model.load_aio()}")


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_hydration_memo()
    yield
    clear_hydration_memo()


@pytest.fixture
def registry() -> InMemoryRegistry:
    return InMemoryRegistry()


@pytest.fixture
def trained(registry, default_in_memory_fs_target) -> TrainedModel:
    """A model built (and so registered) under the in-memory registry."""
    model = TrainedModel(data=Dataset(size=4), learning_rate=0.3)
    summary = sd.build(model, registry=registry)
    assert summary.status == BuildExitStatus.SUCCESS
    return model


def _stored_bodies(registry: InMemoryRegistry) -> list[dict]:
    return [row.body for row in registry.instances.values()]


ENGINES = {
    "build": lambda tasks, registry: sd.build(tasks, registry=registry),
    "build_aio": lambda tasks, registry: asyncio.run(
        sd.build_aio(tasks, registry=registry)
    ),
    "build_sequential": lambda tasks, registry: sd.build_sequential(
        tasks, registry=registry
    ),
    "build_sequential_aio": lambda tasks, registry: asyncio.run(
        sd.build_sequential_aio(tasks, registry=registry)
    ),
}


@pytest.mark.parametrize("engine", ENGINES.values(), ids=ENGINES.keys())
def test_a_downstream_task_of_a_ref_builds_and_registers_real_bodies(
    engine, registry, trained
):
    evaluation = Evaluate(model=TrainedModel.ref(trained.id))
    assert evaluation.id == Evaluate(model=trained).id
    summary = engine(evaluation, registry)
    assert summary.status == BuildExitStatus.SUCCESS
    assert evaluation.load() == "evaluated model(6)"
    stored = json.dumps(_stored_bodies(registry))
    assert "task_class" not in stored
    (evaluate_body,) = [
        b for b in _stored_bodies(registry) if b["__name"] == "Evaluate"
    ]
    assert evaluate_body["model"] == trained.instance_body()


@pytest.mark.parametrize("engine", ENGINES.values(), ids=ENGINES.keys())
def test_a_ref_root_builds_and_loads(engine, registry, trained):
    ref = TrainedModel.ref(trained.id)
    summary = engine([ref], registry)
    assert summary.status == BuildExitStatus.SUCCESS
    assert ref.hydrate(registry).load() == "model(6)"
    with sd.registry_provider.override(registry):
        assert ref.load() == "model(6)"


def test_refs_in_a_list_field_are_hydrated(registry, trained):
    other = TrainedModel(data=Dataset(size=2))
    sd.build(other, registry=registry)
    compare = Compare(models=[TrainedModel.ref(trained.id), TrainedModel.ref(other.id)])
    assert compare.id == Compare(models=[trained, other]).id
    summary = sd.build(compare, registry=registry)
    assert summary.status == BuildExitStatus.SUCCESS
    assert compare.load() == "model(6) vs model(1)"


def test_a_pinned_ref_builds_against_its_instance(registry, trained):
    (instance_id,) = [
        row.id for row in registry.instances.values() if row.task_id == str(trained.id)
    ]
    evaluation = Evaluate(model=TrainedModel.ref(trained.id, instance_id=instance_id))
    assert sd.build(evaluation, registry=registry).status == BuildExitStatus.SUCCESS


def test_a_ref_returned_by_requires_is_hydrated_by_the_walk(registry, trained):
    task = RefInRequires(model_id=str(trained.id))
    with sd.registry_provider.override(registry):
        walk = asyncio.run(walk_aio([task]))
    (dep,) = walk.deps[task.id]
    assert type(dep) is TrainedModel
    assert dep == trained


def test_the_walk_hydrates_its_roots(registry, trained):
    with sd.registry_provider.override(registry):
        walk = asyncio.run(walk_aio([TrainedModel.ref(trained.id)]))
    assert walk.roots == [trained]


def test_a_build_with_an_unknown_ref_fails_before_registering(registry, trained):
    from uuid import uuid4

    with pytest.raises(sd.TaskRefHydrationError, match="holds no task"):
        sd.build(Evaluate(model=TrainedModel.ref(uuid4())), registry=registry)
    assert not any(b["__name"] == "Evaluate" for b in _stored_bodies(registry))


def test_a_ref_in_requires_is_read_from_the_builds_registry(registry, trained):
    """No configured registry: the reference must come from the one passed
    to the build."""
    task = RefInRequires(model_id=str(trained.id))
    assert sd.build(task, registry=registry).status == BuildExitStatus.SUCCESS


@pytest.mark.parametrize(
    "engine",
    [ENGINES["build_aio"], ENGINES["build_sequential_aio"]],
    ids=["build_aio", "build_sequential_aio"],
)
def test_a_yielded_ref_is_hydrated_before_the_yield_is_registered(
    engine, registry, trained
):
    task = YieldsRef(model_id=str(trained.id))
    summary = engine(task, registry)
    assert summary.status == BuildExitStatus.SUCCESS
    assert task.load() == "after the model"
    assert "task_class" not in json.dumps(_stored_bodies(registry))


@pytest.mark.parametrize(
    "engine",
    [ENGINES["build_aio"], ENGINES["build_sequential_aio"]],
    ids=["build_aio", "build_sequential_aio"],
)
def test_a_yielded_ref_loads_from_the_builds_registry_after_resumption(
    engine, registry, trained
):
    """No configured registry: the reference the task yielded was hydrated
    from the build's registry, and its own ``load()`` reads that one."""
    task = YieldsAndLoadsRef(model_id=str(trained.id))
    assert engine(task, registry).status == BuildExitStatus.SUCCESS
    assert task.load() == "after model(6)"
