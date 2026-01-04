"""Comparison of three levels of the task API.

The following three ways of specifying a root_task, its dependencies, persistent
targets and serialization are 100% equivalent.
"""

import stardag as sd


def decorator_api(limit: int) -> sd.TaskLoads[int]:
    @sd.task(name="Range")
    def get_range(limit: int) -> list[int]:
        return list(range(limit))

    @sd.task(name="Sum")
    def get_sum(integers: sd.Depends[list[int]]) -> int:
        return sum(integers)

    return get_sum(integers=get_range(limit=limit))


def auto_task_api(limit: int) -> sd.TaskLoads[int]:
    class Range(sd.AutoTask[list[int]]):
        limit: int

        def run(self):
            self.output().save(list(range(self.limit)))

    class Sum(sd.AutoTask[int]):
        integers: sd.TaskLoads[list[int]]

        def requires(self):
            return self.integers

        def run(self):
            self.output().save(sum(self.integers.output().load()))

    return Sum(integers=Range(limit=limit))


def base_task_api(limit: int) -> sd.TaskLoads[int]:
    from stardag.target import LoadableSaveableFileSystemTarget
    from stardag.target.serialize import JSONSerializer, Serializable

    def default_relpath(task: sd.Task) -> str:
        task_id = str(task.id)
        return "/".join(
            [
                task.get_name(),
                task_id[:2],
                task_id[2:4],
                f"{task_id}.json",
            ]
        )

    class Range(sd.Task[LoadableSaveableFileSystemTarget[list[int]]]):
        limit: int

        def output(self) -> LoadableSaveableFileSystemTarget[list[int]]:
            return Serializable(
                wrapped=sd.get_target(default_relpath(self), task=self),
                serializer=JSONSerializer(list[int]),
            )

        def run(self):
            self.output().save(list(range(self.limit)))

    class Sum(sd.Task[LoadableSaveableFileSystemTarget[int]]):
        integers: sd.TaskLoads[list[int]]

        def requires(self):
            return self.integers

        def output(self) -> LoadableSaveableFileSystemTarget[int]:
            return Serializable(
                wrapped=sd.get_target(default_relpath(self), task=self),
                serializer=JSONSerializer(int),
            )

        def run(self):
            return self.output().save(sum(self.integers.output().load()))

    return Sum(integers=Range(limit=limit))
