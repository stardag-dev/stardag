import pytest

import stardag as sd

try:
    from prefect import flow

    from stardag.integration.prefect import build_aio

    prefect_available = True
except ImportError:
    flow = None
    build_aio = None
    prefect_available = False

from stardag.utils.testing.dynamic_deps_dag import (
    assert_dynamic_deps_task_complete_recursive,
    get_dynamic_deps_dag,
)


@pytest.mark.skipif(not prefect_available, reason="Prefect is not installed")
async def test_build_dag_dynamic_deps(default_in_memory_fs_target):
    dag = get_dynamic_deps_dag()
    assert_dynamic_deps_task_complete_recursive(dag, False)

    @flow  # type: ignore
    async def dynamic_deps_dag():
        task_id_to_future = await build_aio(dag)  # type: ignore
        for future in task_id_to_future.values():
            future.wait()

    await dynamic_deps_dag()
    assert_dynamic_deps_task_complete_recursive(dag, True)


@pytest.mark.skipif(not prefect_available, reason="Prefect is not installed")
async def test_a_stopped_build_is_not_reported_as_a_task_failure(
    default_in_memory_fs_target,
):
    """A claim refused ``build_not_running`` raises ``BuildStopped`` and
    reports nothing against the task (no claim was taken)."""
    from stardag.build import BuildStopped
    from stardag.build._registration import walk_aio
    from stardag.build._session import ResidentSession
    from stardag.integration.prefect._build import _PrefectTaskRunWrapper
    from stardag.testing import InMemoryRegistry
    from stardag.utils.testing.helper_tasks import SyncOnlyTask

    registry = InMemoryRegistry()
    task = SyncOnlyTask(name="prefect-stopped")
    session = ResidentSession(registry)
    await session.open([task])
    await session.register(await walk_aio([task]))
    assert session.build_id is not None
    registry.build_cancel(session.build_id)

    wrapper = _PrefectTaskRunWrapper(session)
    with pytest.raises(BuildStopped, match="no longer running"):
        await wrapper.run(task)
    assert not registry.called("member_fail")
    assert await session.finish(None) == "cancelled"


@pytest.mark.skipif(not prefect_available, reason="Prefect is not installed")
async def test_local_prefect_executions_record_their_mode(default_in_memory_fs_target):
    from stardag.integration.prefect._build import _PrefectTaskRunWrapper
    from stardag.testing import InMemoryRegistry
    from stardag.build._session import ResidentSession
    from stardag.utils.testing.helper_tasks import AsyncOnlyTask, SyncOnlyTask

    wrapper = _PrefectTaskRunWrapper(ResidentSession(InMemoryRegistry()))
    sync = await wrapper._executor_details(SyncOnlyTask(name="s"))
    asyn = await wrapper._executor_details(AsyncOnlyTask(name="a"))
    assert (sync.executor, asyn.executor) == ("sync_thread", "async_main_loop")


@pytest.mark.skipif(not prefect_available, reason="Prefect is not installed")
async def test_task_refs_are_hydrated_before_the_walk(default_in_memory_fs_target):
    """A ``TaskRef`` among the roots' dependencies is replaced by its
    hydrated task: the registry stores the real body, and the root builds."""
    from uuid import uuid4

    import stardag as sd
    from stardag._core.task_ref import clear_hydration_memo
    from stardag.testing import InMemoryRegistry
    from stardag.testing._registry_state import InstanceRow, TaskRow
    from stardag.utils.testing.helper_tasks import AsyncOnlyTask

    clear_hydration_memo()
    registry = InMemoryRegistry()
    leaf = AsyncOnlyTask(name="prefect-ref-leaf")
    await leaf.run_aio()
    registry.tasks[str(leaf.id)] = TaskRow(
        task_id=str(leaf.id),
        task_namespace=leaf.get_namespace(),
        task_name=leaf.get_name(),
        version=leaf.version,
        output_uri=leaf.target().uri,
    )
    instance_id = uuid4()
    registry.instances[instance_id] = InstanceRow(
        id=instance_id,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash=str(leaf.instance_hash),
        task_id=str(leaf.id),
        body=leaf.instance_body(),
    )
    root = AsyncOnlyTask(
        name="prefect-ref-root", deps=(AsyncOnlyTask.ref(leaf.id, instance_id),)
    )
    expected = AsyncOnlyTask(name="prefect-ref-root", deps=(leaf,))
    assert root.id == expected.id

    @flow  # type: ignore
    async def build_with_ref():
        futures = await build_aio(root, registry=registry)  # type: ignore
        for future in futures.values():
            future.wait()

    await build_with_ref()
    assert expected.complete()
    bodies = [row.body for row in registry.instances.values()]
    assert expected.instance_body() in bodies
    assert not any("task_class" in str(b) for b in bodies)
    assert isinstance(root, sd.Task)


class _RefInRequires(sd.Task[str]):
    """Builds its upstream reference inside ``requires()``."""

    leaf_id: str

    def requires(self):
        from stardag.utils.testing.helper_tasks import AsyncOnlyTask

        return AsyncOnlyTask.ref(self.leaf_id)

    async def run_aio(self):
        self._save("needs-ref done")


class _YieldsRef(sd.Task[str]):
    """Yields a reference as a dynamic dependency."""

    leaf_id: str

    def run(self):  # type: ignore[override]
        from stardag.utils.testing.helper_tasks import AsyncOnlyTask

        yield AsyncOnlyTask.ref(self.leaf_id)
        self._save("yields-ref done")


@pytest.mark.skipif(not prefect_available, reason="Prefect is not installed")
@pytest.mark.parametrize("root_class", [_RefInRequires, _YieldsRef])
async def test_refs_from_requires_and_yields_use_the_builds_registry(
    root_class, default_in_memory_fs_target
):
    """With no configured registry, a reference built in ``requires()`` or
    yielded from ``run()`` must be hydrated from the build's registry."""
    from uuid import uuid4

    from stardag._core.task_ref import clear_hydration_memo
    from stardag.testing import InMemoryRegistry
    from stardag.testing._registry_state import InstanceRow, TaskRow
    from stardag.utils.testing.helper_tasks import AsyncOnlyTask

    clear_hydration_memo()
    registry = InMemoryRegistry()
    leaf = AsyncOnlyTask(name=f"prefect-ref-leaf-{root_class.__name__}")
    await leaf.run_aio()
    registry.tasks[str(leaf.id)] = TaskRow(
        task_id=str(leaf.id),
        task_namespace=leaf.get_namespace(),
        task_name=leaf.get_name(),
        version=leaf.version,
        output_uri=leaf.target().uri,
    )
    instance_id = uuid4()
    registry.instances[instance_id] = InstanceRow(
        id=instance_id,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash=str(leaf.instance_hash),
        task_id=str(leaf.id),
        body=leaf.instance_body(),
    )
    root = root_class(leaf_id=str(leaf.id))

    @flow  # type: ignore
    async def build_it():
        futures = await build_aio(root, registry=registry)  # type: ignore
        for future in futures.values():
            future.wait()

    await build_it()
    assert root.complete()
    clear_hydration_memo()
