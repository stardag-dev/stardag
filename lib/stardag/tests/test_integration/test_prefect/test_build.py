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
