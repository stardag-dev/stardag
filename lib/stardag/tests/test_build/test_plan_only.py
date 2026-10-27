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
- ``sd.register`` prints a ``ref(...)`` line per root and returns a
  ``RegisteredTask`` per root; ``tags`` / ``task_description`` are refused
  for now.
"""

from __future__ import annotations

import typing
from typing import Annotated, Generic, TypeVar
from uuid import UUID

import pytest

import stardag as sd
from stardag import InstanceConflictError, StardagField, auto_namespace
from stardag.build import BuildExitStatus, build_aio
from stardag.build._registration import new_id
from stardag.exceptions import APIError, PlanOnlyUnsupportedError
from stardag.registry._api_routes import _build_create_req
from stardag.target import InMemoryFileTarget
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

auto_namespace(__name__)

Target = typing.Type[InMemoryFileTarget]


class Tunable(sd.Task[int]):
    key: str
    width: Annotated[int, StardagField(significant=False)] = 1

    def run(self) -> None:
        self._save(self.width)


_T = TypeVar("_T")


class Boxed(sd.Task[int], Generic[_T]):
    value: _T

    def run(self) -> None:
        self._save(0)


@sd.task(name="Doubled")
def double_it(value: int) -> int:
    return 2 * value


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
        text = repr(summary)
        assert "Build REGISTERED" in text and "Registered: 3" in text
        assert "Succeeded" not in text and "Pending" not in text
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

    async def test_skip_blocked_is_a_no_op_on_it(
        self, default_in_memory_fs_target: Target
    ):
        """A plan-only build sharing an upstream another build failed leaves
        its pending root as it is: SKIPPED is a global task status."""
        registry = InMemoryRegistry()
        leaf, _, root = _chain()
        summary = await build_aio([root], registry=registry, plan_only=True)
        assert summary.build_id is not None
        registry.task(str(leaf.id)).status = "failed"
        assert registry.build_skip_blocked(summary.build_id) == []
        assert registry.status_of(root.id) == "pending"

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


class TestBuildCreateAdapter:
    async def test_an_older_subclass_signature_still_creates_ordinary_builds(
        self, default_in_memory_fs_target: Target
    ):
        """A registry double implementing ``build_create`` without
        ``plan_only`` keeps working for ordinary builds: the async adapter
        forwards the keyword only when it is set."""

        class Older(InMemoryRegistry):
            def build_create(  # type: ignore[override]
                self,
                *,
                root_task_ids,
                build_id=None,
                name=None,
                description=None,
                executor_metadata=None,
            ):
                return super().build_create(
                    root_task_ids=root_task_ids,
                    build_id=build_id,
                    name=name,
                    description=description,
                    executor_metadata=executor_metadata,
                )

        info = await Older().build_create_aio(root_task_ids=["t"])
        assert info.status == "running"


class TestCreateRequest:
    def test_plan_only_is_sent_only_when_set(self):
        """An ordinary create stays the request an older registry accepts."""
        ordinary = _build_create_req(["t"], None, None, None, None)
        assert "plan_only" not in (ordinary.json or {})
        plan_only = _build_create_req(["t"], None, None, None, None, True)
        assert (plan_only.json or {})["plan_only"] is True


class TestRegister:
    async def test_returns_and_prints_a_ref_per_root(
        self, default_in_memory_fs_target: Target, capsys
    ):
        registry = InMemoryRegistry()
        leaf, _, root = _chain()
        registered = await sd.register_aio([root, leaf], registry=registry)

        by_task = {i.task_id: i.id for i in registry.instances.values()}
        assert [r.instance_id for r in registered] == [
            by_task[str(root.id)],
            by_task[str(leaf.id)],
        ]
        assert [r.task_id for r in registered] == [str(root.id), str(leaf.id)]
        assert all(isinstance(r.instance_id, UUID) for r in registered)
        assert registered[0].task is root
        expected = (
            f'SyncOnlyTask.ref("{root.id}", instance_id="{by_task[str(root.id)]}")'
        )
        assert registered[0].ref_line == expected
        out = capsys.readouterr().out
        assert f"from {SyncOnlyTask.__module__} import SyncOnlyTask" in out
        assert all(r.ref_line in out for r in registered)
        assert _only_build(registry).status == "registered"
        assert registry.executions == {}

    async def test_a_repeated_root_is_registered_once(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        first, second = await sd.register_aio(
            [root, root], registry=registry, echo=False
        )
        assert first == second
        (created,) = registry.calls_to("build_create")
        assert created["root_task_ids"] == [str(root.id)]

    async def test_differing_constructions_of_one_task_are_refused(
        self, default_in_memory_fs_target: Target
    ):
        """Only identical instances are coalesced: two roots with one task id
        and different non-significant fields are refused, before any build
        exists, rather than both answered with the first's instance."""
        registry = InMemoryRegistry()
        key = f"k-{new_id()}"
        a, b = Tunable(key=key, width=1), Tunable(key=key, width=2)
        assert a.id == b.id and a.instance_hash != b.instance_hash
        with pytest.raises(InstanceConflictError):
            await sd.register_aio([a, b], registry=registry, echo=False)
        assert registry.builds == {}

    async def test_a_non_task_is_refused_before_anything_is_read(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        with pytest.raises(ValueError, match="index 1"):
            await sd.register_aio([root, "not a task"], registry=registry)  # type: ignore[list-item]
        assert registry.builds == {}

    async def test_a_function_task_is_referred_to_by_its_function_name(
        self, default_in_memory_fs_target: Target, capsys
    ):
        """``@sd.task(name="Doubled") def double_it`` is bound as
        ``double_it`` in its module; the class name would not import."""
        registry = InMemoryRegistry()
        task = double_it(value=int(new_id()) % 1000)
        (registered,) = await sd.register_aio(task, registry=registry)
        out = capsys.readouterr().out
        assert f"from {__name__} import double_it" in out
        assert registered.ref_line == (
            f'double_it.ref("{task.id}", instance_id="{registered.instance_id}")'
        )

    def test_the_import_line_says_when_a_class_cannot_be_imported(self):
        """A nested class is imported by its outermost name; a class inside
        a function, or in ``__main__``, gets a note instead of an import
        that would fail."""
        from stardag.build._plan_only import RegisteredTask, _import_path

        def handle(module: str, qualname: str) -> RegisteredTask:
            cls = type("C", (), {"__module__": module, "__qualname__": qualname})
            return RegisteredTask(
                task=cls(),  # type: ignore[arg-type]
                task_id="t",
                instance_id=new_id(),
                ref_line="",
            )

        nested = handle("pkg.mod", "A.Inner")
        assert nested.import_line == "from pkg.mod import A"
        assert _import_path(type(nested.task)) == ("pkg.mod", "A.Inner")
        assert handle("pkg.mod", "f.<locals>.C").import_line.startswith("# ")
        main = handle("__main__", "Model").import_line
        assert main.startswith("# Model is defined in the script being run")
        assert "from __main__" not in main

    def test_a_parametrised_generic_is_referred_to_by_its_origin(self):
        """``Boxed[int]`` is a pydantic subclass named ``Boxed[int]``;
        ``from m import Boxed[int]`` would not parse."""
        from stardag.build._plan_only import _import_path

        assert _import_path(Boxed[int]) == (__name__, "Boxed")

    async def test_a_refused_registered_is_recorded(
        self, default_in_memory_fs_target: Target
    ):
        """``registered`` against a FAILED plan-only build is refused
        ``build_terminal`` and its unapplied event kept, as the server
        commits it with the refusal."""
        registry = InMemoryRegistry()
        _, _, root = _chain()
        build = registry.build_create(root_task_ids=[str(root.id)], plan_only=True)
        registry.build_fail(build.id, "gave up")
        with pytest.raises(APIError) as exc:
            registry.build_registered(build.id)
        assert exc.value.code == "build_terminal"
        refused = [e for e in registry.events if e.type == "BUILD_REGISTERED"]
        assert [e.applied for e in refused] == [False]

    @pytest.mark.parametrize(
        "annotation", [{"tags": ["best"]}, {"task_description": "the baseline"}]
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
        (registered,) = sd.register(root, registry=registry)
        assert str(registered.instance_id) in capsys.readouterr().out
