from stardag._auto_task import AutoTask
from stardag._task import Task, auto_namespace
from stardag.polymorphic import SubClass
from stardag.target import LoadableTarget

LeafTaskLoadedT = dict[str, str | int | None]

auto_namespace(__name__)


class LeafTask(AutoTask[LeafTaskLoadedT]):
    param_a: int
    param_b: str

    def run(self):
        self.output().save(self.model_dump(mode="json"))


ParentTaskLoadedT = dict[str, list[LeafTaskLoadedT]]


class ParentTask(AutoTask[ParentTaskLoadedT]):
    param_ab_s: list[tuple[int, str]]

    def requires(self):
        return [LeafTask(param_a=a, param_b=b) for a, b in self.param_ab_s]

    def run(self):
        self.output().save(
            {"leaf_tasks": [task.output().load() for task in self.requires()]}
        )


RootTaskLoadedT = dict[str, ParentTaskLoadedT]


class RootTask(AutoTask[RootTaskLoadedT]):
    parent_task: SubClass[Task[LoadableTarget[ParentTaskLoadedT]]]

    def requires(self):
        return self.parent_task

    def run(self):
        self.output().save({"parent_task": self.parent_task.output().load()})


def get_simple_dag():
    return RootTask(
        parent_task=ParentTask(param_ab_s=[(1, "a"), (2, "b")]),
    )


def get_simple_dag_expected_root_output():
    # Key order: __type_namespace__/__type_name__ (prepended by _serialize_extra),
    # version (from BaseTask), param_a/param_b (from LeafTask)
    return {
        "parent_task": {
            "leaf_tasks": [
                {
                    "__type_namespace__": __name__,
                    "__type_name__": "LeafTask",
                    "version": "",
                    "param_a": 1,
                    "param_b": "a",
                },
                {
                    "__type_namespace__": __name__,
                    "__type_name__": "LeafTask",
                    "version": "",
                    "param_a": 2,
                    "param_b": "b",
                },
            ]
        },
    }
