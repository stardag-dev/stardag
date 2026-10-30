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

import httpx
import pytest

import stardag as sd
from stardag._core.task_ref import (
    TaskRef,
    clear_hydration_memo,
    contains_refs,
    reference_call,
    reference_line,
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


class Outer:
    class NestedModel(sd.Task[str]):
        def run(self):
            self._save("nested")


@sd.task(name="RefLineRenamedTask")
def bound_under_another_name(x: int) -> int:
    return x


_DEFAULT_MODEL = TrainedModel(data=Dataset(size=9))


class WithRefDefault(sd.Task[str]):
    size: int
    extra: Annotated[sd.TaskLoads[str], sd.StardagField(significant=False)] = (
        TrainedModel.ref(_DEFAULT_MODEL.id)
    )

    def run(self):
        self._save("x")


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
    """Seed ``task`` as one instance (no build). Returns the instance id."""
    return registry.add_instance(
        body=task.instance_body(), task_id=str(task.id), instance_id=uuid4()
    ).id


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
    def test_alias_task_warns_once_at_the_callers_line(self, model, monkeypatch):
        from stardag._core import alias_task

        monkeypatch.setattr(alias_task, "_DEPRECATION_WARNED", False)
        aliased = sd.AliasedMetadata(
            id=model.id, uri="in-memory://x", body=model.instance_body()
        )
        with pytest.warns(DeprecationWarning, match="MyTask.ref") as record:
            alias = sd.AliasTask[str](aliased=aliased)
            sd.AliasTask[str](aliased=aliased)
        assert len(record) == 1
        assert record[0].filename == __file__
        # Validating a stored body (what a worker does) never warns.
        monkeypatch.setattr(alias_task, "_DEPRECATION_WARNED", False)
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            from pydantic import TypeAdapter

            rebuilt = TypeAdapter(sd.SubClass[sd.BaseTask]).validate_python(
                alias.model_dump()
            )
            assert rebuilt.id == alias.id

    def test_task_ref_from_task_returns_a_task_slug(self, model):
        with pytest.warns(DeprecationWarning, match="TaskSlug"):
            slug = sd.TaskRef.from_task(model)
        assert slug == sd.TaskSlug.from_task(model)
        assert slug.slug.endswith(str(model.id)[:8])


class TestReferenceLine:
    def test_a_module_level_class(self):
        assert reference_line(TrainedModel) == (
            f"from {__name__} import TrainedModel",
            "TrainedModel",
        )

    def test_a_nested_class_through_its_outermost(self):
        assert reference_line(Outer.NestedModel) == (
            f"from {__name__} import Outer",
            "Outer.NestedModel",
        )

    def test_a_renamed_function_task_by_its_bound_name(self):
        assert reference_line(bound_under_another_name) == (
            f"from {__name__} import bound_under_another_name",
            "bound_under_another_name",
        )

    def test_no_import_for_main_or_local_classes(self, monkeypatch):
        class LocalModel(sd.Task[str], namespace_override="ref-line-local"):
            def run(self):
                self._save("local")

        assert reference_line(LocalModel) == (None, "LocalModel")
        monkeypatch.setattr(LocalModel, "__module__", "__main__")
        assert reference_line(LocalModel)[0] is None


class TestErrorsAndHints:
    def test_a_plain_dump_of_a_parent_holding_a_ref_is_refused(self, model):
        from pydantic_core import PydanticSerializationError

        parent = Evaluate(model=TrainedModel.ref(model.id))
        for dump in (parent.model_dump, parent.model_dump_json):
            with pytest.raises(PydanticSerializationError, match="unhydrated"):
                dump()
        with pytest.raises(PydanticSerializationError, match="resolve_refs"):
            TrainedModel.ref(model.id).model_dump()

    def test_reading_a_parameter_says_to_hydrate(self, model):
        with pytest.raises(AttributeError, match=r"call \.hydrate\(\)"):
            TrainedModel.ref(model.id).learning_rate  # type: ignore[attr-defined]

    def test_the_former_task_ref_constructor_points_to_task_slug(self, model):
        with pytest.raises(TypeError, match="stardag.TaskSlug"):
            sd.TaskRef(name="X", version="1", id=model.id)  # type: ignore[call-arg]

    def test_a_concrete_class_field_hints_at_task_loads(self, model):
        from pydantic import ValidationError

        class StrictParent(sd.Task[str]):
            model: TrainedModel

            def run(self):
                self._save("x")

        with pytest.raises(ValidationError, match=r"sd\.TaskLoads\[\.\.\.\]"):
            StrictParent(model=TrainedModel.ref(model.id))  # type: ignore[arg-type]
        assert StrictParent(model=model).model == model

    def test_an_unrelated_failure_is_not_hinted_and_keeps_its_errors(self, model):
        """A valid reference in a TaskLoads field and an invalid other field:
        the original ValidationError, field errors intact, no hint."""
        from pydantic import ValidationError

        class Loose(sd.Task[str]):
            model: sd.TaskLoads[str]
            count: int = 0

            def run(self):
                self._save("x")

        with pytest.raises(ValidationError) as e:
            Loose(model=TrainedModel.ref(model.id), count="many")  # type: ignore[arg-type]
        assert "TaskLoads" not in str(e.value)
        assert [err["loc"] for err in e.value.errors()] == [("count",)]

    def test_refs_in_a_deque_are_detected_and_resolved_keeping_maxlen(self, model):
        import collections

        registry = InMemoryRegistry()
        iid = uuid4()
        registry.add_instance(
            body=model.instance_body(), task_id=model.id, instance_id=iid
        )
        held = collections.deque(
            [TrainedModel.ref(model.id, instance_id=iid)], maxlen=3
        )
        assert contains_refs(held)
        resolved = resolve_refs(held, registry)
        assert isinstance(resolved, collections.deque) and resolved.maxlen == 3
        assert list(resolved) == [model]

    def test_hydration_errors_are_rehydration_errors(self):
        assert issubclass(sd.TaskRefHydrationError, sd.TaskRehydrationError)


class _Offline(InMemoryRegistry):
    def task_get(self, task_id):
        raise httpx.ConnectError("connection refused")

    def task_instance_get(self, instance_id, *, task_id=None):
        raise httpx.ConnectError("connection refused")


class TestOffline:
    def test_unpinned_says_pin_it(self, model):
        with pytest.raises(sd.TaskRefHydrationError, match="pin it with instance_id="):
            TrainedModel.ref(model.id).hydrate(_Offline())

    def test_pinned_not_cached_says_one_online_read(self, model):
        with pytest.raises(sd.TaskRefHydrationError, match="one online read"):
            TrainedModel.ref(model.id, instance_id=uuid4()).hydrate(_Offline())

    def test_pinned_with_the_cache_off_says_so(self, model):
        from stardag.config import config_provider

        config = config_provider.get().model_copy(update={"registry_cache": False})
        with config_provider.override(config):
            with pytest.raises(sd.TaskRefHydrationError, match="cache is turned off"):
                TrainedModel.ref(model.id, instance_id=uuid4()).hydrate(_Offline())


class TestReviewRoundTwo:
    def test_concurrent_hydration_reads_once_and_shares_the_task(self, model):
        import threading
        import time

        registry = InMemoryRegistry()
        _store(registry, model)
        reads: list[str] = []
        task_get = registry.task_get

        def slow_task_get(task_id: str):
            reads.append(task_id)
            time.sleep(0.05)
            return task_get(task_id)

        registry.task_get = slow_task_get  # type: ignore[method-assign]
        results: list[object] = []
        threads = [
            threading.Thread(
                target=lambda: results.append(
                    TrainedModel.ref(model.id).hydrate(registry)
                )
            )
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert reads == [str(model.id)]
        assert len({id(r) for r in results}) == 1

    def test_an_outage_is_a_registry_unavailable_error(self, model):
        from stardag._core.task_ref import TaskRefRegistryUnavailableError
        from stardag.exceptions import APIError

        class Gateway(InMemoryRegistry):
            def task_get(self, task_id):
                raise APIError("bad gateway", status_code=503)

        for registry in (_Offline(), Gateway()):
            with pytest.raises(TaskRefRegistryUnavailableError):
                TrainedModel.ref(model.id).hydrate(registry)

    def test_an_unpinned_body_that_does_not_validate_is_a_rehydration_error(
        self, model
    ):
        registry = InMemoryRegistry()
        instance_id = _store(registry, model)
        del registry.instances[instance_id].body["data"]
        with pytest.raises(sd.TaskRefHydrationError, match="Failed to rehydrate"):
            TrainedModel.ref(model.id).hydrate(registry)
        with pytest.raises(sd.TaskRehydrationError):
            TrainedModel.from_registry(model.id, registry)

    def test_a_ref_equal_to_a_task_compat_default_drops_out_of_the_hash(self):
        baseline = Dataset(size=1)

        class WithDefaultData(sd.Task[str]):
            data: Annotated[
                sd.TaskLoads[list[int]], sd.StardagField(compat_default=baseline)
            ] = baseline

            def run(self):
                self._save("x")

        real = WithDefaultData(data=baseline)
        via_ref = WithDefaultData(data=Dataset.ref(baseline.id))
        assert via_ref.id == real.id == WithDefaultData().id

    def test_refs_in_dataclasses_and_other_mappings_are_resolved(self, model):
        import dataclasses
        from collections import OrderedDict

        @dataclasses.dataclass(frozen=True)
        class Holder:
            model: object

        registry = InMemoryRegistry()
        _store(registry, model)
        ref = TrainedModel.ref(model.id)
        holder, ordered = resolve_refs(
            [Holder(model=ref), OrderedDict(m=ref)], registry
        )
        assert type(holder) is Holder and holder.model == model
        assert type(ordered) is OrderedDict and ordered["m"] == model
        assert contains_refs(Holder(model=ref))

    def test_a_hydrated_task_holding_a_ref_is_resolved_too(self, model):
        registry = InMemoryRegistry()
        _store(registry, _DEFAULT_MODEL)
        stored = WithRefDefault(size=1, extra=_DEFAULT_MODEL)
        instance_id = _store(registry, stored)
        # The stored body predates ``extra``: hydration takes its default,
        # a reference, which must be hydrated in turn.
        del registry.instances[instance_id].body["extra"]
        resolved = resolve_refs(WithRefDefault.ref(stored.id), registry)
        assert type(resolved) is WithRefDefault
        assert resolved.extra == _DEFAULT_MODEL
        assert not contains_refs(resolved)

    def test_a_set_whose_refs_collapse_is_refused(self, model):
        registry = InMemoryRegistry()
        instance_id = _store(registry, model)
        both = {
            TrainedModel.ref(model.id),
            TrainedModel.ref(model.id, instance_id),
        }
        assert len(both) == 2
        with pytest.raises(sd.TaskRefHydrationError, match="set"):
            resolve_refs(both, registry)


class CustomLoad(sd.LoadableTask[int]):
    """A ``LoadableTask`` with its own load, not a ``Task``."""

    value: int

    def complete(self) -> bool:
        return True

    def load(self) -> int:
        return self.value

    def run(self) -> None:
        pass


class PlainBase(sd.BaseTask):
    """A plain ``BaseTask``: no declared loaded type."""

    label: str

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class NeedsInt(sd.Task[float]):
    number: sd.TaskLoads[int]

    def run(self):
        self._save(float(self.number.load()))


class AnyUpstream(sd.Task[str]):
    upstream: sd.SubClass[sd.BaseTask]

    def run(self):
        self._save("x")


class TestEveryTaskClassHasRef:
    def test_a_loadable_task_ref_keys_hydrates_and_loads(self):
        real = CustomLoad(value=7)
        ref = CustomLoad.ref(real.id)
        assert type(ref).__name__ == "TaskRef[CustomLoad, int]"
        assert NeedsInt(number=ref).id == NeedsInt(number=real).id
        registry = InMemoryRegistry()
        _store(registry, real)
        assert ref.hydrate(registry) == real
        with sd.registry_provider.override(registry):
            assert ref.load() == 7

    def test_a_plain_base_task_ref_fits_a_subclass_field(self):
        real = PlainBase(label="p")
        ref = PlainBase.ref(real.id)
        assert AnyUpstream(upstream=ref).id == AnyUpstream(upstream=real).id
        registry = InMemoryRegistry()
        _store(registry, real)
        (resolved,) = resolve_refs([AnyUpstream(upstream=ref)], registry)
        assert resolved.upstream == real

    def test_reference_line_for_every_kind_of_class(self):
        for cls in (CustomLoad, PlainBase, TrainedModel):
            assert reference_line(cls) == (
                f"from {__name__} import {cls.__name__}",
                cls.__name__,
            )
            assert hasattr(cls, "ref")


class TestRoundThree:
    def test_model_copy_with_an_update_is_refused(self, model):
        ref = TrainedModel.ref(model.id)
        with pytest.raises(
            sd.TaskRefNotHydratedError, match=r"hydrate\(\)\.model_copy"
        ):
            ref.model_copy(update={"learning_rate": 0.9})
        copied = ref.model_copy()
        assert copied == ref and copied.id == model.id
        assert type(copied) is type(ref)

    def test_get_name_and_namespace_answer_for_the_referenced_class(self, model):
        ref = TrainedModel.ref(model.id)
        assert ref.get_name() == TrainedModel.get_name() == "TrainedModel"
        assert ref.get_namespace() == TrainedModel.get_namespace()
        assert PlainBase.ref(model.id).get_name() == "PlainBase"
        with pytest.raises(TypeError, match="call it on a reference"):
            TaskRef.get_name()


class TestRoundFour:
    def test_a_resolution_that_would_change_an_id_is_refused(self, model):
        from typing import Annotated as A

        class DefaultList(sd.Task[str]):
            models: A[
                list[sd.TaskLoads[str]], sd.StardagField(compat_default=[model])
            ] = [model]

            def run(self):
                self._save("x")

        registry = InMemoryRegistry()
        _store(registry, model)
        holder = DefaultList(models=[TrainedModel.ref(model.id)])
        with pytest.raises(sd.TaskRefHydrationError, match="change its task id"):
            resolve_refs(holder, registry)

    def test_an_explicit_hydration_binds_the_reference(self, model):
        registry = InMemoryRegistry()
        _store(registry, model)
        ref = TrainedModel.ref(model.id)
        ref.hydrate(registry)
        # No configured registry: the reference reads the one it was
        # hydrated from.
        assert ref.hydrate() == model
        assert TrainedModel.ref(model.id) == ref
        with pytest.raises(sd.TaskRefHydrationError, match="no registry"):
            TrainedModel.ref(model.id).hydrate()

    def test_a_hydration_outage_is_a_registry_outage(self):
        from stardag._core.task_ref import TaskRefRegistryUnavailableError
        from stardag.build._reactive._frontier_actions import is_registry_outage

        assert is_registry_outage(TaskRefRegistryUnavailableError("down"))
        assert not is_registry_outage(sd.TaskRefHydrationError("no such task"))


class TestFunctionFormAndSeeding:
    def test_task_ref_is_the_method_as_a_function(self, model):
        assert sd.task_ref(TrainedModel, model.id) == TrainedModel.ref(model.id)
        assert type(sd.task_ref(CustomLoad, model.id)) is type(CustomLoad.ref(model.id))
        with pytest.raises(TypeError, match="needs a task class"):
            sd.task_ref(int, model.id)  # type: ignore[call-overload]

    def test_task_ref_works_when_a_field_shadows_ref(self):
        with pytest.warns(UserWarning, match="shadows an attribute"):

            class Checkout(sd.Task[int]):
                ref: str = "main"  # pyright: ignore[reportIncompatibleMethodOverride]

                def run(self):
                    self._save(1)

        real = Checkout(ref="v1")
        assert real.ref == "v1"
        reference = sd.task_ref(Checkout, real.id)
        assert NeedsInt(number=reference).id == NeedsInt(number=real).id
        assert repr(reference).startswith("sd.task_ref(Checkout, ")
        registry = InMemoryRegistry()
        _store(registry, real)
        assert reference.hydrate(registry) == real
        assert reference_call("Checkout", Checkout, "t", "i") == (
            'sd.task_ref(Checkout, "t", instance_id="i")'
        )

    def test_add_instance_seeds_pinned_and_newest_reads(self, model):
        registry = InMemoryRegistry()
        older_id, newest_id = uuid4(), uuid4()
        older = registry.add_instance(
            body=model.model_copy(update={"notes": "older"}).instance_body(),
            task_id=str(model.id),
            instance_id=older_id,
        )
        registry.add_instance(
            body=model.instance_body(), task_id=str(model.id), instance_id=newest_id
        )
        assert older.id == older_id and older.task_id == str(model.id)
        pinned = TrainedModel.from_registry(model.id, registry, instance_id=older_id)
        assert pinned.instance_body()["notes"] == "older"
        # Unpinned: the most recently seeded instance, as task_get serves it.
        newest_task = TrainedModel.from_registry(model.id, registry)
        assert newest_task.instance_body()["notes"] == "first"
        assert registry.task_get(str(model.id)).instances[0].id == newest_id
        assert TrainedModel.ref(model.id).hydrate(registry).notes == "first"
        assert registry.task_get(str(model.id)).task_name == "TrainedModel"

    def test_add_instance_is_idempotent_and_refuses_a_different_body(self, model):
        registry = InMemoryRegistry()
        instance_id = uuid4()
        kwargs = dict(task_id=str(model.id), instance_id=instance_id)
        first = registry.add_instance(body=model.instance_body(), **kwargs)
        assert registry.add_instance(body=model.instance_body(), **kwargs) == first
        assert len(registry.task_get(str(model.id)).instances) == 1
        other_body = model.model_copy(update={"notes": "else"}).instance_body()
        with pytest.raises(ValueError, match="different body"):
            registry.add_instance(body=other_body, **kwargs)
        with pytest.raises(ValueError, match="different task id"):
            registry.add_instance(
                body=model.instance_body(),
                task_id=str(uuid4()),
                instance_id=instance_id,
            )

    def test_add_instance_needs_no_class_and_does_not_validate(self, model):
        """Seeding stores the body as is; hydration does the checking."""
        registry = InMemoryRegistry()
        unknown = {"__namespace": "not.imported", "__name": "Gone", "x": 1}
        seeded = registry.add_instance(
            body=unknown, task_id=str(model.id), instance_id=uuid4()
        )
        assert seeded.body == unknown
        stale = model.model_copy(update={"learning_rate": 0.9})
        mismatch = registry.add_instance(
            body=stale.instance_body(), task_id=str(model.id), instance_id=uuid4()
        )
        with pytest.raises(sd.TaskRefHydrationError, match="does not match"):
            TrainedModel.ref(model.id, mismatch.id).hydrate(registry)

    def test_the_recorded_bodies_pattern(self, model):
        """A ref pinned in code, its body recorded next to it, seeded in a
        test under the same ids: hydration needs no real registry."""
        from dataclasses import dataclass

        from stardag.testing import test_harness

        @dataclass
        class Recorded:
            task_id: str
            instance_id: str
            body: dict

        recorded_bodies = [
            Recorded(str(model.id), str(uuid4()), model.instance_body()),
            Recorded(str(model.data.id), str(uuid4()), model.data.instance_body()),
        ]
        task_id, instance_id = (
            recorded_bodies[0].task_id,
            recorded_bodies[0].instance_id,
        )

        reg = InMemoryRegistry()
        for rec in recorded_bodies:  # {task_id, instance_id, body}
            reg.add_instance(
                body=rec.body, task_id=rec.task_id, instance_id=rec.instance_id
            )
        with test_harness(registry=reg):
            assert TrainedModel.ref(
                task_id, instance_id=instance_id
            ).hydrate().id == UUID(task_id)

    def test_a_seeded_task_can_then_be_registered_by_a_build(self, model):
        """The seeded row has no output URI; a registration fills it in
        rather than refusing it as an identity conflict."""
        import asyncio

        from stardag.build._registration import register_plan_aio, walk_aio

        registry = InMemoryRegistry()
        _store(registry, model.data)
        deployment = registry.add_deployment(kind="local", code_id="c")
        build_id = registry.build_create(root_task_ids=[str(model.data.id)]).id
        with sd.registry_provider.override(registry):
            walk = asyncio.run(walk_aio([model.data], registry=registry))
        asyncio.run(
            register_plan_aio(
                registry, build_id, walk, deployment_id=deployment, settings={}
            )
        )
        assert registry.tasks[str(model.data.id)].output_uri == (
            model.data.target().uri
        )


def test_deepcopy_of_a_bound_reference_copies_only_its_identity(model):
    import copy
    import threading

    class Locked(InMemoryRegistry):
        def __init__(self) -> None:
            super().__init__()
            self.lock = threading.Lock()  # what an authenticated client holds

    registry = Locked()
    _store(registry, model)
    ref = TrainedModel.ref(model.id)
    ref.hydrate(registry)
    copied = copy.deepcopy(ref)
    assert copied == ref and copied is not ref
    parent = Evaluate(model=ref)
    assert parent.model_copy(deep=True).id == parent.id
