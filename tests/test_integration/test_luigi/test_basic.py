import json

import luigi

from stardag.integration.luigi import StardagTask
from stardag.target._in_memory import InMemoryFileSystemTarget
from stardag.utils.testing.simple_dag import RootTask, RootTaskLoadedT


def test_basic(
    default_in_memory_fs_target,
    simple_dag: RootTask,
    simple_dag_expected_root_output: RootTaskLoadedT,
):
    root_task = simple_dag
    assert not root_task.complete()
    luigi_root_task = StardagTask(wrapped=root_task)
    luigi.build([luigi_root_task], local_scheduler=True)
    assert root_task.complete()

    assert simple_dag.output().load() == simple_dag_expected_root_output
    expected_root_path = f"in-memory://{simple_dag._relpath}"
    assert (
        InMemoryFileSystemTarget.path_to_bytes[expected_root_path]
        == json.dumps(simple_dag_expected_root_output, separators=(",", ":")).encode()
    )
