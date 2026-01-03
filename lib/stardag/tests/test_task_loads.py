from stardag._task import BaseTask, Task
from stardag._task_loads import TaskLoads
from stardag.target import InMemoryTarget, LoadableTarget
from stardag.utils.testing.generic import assert_serialize_validate_roundtrip


class ContainerTask(BaseTask):
    task: TaskLoads[str]

    def complete(self) -> bool:
        return True

    def run(self) -> None:
        pass


class LoadsStrTask(Task[LoadableTarget[str]]):
    data: str = "hello world"

    def run(self) -> None:
        self.output().save(self.data)

    def output(self) -> InMemoryTarget[str]:
        return InMemoryTarget(key=self.id)


class LoadsIntTask(Task[LoadableTarget[int]]):
    number: int = 42

    def run(self) -> None:
        self.output().save(self.number)

    def output(self) -> InMemoryTarget[int]:
        return InMemoryTarget(key=self.id)


def test_task_loads():
    container_task = ContainerTask(task=LoadsStrTask())
    # test serialization/deserialization roundtrip
    assert_serialize_validate_roundtrip(ContainerTask, container_task)

    # container_task_int = ContainerTask(task=LoadsIntTask())
    # assert_serialize_validate_roundtrip(ContainerTask, container_task_int)
