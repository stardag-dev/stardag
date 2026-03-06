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


def task_api(limit: int) -> sd.TaskLoads[int]:
    class Range(sd.Task[list[int]]):
        limit: int

        def run(self):
            self._save(list(range(self.limit)))

    class Sum(sd.Task[int]):
        integers: sd.TaskLoads[list[int]]

        def requires(self):
            return self.integers

        def run(self):
            self._save(sum(self.integers.load()))

    return Sum(integers=Range(limit=limit))


def target_task_api(limit: int) -> sd.TargetTask:
    from stardag.target import LoadableSaveableFileSystemTarget
    from stardag.target.serialize import FileSerializable, JSONSerializer

    def default_relpath(task: sd.TargetTask) -> str:
        task_id = str(task.id)
        return "/".join(
            [
                task.get_name(),
                task_id[:2],
                task_id[2:4],
                f"{task_id}.json",
            ]
        )

    class Range(sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]):
        limit: int

        def target(self) -> LoadableSaveableFileSystemTarget[list[int]]:
            return FileSerializable(
                wrapped=sd.get_file_target(default_relpath(self)),
                serializer=JSONSerializer(list[int]),
            )

        def run(self):
            self.target().save(list(range(self.limit)))

    class Sum(sd.TargetTask[LoadableSaveableFileSystemTarget[int]]):
        integers: sd.SubClass[
            sd.TargetTask[LoadableSaveableFileSystemTarget[list[int]]]
        ]

        def requires(self):
            return self.integers

        def target(self) -> LoadableSaveableFileSystemTarget[int]:
            return FileSerializable(
                wrapped=sd.get_file_target(default_relpath(self)),
                serializer=JSONSerializer(int),
            )

        def run(self):
            return self.target().save(sum(self.integers.target().load()))

    return Sum(integers=Range(limit=limit))
