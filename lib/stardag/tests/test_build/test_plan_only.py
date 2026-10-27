"""Plan-only builds and ``sd.register`` (the ``resident`` test tier).

A plan-only build registers the roots and their whole static closure under
its scope and runs nothing: created PENDING, ended REGISTERED, never
RUNNING, so nothing is ever claimable. Asserted against the in-memory
registry, which follows the server's seams:

- every task is expanded, complete or not (``register_all`` implied), and
  no execution exists afterwards;
- the build is REGISTERED, its frontier hands out nothing, and it refuses
  ``resume`` and the reactive marker;
- it never carries on without the registry (``on_registry_failure`` does
  not apply), and an older registry's 422 is a clear error;
- ``sd.register`` prints a ``ref(...)`` line per root and returns the
  roots' instance ids; ``tags`` / ``description`` are refused for now.
"""

from __future__ import annotations

import typing
from uuid import UUID

import pytest

import stardag as sd
from stardag import auto_namespace
from stardag.build import BuildExitStatus, build_aio
from stardag.build._registration import new_id
from stardag.exceptions import APIError, PlanOnlyUnsupportedError
from stardag.registry._api_routes import _build_create_req
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


def _chain() -> tuple[SyncOnlyTask, SyncOnlyTask, SyncOnlyTask]:
    leaf = SyncOnlyTask(name=f"leaf-{new_id()}")
    mid = SyncOnlyTask(name="mid", deps=(leaf,))
    root = SyncOnlyTask(name="root", deps=(mid,))
    return leaf, mid, root


def _only_build(registry: InMemoryRegistry):
    (build,) = registry.builds.values()
    return build


class TestPlanOnlyBuild:
    async def test_registered_none_started(self, default_in_memory_fs_target: Target):
        registry = InMemoryRegistry()
        leaf, mid, root = _chain()
        summary = await build_aio([root], registry=registry, plan_only=True)

        assert summary.status == BuildExitStatus.REGISTERED
        assert summary.task_count.discovered == 3
        build = _only_build(registry)
        assert (build.id, build.status, build.plan_only) == (
            summary.build_id,
            "registered",
            True,
        )
        (created,) = registry.calls_to("build_create")
        assert created["plan_only"] is True
        assert registry.executions == {}
        assert "member_start" not in registry.methods_called()
        assert not any(t.complete() for t in (leaf, root))
        assert {i.task_id for i in registry.instances.values()} == {
            str(t.id) for t in (leaf, mid, root)
        }
        assert all(i.expanded for i in registry.instances.values())
        frontier = registry.build_get_frontier(build.id)
        assert (frontier.runnable, frontier.discovery_jobs) == ([], [])

    async def test_complete_tasks_are_expanded_too(
        self, default_in_memory_fs_target: Target
    ):
        """The whole static closure, not the walk's usual stop at a complete
        task: a registered model's inputs are recorded with it."""
        leaf, mid, root = _chain()
        await build_aio([root], registry=InMemoryRegistry())  # materialise
        registry = InMemoryRegistry()
        summary = await build_aio([root], registry=registry, plan_only=True)

        assert summary.task_count.previously_completed == 3
        assert all(i.expanded for i in registry.instances.values())
        assert len(registry.instances) == 3
        assert {registry.status_of(t.id) for t in (leaf, mid, root)} == {"completed"}

    async def test_a_plan_only_build_cannot_be_resumed_or_made_reactive(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        summary = await build_aio([root], registry=registry, plan_only=True)
        build_id = summary.build_id
        assert build_id is not None
        for call in (
            lambda: registry.build_resume(build_id),
            lambda: registry.build_set_reactive_meta(build_id, app_name="a"),
        ):
            with pytest.raises(APIError) as exc:
                call()
            assert exc.value.code == "build_plan_only"
        with pytest.raises(ValueError, match="cannot resume"):
            await build_aio(
                [root],
                registry=registry,
                plan_only=True,
                resume_build_id=build_id,
            )

    async def test_needs_a_registry(self, noop_registry, default_in_memory_fs_target):
        _, _, root = _chain()
        with pytest.raises(RuntimeError, match="needs a registry"):
            await build_aio([root], registry=noop_registry, plan_only=True)

    async def test_never_falls_back_to_running(
        self, default_in_memory_fs_target: Target
    ):
        """``on_registry_failure="warn"`` carries an ordinary build through
        an outage; a plan-only build raises, and the build it created is
        failed rather than left looking busy."""

        class Outage(InMemoryRegistry):
            def plan_register_members(self, *args, **kwargs):
                raise APIError("Register members failed", status_code=503)

        registry = Outage()
        _, _, root = _chain()
        with pytest.raises(APIError):
            await build_aio(
                [root], registry=registry, plan_only=True, on_registry_failure="warn"
            )
        assert _only_build(registry).status == "failed"
        assert registry.executions == {}
        assert not root.complete()

    async def test_an_older_registry_is_a_clear_error(
        self, default_in_memory_fs_target: Target
    ):
        class OldRegistry(InMemoryRegistry):
            def build_create(self, *, plan_only: bool = False, **kwargs):
                if plan_only:
                    raise APIError(
                        "Create build failed",
                        status_code=422,
                        detail="[{'type': 'extra_forbidden', 'loc': "
                        "['body', 'plan_only']}]",
                    )
                return super().build_create(**kwargs)

        registry = OldRegistry()
        _, _, root = _chain()
        with pytest.raises(PlanOnlyUnsupportedError, match="upgrade"):
            await build_aio([root], registry=registry, plan_only=True)
        assert registry.builds == {}


class TestCreateRequest:
    def test_plan_only_is_sent_only_when_set(self):
        """An ordinary create stays the request an older registry accepts."""
        ordinary = _build_create_req(["t"], None, None, None, None)
        assert "plan_only" not in (ordinary.json or {})
        plan_only = _build_create_req(["t"], None, None, None, None, True)
        assert (plan_only.json or {})["plan_only"] is True


class TestRegister:
    async def test_prints_a_ref_per_root_and_returns_the_instance_ids(
        self, default_in_memory_fs_target: Target, capsys
    ):
        registry = InMemoryRegistry()
        leaf, _, root = _chain()
        instance_ids = await sd.register_aio([root, leaf], registry=registry)

        by_task = {i.task_id: i.id for i in registry.instances.values()}
        assert instance_ids == [by_task[str(root.id)], by_task[str(leaf.id)]]
        assert all(isinstance(i, UUID) for i in instance_ids)
        out = capsys.readouterr().out
        assert f"from {SyncOnlyTask.__module__} import SyncOnlyTask" in out
        assert f'SyncOnlyTask.ref("{root.id}", instance_id="{instance_ids[0]}")' in out
        assert f'SyncOnlyTask.ref("{leaf.id}", instance_id="{instance_ids[1]}")' in out
        assert _only_build(registry).status == "registered"
        assert registry.executions == {}

    async def test_a_repeated_root_is_registered_once(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        ids = await sd.register_aio([root, root], registry=registry, echo=False)
        assert ids[0] == ids[1]
        (created,) = registry.calls_to("build_create")
        assert created["root_task_ids"] == [str(root.id)]

    @pytest.mark.parametrize(
        "annotation", [{"tags": ["best"]}, {"description": "the baseline"}]
    )
    async def test_annotations_are_not_available_yet(
        self, default_in_memory_fs_target: Target, annotation: dict
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        with pytest.raises(NotImplementedError, match="not available yet"):
            await sd.register_aio([root], registry=registry, **annotation)
        assert registry.builds == {}

    def test_the_sync_wrapper(self, default_in_memory_fs_target: Target, capsys):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        (instance_id,) = sd.register(root, registry=registry)
        assert str(instance_id) in capsys.readouterr().out
