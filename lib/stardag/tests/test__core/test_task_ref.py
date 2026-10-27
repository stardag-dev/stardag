"""``Task.ref``: identity without hydration, hydration and its checks,
delegation, and the refusal to store a reference.

No test reaches a real registry: hydration reads an ``InMemoryRegistry``
(or a ``Mock``), and the root conftest keeps the configured registry off
and the body cache per test.
"""

from __future__ import annotations

import asyncio
import pickle
import warnings
from typing import Annotated
from unittest.mock import Mock
from uuid import UUID, uuid4

import pytest

import stardag as sd
from stardag._core.task_ref import (
    TaskRef,
    clear_hydration_memo,
    contains_refs,
    resolve_refs,
)
from stardag.base_model import CONTEXT_MODE_KEY
from stardag.registry import NoOpRegistry, RegistryABC
from stardag.testing import InMemoryRegistry

sd.auto_namespace(__name__)


class Dataset(sd.Task[list[int]]):
    size: int

    def run(self):
        self._save(list(range(self.size)))


class TrainedModel(sd.Task[str]):
    data: sd.TaskLoads[list[int]]
    learning_rate: float = 0.1
    notes: Annotated[str, sd.StardagField(significant=False)] = ""

    def run(self):
        self._save(f"model({sum(self.data.load())}, {self.learning_rate})")


class Evaluate(sd.Task[float]):
    model: sd.TaskLoads[str]

    def run(self):
        self._save(float(len(self.model.load())))


class Report(sd.Task[str]):
    evaluation: sd.TaskLoads[float]
    extra: list[sd.TaskLoads[str]] = []
    by_name: dict[str, sd.TaskLoads[str]] = {}

    def run(self):
        self._save("report")


class OtherModel(sd.Task[str]):
    seed: int

    def run(self):
        self._save("other")


@pytest.fixture(autouse=True)
def _fresh_memo():
    clear_hydration_memo()
    yield
    clear_hydration_memo()


def _store(registry: InMemoryRegistry, task: sd.BaseTask) -> UUID:
    """Register ``task`` as one instance (no build): the row a build would
    have written. Returns the instance id."""
    instance_id = uuid4()
    registry.instances[instance_id] = _instance_row(instance_id, task)
    registry.tasks.setdefault(str(task.id), _task_row(registry, task))
    return instance_id


def _instance_row(instance_id: UUID, task: sd.BaseTask):
    from stardag.testing._registry_state import InstanceRow

    return InstanceRow(
        id=instance_id,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash=str(task.instance_hash),
        task_id=str(task.id),
        body=task.instance_body(),
    )


def _task_row(registry: InMemoryRegistry, task: sd.BaseTask):
    from stardag.testing._registry_state import TaskRow

    return TaskRow(
        task_id=str(task.id),
        task_namespace=task.get_namespace(),
        task_name=task.get_name(),
        version=task.version,
        output_uri=None,
    )


@pytest.fixture
def model() -> TrainedModel:
    return TrainedModel(data=Dataset(size=3), learning_rate=0.5, notes="first")


class TestIdentity:
    def test_construction_makes_no_registry_call(self, model):
        registry = Mock(spec=RegistryABC)
        with sd.registry_provider.override(registry):
            ref = TrainedModel.ref(model.id)
            Evaluate(model=ref).id
        assert registry.mock_calls == []

    def test_the_id_is_the_referenced_task_id(self, model):
        ref = TrainedModel.ref(str(model.id), instance_id=str(uuid4()))
        assert ref.id == model.id
        assert isinstance(ref, TaskRef)
        assert isinstance(ref, sd.Task)

    def test_hash_mode_is_the_id_only(self, model):
        ref = TrainedModel.ref(model.id)
        dumped = ref.model_dump(mode="json", context={CONTEXT_MODE_KEY: "hash"})
        assert dumped == {"id": str(model.id)}

    def test_a_downstream_task_keys_the_same_at_depth_two(self, model):
        real = Report(evaluation=Evaluate(model=model))
        via_ref = Report(evaluation=Evaluate(model=TrainedModel.ref(model.id)))
        assert via_ref.id == real.id

    def test_a_ref_deep_in_the_dag_keys_the_same(self, model):
        data_ref = Dataset.ref(model.data.id)
        rebuilt = TrainedModel(data=data_ref, learning_rate=0.5, notes="first")
        assert rebuilt.id == model.id
        assert Report(evaluation=Evaluate(model=rebuilt)).id == (
            Report(evaluation=Evaluate(model=model)).id
        )

    def test_refs_in_lists_and_dicts_key_the_same(self, model):
        ref = TrainedModel.ref(model.id)
        evaluation = Evaluate(model=model)
        real = Report(evaluation=evaluation, extra=[model], by_name={"m": model})
        via_ref = Report(evaluation=evaluation, extra=[ref], by_name={"m": ref})
        assert via_ref.id == real.id

    def test_another_loaded_type_warns_at_runtime_too(self, model):
        with pytest.warns(UserWarning, match="not compatible"):
            Evaluate(model=Dataset.ref(model.data.id))  # type: ignore[arg-type]

    def test_a_matching_loaded_type_does_not_warn(self, model):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            Evaluate(model=TrainedModel.ref(model.id))

    def test_function_tasks_get_a_typed_ref(self):
        @sd.task
        def make_number(x: int) -> int:
            return x

        ref = make_number.ref(make_number(x=1).id)
        assert ref.id == make_number(x=1).id
        with pytest.warns(UserWarning, match="not compatible"):
            Evaluate(model=ref)  # type: ignore[arg-type]

    def test_pickles(self, model):
        ref = TrainedModel.ref(model.id, instance_id=uuid4())
        parent = Evaluate(model=ref)
        assert pickle.loads(pickle.dumps(ref)) == ref
        assert pickle.loads(pickle.dumps(parent)).id == parent.id

    def test_repr_is_the_ref_call(self, model):
        instance_id = uuid4()
        ref = TrainedModel.ref(model.id, instance_id=instance_id)
        assert repr(ref) == (
            f"TrainedModel.ref('{model.id}', instance_id='{instance_id}')"
        )


class TestNeverStored:
    def test_an_instance_body_with_a_ref_is_refused(self, model):
        parent = Evaluate(model=TrainedModel.ref(model.id))
        with pytest.raises(sd.TaskRefNotHydratedError, match="unhydrated"):
            parent.instance_body()
        with pytest.raises(sd.TaskRefNotHydratedError):
            parent.instance_hash  # noqa: B018
        with pytest.raises(sd.TaskRefNotHydratedError):
            TrainedModel.ref(model.id).instance_body()

    def test_a_modal_payload_with_a_ref_is_refused(self, model):
        from stardag.integration.modal._payload import to_task_payload

        with pytest.raises(sd.TaskRefNotHydratedError):
            to_task_payload(Evaluate(model=TrainedModel.ref(model.id)))

    def test_a_ref_cannot_run(self, model):
        with pytest.raises(sd.TaskRefNotHydratedError, match="cannot run"):
            TrainedModel.ref(model.id).run()

    def test_contains_refs(self, model):
        assert contains_refs(TrainedModel.ref(model.id))
        assert contains_refs(
            Report(evaluation=Evaluate(model=TrainedModel.ref(model.id)))
        )
        assert contains_refs([{"x": TrainedModel.ref(model.id)}])
        assert not contains_refs(Report(evaluation=Evaluate(model=model)))

    def test_a_copy_that_adds_a_ref_is_seen(self, model):
        evaluation = Evaluate(model=model)
        assert not contains_refs(evaluation)
        swapped = evaluation.model_copy(update={"model": TrainedModel.ref(model.id)})
        assert contains_refs(swapped)


class TestHydrate:
    def test_pinned_and_unpinned(self, model):
        registry = InMemoryRegistry()
        _store(registry, model.model_copy(update={"notes": "older"}))
        newest = _store(registry, model)
        pinned = TrainedModel.ref(model.id, instance_id=newest).hydrate(registry)
        assert pinned == model
        assert type(pinned) is TrainedModel
        assert TrainedModel.ref(model.id).hydrate(registry).notes == "first"

    def test_pins_an_older_instance(self, model):
        registry = InMemoryRegistry()
        older = _store(registry, model.model_copy(update={"notes": "older"}))
        _store(registry, model)
        hydrated = TrainedModel.ref(model.id, instance_id=older).hydrate(registry)
        assert hydrated.notes == "older"
        assert hydrated.id == model.id

    def test_uses_the_configured_registry_by_default(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        with sd.registry_provider.override(registry):
            assert TrainedModel.ref(model.id).hydrate() == model

    def test_is_memoized_per_registry(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        reads: list[str] = []
        task_get = registry.task_get

        def counting_task_get(task_id: str):
            reads.append(task_id)
            return task_get(task_id)

        registry.task_get = counting_task_get  # type: ignore[method-assign]
        ref = TrainedModel.ref(model.id)
        first = ref.hydrate(registry)
        assert TrainedModel.ref(model.id).hydrate(registry) is first
        assert reads == [str(model.id)]
        other = InMemoryRegistry()
        _store(other, model)
        assert ref.hydrate(other) is not first

    def test_hydrate_aio(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        assert asyncio.run(TrainedModel.ref(model.id).hydrate_aio(registry)) == model

    def test_a_ref_to_a_base_class_hydrates_the_subclass(self, model):
        class FineTuned(TrainedModel):
            epochs: int = 3

        tuned = FineTuned(data=Dataset(size=2))
        registry = InMemoryRegistry()
        _store(registry, tuned)
        assert type(TrainedModel.ref(tuned.id).hydrate(registry)) is FineTuned

    def test_another_class_is_refused(self, model):
        other = OtherModel(seed=1)
        registry = InMemoryRegistry()
        _store(registry, other)
        with pytest.raises(sd.TaskRefHydrationError, match="not a .*TrainedModel"):
            TrainedModel.ref(other.id).hydrate(registry)

    def test_a_body_with_another_id_is_refused(self, model):
        registry = InMemoryRegistry()
        stale = model.model_copy(update={"learning_rate": 0.9})
        instance_id = _store(registry, stale)
        # Filed under the referenced id, as a moved significant field would.
        registry.instances[instance_id].task_id = str(model.id)
        registry.tasks[str(model.id)] = registry.tasks.pop(str(stale.id))
        registry.tasks[str(model.id)].task_id = str(model.id)
        with pytest.raises(sd.TaskRefHydrationError, match="does not match"):
            TrainedModel.ref(model.id, instance_id=instance_id).hydrate(registry)

    def test_an_unknown_task_is_refused(self, model):
        with pytest.raises(sd.TaskRefHydrationError, match="holds no task"):
            TrainedModel.ref(model.id).hydrate(InMemoryRegistry())

    def test_an_unknown_instance_is_refused(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        with pytest.raises(sd.TaskRefHydrationError, match="holds no instance"):
            TrainedModel.ref(model.id, instance_id=uuid4()).hydrate(registry)

    def test_no_registry_is_refused(self, model):
        with pytest.raises(sd.TaskRefHydrationError, match="no registry"):
            TrainedModel.ref(model.id).hydrate(NoOpRegistry())

    def test_a_nested_class_not_imported_is_named(self, model):
        registry = InMemoryRegistry()
        instance_id = _store(registry, model)
        body = registry.instances[instance_id].body
        body["data"] = {**body["data"], "__namespace": "not.imported", "__name": "Gone"}
        with pytest.raises(sd.TaskRefHydrationError, match="'not.imported'.*'Gone'"):
            TrainedModel.ref(model.id, instance_id=instance_id).hydrate(registry)

    def test_an_alias_payload_is_refused_before_it_is_unpickled(
        self, model, monkeypatch
    ):
        def no_unpickling(_data):
            raise AssertionError("registry bytes were unpickled")

        monkeypatch.setattr("stardag._core.base_task.pickle_loads", no_unpickling)
        registry = InMemoryRegistry()
        for pinned in (False, True):
            instance_id = _store(registry, model)
            body = registry.instances[instance_id].body
            body["data"] = {**body["data"], "__aliased": {"loads_type": "AAAA"}}
            ref = TrainedModel.ref(
                model.id, instance_id=instance_id if pinned else None
            )
            with pytest.raises(sd.TaskRefHydrationError, match="AliasTask"):
                ref.hydrate(registry)


class TestDelegation:
    def test_complete_target_and_load(self, model, default_in_memory_fs_target):
        registry = InMemoryRegistry()
        _store(registry, model)
        ref = TrainedModel.ref(model.id)
        with sd.registry_provider.override(registry):
            assert not ref.complete()
            model.target().save("saved")
            assert ref.complete()
            assert ref.target().uri == model.target().uri
            assert ref.load() == "saved"
            assert asyncio.run(ref.complete_aio())
            assert asyncio.run(ref.load_aio()) == "saved"


class TestResolveRefs:
    def test_without_refs_returns_the_same_objects(self, model):
        roots = [Evaluate(model=model)]
        assert resolve_refs(roots) is roots

    def test_replaces_refs_at_every_depth_and_keeps_ids(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        ref = TrainedModel.ref(model.id)
        report = Report(evaluation=Evaluate(model=ref), extra=[ref], by_name={"m": ref})
        (resolved,) = resolve_refs([report], registry)
        assert resolved.id == report.id
        assert not contains_refs(resolved)
        assert resolved.evaluation.model == model  # type: ignore[attr-defined]
        assert resolved.extra == [model]
        assert resolved.by_name == {"m": model}
        assert resolved.instance_body()["evaluation"]["model"] == model.instance_body()

    def test_a_root_ref_becomes_the_task(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        assert resolve_refs(TrainedModel.ref(model.id), registry) == model


class TestDeprecations:
    def test_alias_task_warns(self, model):
        with pytest.warns(DeprecationWarning, match="Task.ref|MyTask.ref"):
            sd.AliasTask[str](
                aliased=sd.AliasedMetadata(id=model.id, uri="in-memory://x")
            )

    def test_task_ref_from_task_returns_a_task_slug(self, model):
        with pytest.warns(DeprecationWarning, match="TaskSlug"):
            slug = sd.TaskRef.from_task(model)
        assert slug == sd.TaskSlug.from_task(model)
        assert slug.slug.endswith(str(model.id)[:8])
