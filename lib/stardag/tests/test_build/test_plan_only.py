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
- ``sd.register`` hydrates references first, prints a ``ref(...)`` line
  per root and returns a pinned ``TaskRef`` per root; ``tags`` /
  ``task_description`` annotate every root.
"""

from __future__ import annotations

import typing
from typing import Annotated, Generic, TypeVar

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

    async def test_a_closure_conflict_at_registered_fails_the_build(
        self, default_in_memory_fs_target: Target
    ):
        """Another plan expands a shared instance after the seal, reaching a
        second instance of a task this plan holds: the closure step at
        ``registered`` fails the build (FAILED, with its ``BUILD_FAILED``)
        and refuses ``instance_conflict``, as the server commits it."""
        from stardag.testing._registry_state import InstanceRow

        leaf, _, root = _chain()

        class Racing(InMemoryRegistry):
            def build_registered(self, build_id):
                plan = self.active_plan(build_id)
                assert plan is not None
                other = InstanceRow(
                    id=new_id(),
                    deployment_id=plan.deployment_id,
                    settings_hash=plan.settings_hash,
                    instance_hash="another-construction",
                    task_id=str(leaf.id),
                    body={"another": "construction"},
                    expanded=True,
                )
                self.instances[other.id] = other
                held = self.members[plan.id][str(root.id)].instance_id
                self.instances[held].upstreams[other.id] = False
                return super().build_registered(build_id)

        registry = Racing()
        with pytest.raises(APIError) as exc:
            await build_aio([root], registry=registry, plan_only=True)
        assert exc.value.code == "instance_conflict"
        build = _only_build(registry)
        assert build.status == "failed"
        assert "instance conflict" in (build.error_message or "")
        failed = [e for e in registry.events if e.type == "BUILD_FAILED"]
        assert [e.applied for e in failed] == [True]
        assert registry.executions == {}

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


class Weights(sd.Task[str]):
    key: str

    def run(self) -> None:
        self._save(f"weights-{self.key}")


class Score(sd.Task[float]):
    model: sd.TaskLoads[str]

    def run(self) -> None:
        self._save(float(len(self.model.load())))


class ScoresRegistered(sd.Task[float]):
    """Reaches its upstream through a reference built in ``requires()``."""

    weights_id: str
    weights_instance: str

    def requires(self):  # type: ignore[override]
        return Weights.ref(self.weights_id, instance_id=self.weights_instance)

    def run(self) -> None:
        self._save(0.0)


class TestRegister:
    async def test_returns_a_pinned_ref_per_root_and_prints_its_line(
        self, default_in_memory_fs_target: Target, capsys
    ):
        registry = InMemoryRegistry()
        leaf, _, root = _chain()
        refs = await sd.register_aio([root, leaf], registry=registry)

        by_task = {i.task_id: i.id for i in registry.instances.values()}
        assert all(isinstance(r, sd.TaskRef) for r in refs)
        assert [(r.task_id, r.instance_id) for r in refs] == [
            (root.id, by_task[str(root.id)]),
            (leaf.id, by_task[str(leaf.id)]),
        ]
        assert refs[0].task_class is SyncOnlyTask
        assert refs[0].hydrate(registry) == root
        out = capsys.readouterr().out
        assert f"from {SyncOnlyTask.__module__} import SyncOnlyTask" in out
        for r in refs:
            assert (
                f'SyncOnlyTask.ref("{r.task_id}", instance_id="{r.instance_id}")' in out
            )
        assert _only_build(registry).status == "registered"
        assert registry.executions == {}

    async def test_a_root_holding_a_ref_is_hydrated_first(
        self, default_in_memory_fs_target: Target
    ):
        """``sd.register(Score(model=Weights.ref(...)))``: the reference is
        hydrated before anything is read, so the root registers with the
        real upstream in its body, as a build would."""
        registry = InMemoryRegistry()
        weights = Weights(key=f"w-{new_id()}")
        (weights_ref,) = await sd.register_aio(weights, registry=registry, echo=False)

        score = Score(model=weights_ref)
        (score_ref,) = await sd.register_aio(score, registry=registry, echo=False)
        assert score_ref.task_id == score.id == Score(model=weights).id
        hydrated = score_ref.hydrate(registry)
        assert hydrated == Score(model=weights)
        assert len(registry.builds) == 2

    async def test_a_ref_from_requires_hydrates_from_the_given_registry(
        self, default_in_memory_fs_target: Target
    ):
        """A reference ``requires()`` builds is hydrated from the registry
        passed to ``sd.register``, not the configured default (which the
        test suite keeps off): the walk receives it."""
        registry = InMemoryRegistry()
        weights = Weights(key=f"w-{new_id()}")
        (weights_ref,) = await sd.register_aio(weights, registry=registry, echo=False)
        root = ScoresRegistered(
            weights_id=str(weights_ref.task_id),
            weights_instance=str(weights_ref.instance_id),
        )
        (root_ref,) = await sd.register_aio(root, registry=registry, echo=False)
        assert root_ref.task_id == root.id
        (plan_only,) = [
            b for b in registry.builds.values() if str(root.id) in b.root_task_ids
        ]
        assert plan_only.status == "registered"

    async def test_a_ref_as_the_root_registers_its_task(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        weights = Weights(key=f"w-{new_id()}")
        (first,) = await sd.register_aio(weights, registry=registry, echo=False)
        (again,) = await sd.register_aio(first, registry=registry, echo=False)
        assert (again.task_id, again.instance_id) == (first.task_id, first.instance_id)

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
        (ref,) = await sd.register_aio(task, registry=registry)
        out = capsys.readouterr().out
        assert f"from {__name__} import double_it" in out
        assert f'double_it.ref("{task.id}", instance_id="{ref.instance_id}")' in out

    def test_the_import_line_says_when_a_class_cannot_be_imported(self):
        """A class in ``__main__``, or one not importable by name, gets a
        note instead of an import that would fail."""
        from stardag.build._plan_only import RegisteredTask

        main_cls = type(
            "Model",
            (SyncOnlyTask,),
            {"__module__": "__main__", "__qualname__": "Model"},
        )
        task = main_cls(name=f"m-{new_id()}")
        handle = RegisteredTask.of(task, new_id())
        assert handle.import_line.startswith(
            "# Model is defined in the script being run"
        )
        assert "from __main__" not in handle.import_line
        assert handle.ref_line.startswith('Model.ref("')

    def test_a_parametrised_generic_is_referred_to_by_its_origin(self):
        """``Boxed[int]`` is a pydantic subclass named ``Boxed[int]``;
        ``from m import Boxed[int]`` would not parse."""
        from stardag.build._plan_only import RegisteredTask

        task = Boxed[int](value=1)
        handle = RegisteredTask.of(task, new_id())
        assert handle.import_line == f"from {__name__} import Boxed"
        assert handle.ref_line.startswith('Boxed.ref("')
        assert handle.ref.task_class is Boxed

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

    async def test_tags_and_description_annotate_every_root(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        leaf, _, root = _chain()
        await sd.register_aio(
            [root, leaf],
            registry=registry,
            tags=["baseline ", "best", "best"],
            task_description="the baseline run",
            echo=False,
        )
        for t in (root, leaf):
            info = registry.task(str(t.id))
            assert (info.tags, info.description) == (
                ["baseline", "best"],
                "the baseline run",
            )

    async def test_one_annotation_leaves_the_other_as_it_was(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        await sd.register_aio(
            root, registry=registry, task_description="kept", echo=False
        )
        await sd.register_aio(root, registry=registry, tags=["new"], echo=False)
        info = registry.task(str(root.id))
        assert (info.tags, info.description) == (["new"], "kept")
        assert [c["tags"] for c in registry.calls_to("task_annotate")] == [
            ...,
            ["new"],
        ]

    async def test_without_annotations_nothing_is_patched(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        await sd.register_aio(root, registry=registry, echo=False)
        assert registry.calls_to("task_annotate") == []

    async def test_a_refused_annotation_leaves_the_registration_standing(
        self, default_in_memory_fs_target: Target
    ):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        with pytest.raises(APIError) as exc:
            await sd.register_aio(root, registry=registry, tags=[""], echo=False)
        assert exc.value.code == "invalid_tag"
        assert _only_build(registry).status == "registered"

    def test_the_sync_wrapper(self, default_in_memory_fs_target: Target, capsys):
        registry = InMemoryRegistry()
        _, _, root = _chain()
        (ref,) = sd.register(root, registry=registry)
        assert str(ref.instance_id) in capsys.readouterr().out
