"""Tests for pickle-free task rehydration from registry task_data."""

from __future__ import annotations

import typing

import pytest

import stardag as sd
from stardag import TaskRehydrationError, task_from_registry_data
from stardag.polymorphic import NAME_KEY, NAMESPACE_KEY, PolymorphicRoot
from stardag.target import InMemoryFileTarget

sd.auto_namespace(__name__)


@sd.task
def rehydrate_range(limit: int) -> list[int]:
    return list(range(limit))


@sd.task
def rehydrate_sum(values: sd.Depends[list[int]]) -> int:
    return sum(values)


class RehydrateClassTask(sd.Task[int]):
    factor: int
    values: sd.TaskLoads[list[int]]

    def requires(self):
        return self.values

    def run(self):
        self._save(self.factor * sum(self.values.load()))


class RehydrateOptimizer(PolymorphicRoot):
    """A non-task polymorphic family: its classes register in its own
    registry, not the task registry."""


class RehydrateAdam(RehydrateOptimizer):
    lr: float = 0.1


class RehydrateWithOptimizer(sd.Task[int]):
    optimizer: sd.SubClass[RehydrateOptimizer]
    epochs: int

    def run(self):
        self._save(self.epochs)


class RehydrateDynamicTask(sd.Task[int]):
    limit: int

    def run(self):
        dep = rehydrate_range(limit=self.limit)
        yield dep
        self._save(sum(dep.load()))


class TestRoundTrip:
    def test_decorator_api_with_nested_dep(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        root = rehydrate_sum(values=rehydrate_range(limit=5))
        data = root.model_dump(mode="json")

        back = task_from_registry_data(data, expected_task_id=root.id)

        assert type(back) is type(root)
        assert back.id == root.id
        # nested dep reconstructed too (getattr: decorator-generated task
        # classes expose params dynamically, opaque to the type checker)
        assert getattr(back, "values").id == getattr(root, "values").id

    def test_class_api_taskloads(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        root = RehydrateClassTask(factor=3, values=rehydrate_range(limit=4))
        back = task_from_registry_data(
            root.model_dump(mode="json"), expected_task_id=root.id
        )
        assert isinstance(back, RehydrateClassTask)
        assert back.factor == 3
        assert back.id == root.id

    def test_dynamic_deps_task(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = RehydrateDynamicTask(limit=3)
        back = task_from_registry_data(
            task.model_dump(mode="json"), expected_task_id=task.id
        )
        assert back.id == task.id

    def test_rehydrated_task_is_runnable(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """The reconstructed instance is a fully functional task."""
        dep = rehydrate_range(limit=3)
        dep.run()
        root = rehydrate_sum(values=dep)

        back = task_from_registry_data(root.model_dump(mode="json"))
        back.run()

        assert back.complete()
        assert root.load() == 3  # 0+1+2, via the original instance's target


class TestErrors:
    def test_missing_discriminators(self):
        with pytest.raises(TaskRehydrationError, match="discriminator keys"):
            task_from_registry_data({"limit": 3})

    def test_missing_namespace_only(self):
        """A payload with __name but no __namespace is a partial dump too —
        must raise the missing-discriminators error, not a misleading
        unregistered-class error for the root namespace."""
        with pytest.raises(TaskRehydrationError, match="discriminator keys"):
            task_from_registry_data({NAME_KEY: "SomeTask", "limit": 3})

    def test_unregistered_class_clear_error(self):
        with pytest.raises(TaskRehydrationError, match="module defining the task"):
            task_from_registry_data(
                {
                    NAMESPACE_KEY: "no.such.namespace",
                    NAME_KEY: "NoSuchTask",
                    "version": "",
                }
            )

    def test_id_mismatch_detected(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = rehydrate_range(limit=3)
        other = rehydrate_range(limit=4)
        with pytest.raises(TaskRehydrationError, match="does not match"):
            task_from_registry_data(
                task.model_dump(mode="json"), expected_task_id=other.id
            )

    def test_aliased_payload_rejected(self):
        """AliasTask payloads embed pickled bytes ('loads_type') that the
        resolve path would unpickle — automatic rehydration from
        registry-supplied data must refuse them."""
        with pytest.raises(TaskRehydrationError, match="aliased"):
            task_from_registry_data(
                {
                    NAMESPACE_KEY: "stardag",
                    NAME_KEY: "AliasTask",
                    "version": "",
                    "__aliased": {"loads_type": "cG93bmVk"},
                }
            )

    def test_nested_aliased_payload_rejected(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """Nested task fields resolve through the same path — an __aliased
        dict at any depth must be rejected, not just at the top level."""
        root = rehydrate_sum(values=rehydrate_range(limit=2))
        data = root.model_dump(mode="json")
        data["values"]["__aliased"] = {"loads_type": "cG93bmVk"}
        with pytest.raises(TaskRehydrationError, match="aliased"):
            task_from_registry_data(data)

    def test_plain_task_annotation_rejected_at_definition(self):
        """A nested task field with a plain (non-polymorphic) annotation used to
        only fail at rehydration; it is now rejected at class-construction time,
        so the un-reconstructable payload can never be produced in the first
        place (see NakedPolymorphicFieldError)."""
        from stardag import NakedPolymorphicFieldError

        with pytest.raises(NakedPolymorphicFieldError, match="SubClass"):

            class PlainDepTask(sd.Task[int]):
                deps: tuple[sd.Task[int], ...] = ()

                def run(self):
                    self._save(1)


class TestTaskFromBody:
    """``sd.task_from_body``: the public, documented entry point for
    validating a stored instance body (``task_from_registry_data`` stays as
    the same function under its engine-side name)."""

    def test_round_trips_an_instance_body_in_compat_mode(
        self,
        default_in_memory_fs_target: typing.Type[InMemoryFileTarget],
        caplog: pytest.LogCaptureFixture,
    ):
        root = RehydrateClassTask(factor=2, values=rehydrate_range(limit=3))
        body = root.instance_body()
        body["no_longer_declared"] = "dropped"  # compat: unknown key dropped

        back = sd.task_from_body(body, expected_task_id=root.id)

        assert "no_longer_declared" in caplog.text
        assert isinstance(back, RehydrateClassTask)
        assert back == root

    def test_identity_check(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        body = rehydrate_range(limit=3).instance_body()
        with pytest.raises(TaskRehydrationError, match="does not match"):
            sd.task_from_body(body, expected_task_id=rehydrate_range(limit=4).id)

    def test_unregistered_root_class_names_namespace_and_name(self):
        with pytest.raises(TaskRehydrationError) as info:
            sd.task_from_body({NAMESPACE_KEY: "models", NAME_KEY: "TrainedModel"})
        message = str(info.value)
        assert "namespace='models'" in message and "name='TrainedModel'" in message
        assert "must be imported" in message

    def test_unregistered_nested_class_names_namespace_and_name(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A nested task class whose module is not imported is named, with
        its namespace and what to do, in plain terms: no internal names
        (``task_data``) and no repr quoting."""
        body = RehydrateClassTask(
            factor=1, values=rehydrate_range(limit=1)
        ).instance_body()
        body["values"][NAMESPACE_KEY] = "datasets"
        body["values"][NAME_KEY] = "Dataset"
        with pytest.raises(TaskRehydrationError) as info:
            sd.task_from_body(body)
        message = str(info.value)
        assert message.startswith(
            f"Cannot rehydrate {__name__}.RehydrateClassTask: no task class is "
            "registered for Dataset (namespace datasets)"
        )
        assert "Import the module that defines" in message
        assert "task_data" not in message and "'" not in message

    def test_a_non_task_polymorphic_value_is_not_reported_missing(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        """A ``SubClass`` of another family is not in the task registry; it
        must not be blamed when an unrelated field is what fails."""
        task = RehydrateWithOptimizer(optimizer=RehydrateAdam(), epochs=3)
        body = task.instance_body()
        assert sd.task_from_body(body, expected_task_id=task.id) == task
        body["epochs"] = "not a number"
        with pytest.raises(TaskRehydrationError) as info:
            sd.task_from_body(body)
        message = str(info.value)
        assert "does not validate" in message and "epochs" in message
        assert "RehydrateAdam" not in message

    def test_the_id_mismatch_names_the_common_causes(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        body = rehydrate_range(limit=3).instance_body()
        with pytest.raises(TaskRehydrationError) as info:
            sd.task_from_body(body, expected_task_id=rehydrate_range(limit=4).id)
        message = str(info.value)
        assert "compat_default" in message
        assert "removed or renamed" in message
        assert "https://stardag-dev.github.io/stardag/how-to/evolve-dags/" in message

    def test_the_expected_id_is_compared_as_a_uuid(
        self, default_in_memory_fs_target: typing.Type[InMemoryFileTarget]
    ):
        task = rehydrate_range(limit=3)
        back = sd.task_from_body(
            task.instance_body(), expected_task_id=str(task.id).upper()
        )
        assert back.id == task.id

    def test_refuses_a_partial_dump(self):
        with pytest.raises(TaskRehydrationError, match="discriminator keys"):
            sd.task_from_body({"limit": 3})
