"""Unit tests for ``stardag.build_config``: the one source of a task's level 2
and 3 parameters, its installation as a context, the structure-config hash
that forms the second half of a scope key, and rebinding a task to the
installed config. Design: ``docs/design/scope-keyed-dependency-structure.md``.

The model-side behaviour (a field being resolved from the config, refused at
init, dropped from payloads) is in ``tests/test_base_model.py``; the code id
and the scope key are in ``tests/test_build/test_scope.py``; the engines
installing the context around a build are in
``tests/test_build/test_build_config_context.py``.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest
from pydantic import WrapSerializer

import stardag as sd
from stardag.base_model import CONTEXT_MODE_KEY, StardagField
from stardag.build_config import (
    BuildConfigError,
    UnknownTaskClassError,
    build_config_scope,
    canonical_structure_config,
    get_build_config,
    rebind_to_build_config,
    resolve_field_value,
    set_build_config,
    structure_config_hash,
    task_config_key,
)
from stardag.target import InMemoryTarget


def _round_in_hash_mode(value, handler, info):
    """A hash-only serializer: the standard way a float is made hash-stable."""
    if info.context and info.context.get(CONTEXT_MODE_KEY) == "hash":
        return round(value, 1)
    return handler(value)


Rounded = Annotated[float, WrapSerializer(_round_in_hash_mode)]


class Fanout(sd.Task[int]):
    __namespace__ = "bc_tests"
    __version__ = "1"

    key: str
    partition_size: Annotated[int, StardagField(significance="dependencies_only")] = 100
    threads: Annotated[int, StardagField(significance="execution_only")] = 1
    # A float that only counts to one decimal in the hash — the standard
    # hash control, here on a level 2 field.
    ratio: Annotated[Rounded, StardagField(significance="dependencies_only")] = 0.5

    def run(self) -> None:
        self.target().save(self.partition_size)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


class Other(sd.Task[int]):
    __namespace__ = "bc_tests"

    key: str
    width: Annotated[int, StardagField(significance="dependencies_only")] = 2

    def run(self) -> None:
        return None


class Plain(sd.Task[int]):
    """No level 2 or 3 fields at all."""

    __namespace__ = "bc_tests"
    key: str

    def run(self) -> None:
        return None


class RootNs(sd.Task[int]):
    """A class in the root namespace: its config key is its bare name."""

    key: str
    width: Annotated[int, StardagField(significance="dependencies_only")] = 3

    def run(self) -> None:
        return None


class Outer(sd.Task[int]):
    """A task whose parameter is a configured task."""

    __namespace__ = "bc_tests"
    inner: Fanout

    def run(self) -> None:
        return None


KEY = "bc_tests.Fanout"
OTHER = "bc_tests.Other"


class TestTaskConfigKey:
    def test_root_namespace_is_the_bare_name(self):
        assert task_config_key("", "T") == "T"

    def test_namespaced_is_dotted(self):
        assert task_config_key("ns", "T") == "ns.T"

    def test_task_classes_produce_the_key_the_config_validates_against(self):
        assert task_config_key(Fanout.get_namespace(), Fanout.get_name()) == KEY
        assert task_config_key(RootNs.get_namespace(), RootNs.get_name()) == "RootNs"
        # ...and the config resolves them under exactly those keys.
        assert canonical_structure_config({KEY: {"partition_size": 7}}) == {
            KEY: {"partition_size": 7}
        }
        assert canonical_structure_config({"RootNs": {"width": 9}}) == {
            "RootNs": {"width": 9}
        }


class TestBuildConfigContext:
    def test_nothing_is_installed_by_default(self):
        assert get_build_config() is None

    def test_scope_installs_and_restores(self):
        with build_config_scope({KEY: {"threads": 2}}):
            assert get_build_config() == {KEY: {"threads": 2}}
        assert get_build_config() is None

    def test_nested_scopes_restore_the_outer(self):
        outer = {KEY: {"threads": 2}}
        inner = {KEY: {"threads": 3}}
        with build_config_scope(outer):
            with build_config_scope(inner):
                assert get_build_config() == inner
            assert get_build_config() == outer
        assert get_build_config() is None

    def test_the_outer_is_restored_when_the_block_raises(self):
        outer = {KEY: {"threads": 2}}
        with build_config_scope(outer):
            with pytest.raises(RuntimeError):
                with build_config_scope({KEY: {"threads": 3}}):
                    raise RuntimeError("boom")
            assert get_build_config() == outer

    def test_set_persists_in_the_current_context(self):
        try:
            set_build_config({KEY: {"threads": 5}})
            assert get_build_config() == {KEY: {"threads": 5}}
            # A scope inside it still restores to what was set, not to None.
            with build_config_scope(None):
                assert get_build_config() is None
            assert get_build_config() == {KEY: {"threads": 5}}
        finally:
            set_build_config(None)

    async def test_concurrent_tasks_each_see_their_own(self):
        async def run(threads: int) -> int:
            set_build_config({KEY: {"threads": threads}})
            await asyncio.sleep(0)
            config = get_build_config()
            assert config is not None
            return config[KEY]["threads"]

        assert await asyncio.gather(run(1), run(2)) == [1, 2]
        # Each asyncio task ran in a copied context; this one is untouched.
        assert get_build_config() is None


class TestResolveFieldValue:
    def test_nothing_without_a_config(self):
        assert resolve_field_value(KEY, "threads") == (False, None)

    def test_nothing_for_an_unknown_class_or_field(self):
        with build_config_scope({KEY: {"threads": 2}}):
            assert resolve_field_value(OTHER, "width") == (False, None)
            assert resolve_field_value(KEY, "partition_size") == (False, None)

    def test_a_present_value_is_found(self):
        with build_config_scope({KEY: {"threads": 2}}):
            assert resolve_field_value(KEY, "threads") == (True, 2)

    @pytest.mark.parametrize("value", [0, "", False, None])
    def test_a_falsy_value_is_still_found(self, value):
        with build_config_scope({KEY: {"threads": value}}):
            assert resolve_field_value(KEY, "threads") == (True, value)


class TestStructureConfigHash:
    def test_empty_and_none_hash_alike(self):
        assert structure_config_hash(None) == structure_config_hash({})

    def test_execution_only_overrides_do_not_change_the_hash(self):
        assert structure_config_hash({KEY: {"threads": 16}}) == structure_config_hash(
            None
        )

    def test_dependencies_only_overrides_change_the_hash(self):
        assert structure_config_hash(
            {KEY: {"partition_size": 250}}
        ) != structure_config_hash(None)

    def test_an_override_equal_to_the_default_is_a_no_op(self):
        assert structure_config_hash(
            {KEY: {"partition_size": 100}}
        ) == structure_config_hash(None)

    def test_the_fields_hash_controls_apply(self):
        """Two ratios that the field's own hash-mode serializer collapses
        produce one scope; hashing the raw JSON would not."""
        assert canonical_structure_config({KEY: {"ratio": 0.76}})[KEY] == {"ratio": 0.8}
        assert structure_config_hash({KEY: {"ratio": 0.76}}) == structure_config_hash(
            {KEY: {"ratio": 0.84}}
        )
        # ...and rounding onto the default makes the override a no-op.
        assert structure_config_hash({KEY: {"ratio": 0.51}}) == structure_config_hash(
            None
        )

    def test_unknown_class_field_and_identity_fields_are_refused(self):
        with pytest.raises(BuildConfigError, match="not registered"):
            structure_config_hash({"bc_tests.Nope": {"x": 1}})
        with pytest.raises(BuildConfigError, match="no such field"):
            structure_config_hash({KEY: {"nope": 1}})
        with pytest.raises(BuildConfigError, match="identity"):
            structure_config_hash({KEY: {"key": "b"}})
        with pytest.raises(BuildConfigError, match="not a valid"):
            structure_config_hash({KEY: {"partition_size": "many"}})

    def test_a_value_is_validated_through_the_field(self):
        """A string for an int field is coerced as the field would coerce it,
        so the hash sees the validated value, not the raw one."""
        assert canonical_structure_config({KEY: {"partition_size": "500"}}) == {
            KEY: {"partition_size": 500}
        }
        assert structure_config_hash(
            {KEY: {"partition_size": "500"}}
        ) == structure_config_hash({KEY: {"partition_size": 500}})

    def test_a_coerced_value_equal_to_the_default_is_dropped(self):
        assert canonical_structure_config({KEY: {"partition_size": "100"}}) == {}

    def test_an_invalid_execution_only_value_is_refused(self):
        """Level 3 never reaches the hash, but a misspelled or mistyped
        override is still a mistake the trigger should hear about."""
        with pytest.raises(BuildConfigError, match="not a valid"):
            canonical_structure_config({KEY: {"threads": "many"}})

    def test_an_unknown_class_is_its_own_error(self):
        with pytest.raises(UnknownTaskClassError, match="bc_tests.Nope") as excinfo:
            canonical_structure_config({"bc_tests.Nope": {"x": 1}})
        assert isinstance(excinfo.value, BuildConfigError)

    def test_the_hash_is_independent_of_insertion_order(self):
        a = {KEY: {"partition_size": 250, "ratio": 0.7}, OTHER: {"width": 4}}
        b = {OTHER: {"width": 4}, KEY: {"ratio": 0.7, "partition_size": 250}}
        assert canonical_structure_config(a) == canonical_structure_config(b)
        assert structure_config_hash(a) == structure_config_hash(b)

    def test_two_classes_both_contribute(self):
        both = structure_config_hash(
            {KEY: {"partition_size": 250}, OTHER: {"width": 4}}
        )
        assert both != structure_config_hash({KEY: {"partition_size": 250}})
        assert both != structure_config_hash({OTHER: {"width": 4}})

    def test_an_empty_per_class_mapping_contributes_nothing(self):
        assert canonical_structure_config({KEY: {}}) == {}
        assert structure_config_hash({KEY: {}}) == structure_config_hash(None)


class TestRebindToBuildConfig:
    def test_re_resolves_from_the_installed_config(self):
        task = Fanout(key="a")
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebound = rebind_to_build_config(task)
        assert isinstance(rebound, Fanout)
        assert rebound.partition_size == 3
        assert rebound.id == task.id

    def test_a_class_without_config_fields_rebinds_to_an_equal_object(self):
        task = Plain(key="p")
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebound = rebind_to_build_config(task)
        assert rebound == task
        assert rebound.id == task.id

    def test_a_nested_task_is_re_resolved_too(self):
        """Only identity data survives the dump, so the nested task's level 2
        value comes from the config installed *here*, not from wherever the
        outer object was built."""
        outer = Outer(inner=Fanout(key="a"))
        assert outer.inner.partition_size == 100
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebound = rebind_to_build_config(outer)
        assert isinstance(rebound, Outer)
        assert rebound.inner.partition_size == 3
        assert rebound.id == outer.id
        assert rebound.inner.id == outer.inner.id
