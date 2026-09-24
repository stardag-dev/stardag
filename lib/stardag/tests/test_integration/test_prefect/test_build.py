import pytest

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
