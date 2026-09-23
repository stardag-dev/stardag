"""The instance hash, the instance body, the round-trip stability check, and
instance-conflict detection. Design: docs/design/registry-v2/design.md,
"Two hashes, one flag"."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Annotated, Any
from uuid import UUID

import pytest
from pydantic import PlainSerializer, WrapSerializer

import stardag as sd
from stardag import (
    InstanceConflictError,
    StardagField,
    TaskRehydrationError,
    UnstableSerializationError,
    check_serialization_stability,
    task_from_registry_data,
)
from stardag._core.instance import SeenInstances, body_diff
from stardag._core.task_id import (
    _get_task_id_from_jsonable,
    canonical_body_json,
    instance_hash_of_body,
    task_uuid5_namespace_provider,
)
from stardag.base_model import CONTEXT_MODE_KEY


class Leaf(sd.Task[int]):
    __namespace__ = "instance_tests"

    key: str
    width: Annotated[int, StardagField(significant=False)] = 1
    tags: frozenset[str] = frozenset()

    def run(self) -> None:
        return None


class Root(sd.Task[int]):
    __namespace__ = "instance_tests"

    up: sd.TaskLoads[int]
    note: Annotated[str, StardagField(significant=False)] = ""

    def run(self) -> None:
        return None


class TestInstanceHash:
    def test_is_a_uuid_distinct_from_the_task_id(self):
        leaf = Leaf(key="a")
        assert isinstance(leaf.instance_hash, UUID)
        assert leaf.instance_hash != leaf.id

    def test_covers_non_significant_fields(self):
        a, b = Leaf(key="a", width=1), Leaf(key="a", width=2)
        assert a.id == b.id
        assert a.instance_hash != b.instance_hash

    def test_is_deterministic_across_task_objects(self):
        assert (
            Leaf(key="a", width=3).instance_hash == Leaf(key="a", width=3).instance_hash
        )

    def test_includes_defaults(self):
        body = Leaf(key="a").instance_body()
        assert body["width"] == 1
        assert body["tags"] == []
        assert body["version"] == ""

    def test_is_the_hash_of_the_body_bytes(self):
        """Rule 1: the hash is of the stored body, not of a separate view."""
        leaf = Leaf(key="a", width=5, tags=frozenset({"y", "x"}))
        body = leaf.instance_body()
        assert leaf.instance_hash == instance_hash_of_body(canonical_body_json(body))
        # ...and the body is the registry-mode dump.
        assert body == leaf.model_dump(
            mode="json", context={CONTEXT_MODE_KEY: "registry"}
        )

    def test_the_body_is_a_fresh_copy(self):
        leaf = Leaf(key="a")
        leaf.instance_body()["key"] = "mutated"
        assert leaf.instance_body()["key"] == "a"

    def test_a_nested_task_appears_as_its_full_body(self):
        root = Root(up=Leaf(key="a", width=9))
        assert root.instance_body()["up"] == Leaf(key="a", width=9).instance_body()
        # A nested non-significant change moves the outer instance hash, not
        # the outer task id.
        other = Root(up=Leaf(key="a", width=8))
        assert other.id == root.id
        assert other.instance_hash != root.instance_hash

    def test_canonical_json_is_sorted_compact_utf8(self):
        assert canonical_body_json({"b": 1, "a": {"d": "é", "c": [1, 2]}}) == (
            '{"a":{"c":[1,2],"d":"é"},"b":1}'
        )

    def test_the_two_hashes_use_different_namespaces(self):
        """One jsonable input, two hashes: the namespaces differ, so a task id
        and an instance hash can never coincide."""
        data: dict[str, Any] = {"x": 1}
        assert _get_task_id_from_jsonable(data) != instance_hash_of_body(
            canonical_body_json(data)
        )

    def test_follows_the_task_namespace_override(self):
        leaf = Leaf(key="a")
        default = instance_hash_of_body(leaf._instance_body_json)
        with task_uuid5_namespace_provider.override(
            UUID("00000000-0000-0000-0000-000000000001")
        ):
            assert instance_hash_of_body(leaf._instance_body_json) != default


class TestSetsAreSortedInBothModes:
    """A ``set[str]`` iterates in a per-process order (string hashing is
    randomised), so the body must not depend on it."""

    VALUES = [f"v{i:02d}" for i in range(40)]

    def test_registry_mode_sorts_a_plain_set(self):
        forward = Leaf(key="a", tags=frozenset(self.VALUES))
        backward = Leaf(key="a", tags=frozenset(reversed(self.VALUES)))
        assert forward.instance_body()["tags"] == sorted(self.VALUES)
        assert forward.instance_hash == backward.instance_hash
        assert forward.id == backward.id

    def test_a_set_body_is_stable(self):
        check_serialization_stability(Leaf(key="a", tags=frozenset(self.VALUES)))

    def test_hashable_set_sorts_by_its_own_key_in_registry_mode(self):
        class Numbers(sd.Task[int]):
            __namespace__ = "instance_tests"
            values: sd.HashableSet[int]

            def run(self) -> None:
                return None

        task = Numbers(values=frozenset({10, 9, 100}))
        # Numeric order (the sort key), not the JSON-string order.
        assert task.instance_body()["values"] == [9, 10, 100]

    # 8 and 16 share a slot in a small set's hash table, so which one a
    # set iterates first depends on insertion order: two equal sets with
    # different iteration orders in one process, standing in for the
    # per-process order of a string set.
    FORWARD = frozenset([8, 16])
    BACKWARD = frozenset([16, 8])

    def test_the_collision_pair_iterates_in_two_orders(self):
        assert self.FORWARD == self.BACKWARD
        assert list(self.FORWARD) != list(self.BACKWARD)

    def test_sets_nested_at_any_level_are_sorted(self):
        class Nested(sd.Task[int]):
            __namespace__ = "instance_tests"
            anything: Any = None
            listed: list[frozenset[int]] = []
            keyed: dict[str, frozenset[int]] = {}

            def run(self) -> None:
                return None

        def build(s: frozenset[int]) -> Nested:
            return Nested(
                anything=[s, (s,), {"k": s}, frozenset([s, frozenset([3])])],
                listed=[s],
                keyed={"k": s},
            )

        forward, backward = build(self.FORWARD), build(self.BACKWARD)
        body = forward.instance_body()
        assert body["anything"] == [[8, 16], [[8, 16]], {"k": [8, 16]}, [[3], [8, 16]]]
        assert body["listed"] == [[8, 16]]
        assert body["keyed"] == {"k": [8, 16]}
        assert forward._instance_body_json == backward._instance_body_json
        assert forward.instance_hash == backward.instance_hash
        assert forward.id == backward.id
        check_serialization_stability(forward)

    def test_hashable_set_breaks_sort_key_ties_canonically(self):
        class Tied(sd.Task[int]):
            __namespace__ = "instance_tests"
            values: Annotated[
                frozenset[int], sd.HashSafeSetSerializer(sort_key=lambda _: 0)
            ]

            def run(self) -> None:
                return None

        forward = Tied(values=self.FORWARD)
        backward = Tied(values=self.BACKWARD)
        # The tie-break is the items' canonical JSON: "16" < "8".
        assert forward.instance_body()["values"] == [16, 8]
        assert backward.instance_body()["values"] == [16, 8]
        assert forward.instance_hash == backward.instance_hash
        assert forward.id == backward.id

    def test_a_nested_hashable_set_keeps_its_own_key(self):
        class Keyed(sd.Task[int]):
            __namespace__ = "instance_tests"
            groups: list[sd.HashableSet[int]]

            def run(self) -> None:
                return None

        task = Keyed(groups=[frozenset({10, 9, 100})])
        assert task.instance_body()["groups"] == [[9, 10, 100]]


# --- the stability check ---------------------------------------------------


def _utc_naive(value: datetime) -> str:
    """Converts to UTC and drops the zone: a naive input is read as *local*
    time, so a body written from an aware value moves when re-read."""
    return value.astimezone(timezone.utc).replace(tzinfo=None).isoformat()


class DroppingZone(sd.Task[int]):
    __namespace__ = "instance_tests"
    when: Annotated[datetime, PlainSerializer(_utc_naive)]

    def run(self) -> None:
        return None


def _seconds_outside_hash_mode(value: datetime, handler, info):
    """Drops sub-second precision in the body, keeps it in the task id."""
    if info.context and info.context.get(CONTEXT_MODE_KEY) == "hash":
        return handler(value)
    return value.replace(microsecond=0).isoformat()


class DroppingPrecision(sd.Task[int]):
    __namespace__ = "instance_tests"
    when: Annotated[datetime, WrapSerializer(_seconds_outside_hash_mode)]

    def run(self) -> None:
        return None


class Floaty(sd.Task[int]):
    __namespace__ = "instance_tests"
    x: float
    y: Annotated[float, StardagField(significant=False)] = 0.0

    def run(self) -> None:
        return None


class TestStability:
    def test_an_ordinary_task_is_stable(self):
        check_serialization_stability(Root(up=Leaf(key="a", width=2), note="n"))

    @pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs time.tzset")
    def test_a_naive_vs_aware_serializer_is_caught(self, monkeypatch):
        monkeypatch.setenv("TZ", "Europe/Stockholm")
        time.tzset()
        try:
            task = DroppingZone(when=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
            with pytest.raises(UnstableSerializationError) as excinfo:
                check_serialization_stability(task)
            assert excinfo.value.fields == ("when",)
            assert "DroppingZone" in str(excinfo.value)
        finally:
            monkeypatch.undo()
            time.tzset()

    def test_a_serializer_dropping_precision_the_task_id_depends_on_is_caught(
        self,
    ):
        task = DroppingPrecision(
            when=datetime(2026, 1, 1, 12, 0, 0, 123456, tzinfo=timezone.utc)
        )
        with pytest.raises(
            UnstableSerializationError, match="task id moves"
        ) as excinfo:
            check_serialization_stability(task)
        assert excinfo.value.fields == ("when",)

    def test_the_same_precision_drop_without_a_microsecond_is_stable(self):
        check_serialization_stability(
            DroppingPrecision(when=datetime(2026, 1, 1, 12, tzinfo=timezone.utc))
        )

    def test_negative_zero_is_stable_and_a_distinct_instance(self):
        """Chosen behaviour: ``-0.0`` round-trips exactly, so it passes the
        check — but it is a different body (and, on a significant field, a
        different task id) than ``0.0``. The value is the user's; stardag
        does not normalise it."""
        neg, pos = Floaty(x=-0.0), Floaty(x=0.0)
        check_serialization_stability(neg)
        assert neg.instance_hash != pos.instance_hash
        assert neg.id != pos.id
        # Non-significant: same task id, two instances.
        assert Floaty(x=1.0, y=-0.0).id == Floaty(x=1.0, y=0.0).id
        assert Floaty(x=1.0, y=-0.0).instance_hash != Floaty(x=1.0, y=0.0).instance_hash

    @pytest.mark.parametrize("value", [float("nan"), float("inf")])
    def test_non_finite_floats_are_caught(self, value: float):
        """Chosen behaviour: pydantic's JSON-mode dump writes NaN and
        infinities as ``null`` (they are not JSON), so the body cannot
        validate back into a ``float`` field and the check refuses it, naming
        the field. A non-finite float that reaches the body raw (an ``Any``
        field) is refused by the canonical dump itself."""
        task = Floaty(x=1.0, y=value)
        assert task.instance_body()["y"] is None
        with pytest.raises(UnstableSerializationError) as excinfo:
            check_serialization_stability(task)
        assert excinfo.value.fields == ("y",)

    def test_a_raw_non_finite_float_is_refused_by_the_canonical_dump(self):
        class Loose(sd.Task[int]):
            __namespace__ = "instance_tests"
            payload: Any

            def run(self) -> None:
                return None

        task = Loose(payload={"v": float("nan")})
        if (
            task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})[
                "payload"
            ]["v"]
            is None
        ):
            pytest.skip("this pydantic writes NaN as null for Any fields too")
        with pytest.raises(UnstableSerializationError, match="non-finite") as excinfo:
            _ = task.instance_hash
        assert excinfo.value.fields == ("payload.v",)

    def test_a_numpy_scalar_is_coerced_and_stable(self):
        """A numpy float on a ``float`` field is validated into a Python
        float, so it is stable and equal to the plain value."""
        np = pytest.importorskip("numpy")
        task = Floaty(x=np.float64(1.5))
        check_serialization_stability(task)
        assert task.instance_hash == Floaty(x=1.5).instance_hash

    def test_the_error_names_nested_fields(self):
        class Outer(sd.Task[int]):
            __namespace__ = "instance_tests"
            inner: sd.TaskLoads[int]

            def run(self) -> None:
                return None

        task = Outer(
            inner=DroppingPrecision(
                when=datetime(2026, 1, 1, 12, 0, 0, 5, tzinfo=timezone.utc)
            )
        )
        with pytest.raises(UnstableSerializationError) as excinfo:
            check_serialization_stability(task)
        assert excinfo.value.fields == ("inner",)


# --- conflicts within one discovery pass ----------------------------------


class TestSeenInstances:
    def test_the_first_construction_is_new_and_a_repeat_is_not(self):
        seen = SeenInstances()
        assert seen.observe(Leaf(key="a")) is True
        assert seen.observe(Leaf(key="a")) is False
        assert Leaf(key="a").id in seen
        assert len(seen) == 1

    def test_different_tasks_do_not_conflict(self):
        seen = SeenInstances()
        assert seen.observe(Leaf(key="a"))
        assert seen.observe(Leaf(key="b"))
        assert len(seen) == 2

    def test_two_instances_of_one_task_id_conflict(self):
        seen = SeenInstances()
        seen.observe(Leaf(key="a", width=1), path="Root -> Leaf")
        with pytest.raises(InstanceConflictError) as excinfo:
            seen.observe(Leaf(key="a", width=2), path="Other -> Leaf")
        error = excinfo.value
        assert error.fields == ("width",)
        assert error.task_id == str(Leaf(key="a").id)
        assert error.paths == ("Root -> Leaf", "Other -> Leaf")
        assert "Root -> Leaf" in str(error) and "Other -> Leaf" in str(error)

    def test_a_nested_difference_is_named_by_its_path(self):
        seen = SeenInstances()
        seen.observe(Root(up=Leaf(key="a", width=1)))
        with pytest.raises(InstanceConflictError) as excinfo:
            seen.observe(Root(up=Leaf(key="a", width=2)))
        assert excinfo.value.fields == ("up.width",)
        assert excinfo.value.paths == (None, None)


class TestBodyDiff:
    def test_paths(self):
        assert body_diff(
            {"a": 1, "b": {"c": [1, 2]}}, {"a": 1, "b": {"c": [1, 3]}}
        ) == ["b.c[1]"]
        assert body_diff({"a": 1}, {"b": 1}) == ["a", "b"]
        assert body_diff([1], [1, 2]) == ["<root>"]
        assert body_diff({"a": 1}, {"a": 1.0}) == ["a"]
        assert body_diff({"a": 1}, {"a": 1}) == []


# --- rehydration: strict for significant, lenient for the rest -------------


class TestRehydration:
    def test_a_body_round_trips(self):
        root = Root(up=Leaf(key="a", width=4), note="n")
        rebuilt = task_from_registry_data(
            root.instance_body(), expected_task_id=root.id
        )
        assert rebuilt == root
        assert rebuilt.instance_hash == root.instance_hash

    def test_an_unknown_key_is_dropped_with_a_warning(self, caplog):
        body = Leaf(key="a").instance_body()
        body["removed_knob"] = 3
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = task_from_registry_data(body, expected_task_id=Leaf(key="a").id)
        assert rebuilt == Leaf(key="a")
        assert "removed_knob" in caplog.text

    def test_an_unknown_nested_key_is_dropped_too(self, caplog):
        body = Root(up=Leaf(key="a")).instance_body()
        body["up"]["removed_knob"] = 3
        with caplog.at_level(logging.WARNING, logger="stardag.base_model"):
            rebuilt = task_from_registry_data(body)
        assert rebuilt == Root(up=Leaf(key="a"))
        assert "removed_knob" in caplog.text

    def test_a_missing_non_significant_field_takes_the_class_default(self):
        body = Leaf(key="a", width=7).instance_body()
        del body["width"]
        rebuilt = task_from_registry_data(body, expected_task_id=Leaf(key="a").id)
        assert isinstance(rebuilt, Leaf)
        assert rebuilt.width == 1

    def test_a_changed_significant_field_fails_the_task_id_check(self):
        body = Leaf(key="a").instance_body()
        body["key"] = "b"
        with pytest.raises(TaskRehydrationError, match="does not match"):
            task_from_registry_data(body, expected_task_id=Leaf(key="a").id)

    def test_aliased_bodies_are_still_refused(self):
        body = json.loads(json.dumps(Leaf(key="a").instance_body()))
        body["__aliased"] = {"id": str(Leaf(key="a").id), "uri": "x", "loads_type": ""}
        with pytest.raises(TaskRehydrationError, match="__aliased"):
            task_from_registry_data(body)
