import pytest

import stardag as sd
from stardag._core.alias_task import AliasedMetadata, AliasTask


class LoadsIntTask(sd.AutoTask[int]):
    def run(self):
        self.output().save(42)


class DownstreamTask(sd.AutoTask[int]):
    loads_int: sd.TaskLoads[int]

    def run(self):
        self.output().save(self.loads_int.output().load() + 1)


def test_alias_task(default_in_memory_fs_target):
    loads_int_task = LoadsIntTask()
    downstream_task = DownstreamTask(loads_int=loads_int_task)

    # Create an AliasTask referencing the LoadsIntTask
    alias_task = AliasTask[int](
        aliased=AliasedMetadata(
            id=loads_int_task.id,
            uri=loads_int_task.output().path,
            body=loads_int_task.model_dump(),
        )
    )

    # Use the AliasTask in place of the LoadsIntTask
    downstream_task_with_alias = DownstreamTask(loads_int=alias_task)

    assert alias_task.aliased.id == loads_int_task.id
    assert not alias_task.complete()

    with pytest.raises(NotImplementedError):
        alias_task.run()

    # Run the tasks
    loads_int_task.run()

    assert alias_task.complete()

    # verify downstream
    assert downstream_task.id == downstream_task_with_alias.id

    downstream_task_with_alias_dumped = downstream_task_with_alias.model_dump()
    downstream_task_with_alias_dumped_expected = {
        "__namespace": "",
        "__name": "DownstreamTask",
        "version": "",
        "loads_int": {
            "__aliased": {
                "id": str(loads_int_task.id),
                "uri": loads_int_task.output().path,
            },
            "__namespace": "",
            "__name": "LoadsIntTask",
            "version": "",
        },
    }
    # b = {
    #     "__namespace": "",
    #     "__name": "DownstreamTask",
    #     "version": "",
    #     "loads_int": {
    #         "__aliased": {
    #             "id": "21c012fb-3772-5fbf-9d0f-ebd5b7353280",
    #             "uri": "in-memory://LoadsIntTask/21/c0/21c012fb-3772-5fbf-9d0f-ebd5b7353280.json",
    #         },
    #         "__namespace": "",
    #         "__name": "LoadsIntTask",
    #         "version": "",
    #     },
    # }
    assert (
        downstream_task_with_alias_dumped == downstream_task_with_alias_dumped_expected
    )

    reconstructed_downstream_task = DownstreamTask.model_validate(
        downstream_task_with_alias_dumped
    )
    assert reconstructed_downstream_task == downstream_task_with_alias
