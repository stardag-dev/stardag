from stardag import AutoTask, TaskLoads, auto_namespace

auto_namespace(__name__)


class DynamicDepsTask(AutoTask[str]):
    """Task with dynamic dependencies for testing.

    This task enforces the dynamic deps contract: after yielding deps,
    it asserts that ALL yielded deps are complete before continuing.
    """

    value: str
    static_deps: tuple[TaskLoads[str], ...] = ()
    dynamic_deps: tuple[TaskLoads[str], ...] = ()

    def requires(self):  # type: ignore
        return self.static_deps

    def run(self):  # type: ignore
        if self.dynamic_deps:
            # Yield all dynamic deps at once
            yield self.dynamic_deps

            # CONTRACT: After yield returns, ALL deps MUST be complete.
            # This assertion enforces the build system contract.
            for dep in self.dynamic_deps:
                if not dep.complete():  # type: ignore
                    raise AssertionError(
                        f"Dynamic deps contract violated! Dep {dep} is not complete "
                        f"after yield. The build system must ensure all yielded deps "
                        f"are complete before resuming the generator."
                    )

        self.output().save(self.value)


def get_dynamic_deps_dag():
    dyn_and_static = DynamicDepsTask(
        value="1",
        static_deps=(
            DynamicDepsTask(value="20"),
            DynamicDepsTask(value="21"),
        ),
        dynamic_deps=(
            DynamicDepsTask(value="30"),
            DynamicDepsTask(value="31"),
        ),
    )
    parent = DynamicDepsTask(
        value="0",
        static_deps=(
            dyn_and_static,
            DynamicDepsTask(value="31"),
        ),
    )

    return parent


def assert_dynamic_deps_task_complete_recursive(
    task: DynamicDepsTask,
    is_complete: bool,
):
    assert task.complete() == is_complete
    for dep in task.static_deps + task.dynamic_deps:
        assert_dynamic_deps_task_complete_recursive(dep, is_complete)  # type: ignore
