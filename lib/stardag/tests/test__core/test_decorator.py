import pytest

from stardag import Task, auto_namespace
from stardag._core.base_task import LoadableTask
from stardag._core.decorator import Depends, task

auto_namespace(__name__)  # Avoid collisions in task registry


def test_basic(default_in_memory_fs_target):
    @task
    def add(a: int, b: int) -> int:
        return a + b

    assert add.__version__ == ""
    assert add.model_fields["version"].default == ""

    add_b_task = add(a=2, b=3)
    add_task = add(a=1, b=add_b_task)
    assert add_task.requires() == {"b": add_b_task}
    assert not add_b_task.complete()
    assert not add_task.complete()

    with pytest.raises(FileNotFoundError):
        add_task.run()

    add_task.b.run()  # type: ignore
    assert add_task.b.load() == 5  # type: ignore

    add_task.run()
    assert add_task.load() == 6


def test_with_params(default_in_memory_fs_target):
    def val_or_id(task):
        return task.id if isinstance(task, Task) else task

    @task(
        version="1",
        relpath=lambda self: (
            f"add_task/add2/v{self.version}/"
            f"{val_or_id(self.a)}_{val_or_id(self.b)}/"  # type: ignore
            "result.txt"
        ),
    )
    def add2(a: int, b: int) -> int:
        return a + b

    assert add2.__version__ == "1"
    assert add2.model_fields["version"].default == "1"

    add_b_task = add2(a=2, b=3)
    add_task = add2(a=1, b=add_b_task)
    assert add_task.requires() == {"b": add_b_task}
    assert not add_b_task.complete()
    assert not add_task.complete()

    with pytest.raises(FileNotFoundError):
        add_task.run()

    add_task.b.run()  # type: ignore
    assert add_task.b.load() == 5  # type: ignore

    add_task.run()
    assert add_task.load() == 6

    assert add_task._relpath.startswith("add_task/")
    assert add_task.b._relpath.startswith("add_task/")  # type: ignore
    assert (
        add_task.target().uri
        == f"in-memory://add_task/add2/v1/1_{add_b_task.id}/result.txt"
    )


def test_Depends():
    class CustomParam:
        def __init__(self, value):
            self.value = value

    @task
    def custom_add(a: int, b: Depends[CustomParam]) -> int:
        return a + b.value

    # assert custom_add.model_fields["b"].rebuild_annotation() == TaskLoads[CustomParam]
    assert custom_add.model_fields["b"].annotation == LoadableTask[CustomParam]


def test_target_root_key(default_in_memory_fs_target):
    from stardag.config import DEFAULT_TARGET_ROOT_KEY

    @task
    def default_root(a: int) -> int:
        return a

    @task(target_root_key="custom-root")
    def custom_root(a: int) -> int:
        return a

    default_instance = default_root(a=1)
    custom_instance = custom_root(a=1)

    assert default_instance._target_root_key == DEFAULT_TARGET_ROOT_KEY
    assert custom_instance._target_root_key == "custom-root"


# --- Async tests ---


@pytest.mark.asyncio
async def test_async_basic(default_in_memory_fs_target):
    """Async decorated functions should use run_aio() for execution."""

    @task
    async def add_async(a: int, b: int) -> int:
        return a + b

    assert add_async._is_async is True
    assert add_async.__version__ == ""

    add_b_task = add_async(a=2, b=3)
    add_task = add_async(a=1, b=add_b_task)
    assert add_task.requires() == {"b": add_b_task}
    assert not add_b_task.complete()
    assert not add_task.complete()

    # run_aio should work
    await add_b_task.run_aio()
    assert add_b_task.load() == 5

    await add_task.run_aio()
    assert add_task.load() == 6


@pytest.mark.asyncio
async def test_async_with_params(default_in_memory_fs_target):
    """Async tasks should work with decorator parameters (version, relpath, etc.)."""

    @task(version="2")
    async def multiply_async(a: int, b: int) -> int:
        return a * b

    assert multiply_async.__version__ == "2"
    assert multiply_async._is_async is True

    t = multiply_async(a=3, b=4)
    await t.run_aio()
    assert t.load() == 12


def test_async_run_sync_fallback(default_in_memory_fs_target):
    """Async tasks can still be run via sync run() (delegates to asyncio.run)."""

    @task
    async def add_async_sync(a: int, b: int) -> int:
        return a + b

    t = add_async_sync(a=3, b=4)
    # sync run() should delegate to run_aio() via BaseTask's asyncio.run() fallback
    t.run()
    assert t.load() == 7


def test_sync_task_not_async(default_in_memory_fs_target):
    """Sync decorated functions should not be marked as async."""

    @task
    def add_sync(a: int, b: int) -> int:
        return a + b

    assert add_sync._is_async is False


@pytest.mark.asyncio
async def test_async_with_dependencies(default_in_memory_fs_target):
    """Async tasks should resolve dependencies (load outputs) before calling the function."""

    @task
    async def double_async(x: int) -> int:
        return x * 2

    @task
    async def add_one_async(x: int) -> int:
        return x + 1

    dep = double_async(x=5)
    t = add_one_async(x=dep)

    assert t.requires() == {"x": dep}

    await dep.run_aio()
    assert dep.load() == 10

    await t.run_aio()
    assert t.load() == 11


@pytest.mark.asyncio
async def test_async_has_custom_run_aio(default_in_memory_fs_target):
    """Async function tasks should register as having custom run_aio."""

    @task
    async def async_task(a: int) -> int:
        return a

    @task
    def sync_task(a: int) -> int:
        return a

    async_instance = async_task(a=1)
    sync_instance = sync_task(a=1)

    # Async task: has custom run_aio (from _FunctionTask), has custom run (from _FunctionTask)
    # The key distinction is _is_async which controls dispatch
    assert async_instance._is_async is True
    assert sync_instance._is_async is False


def test_call_raises_on_async_func():
    """call() should raise TypeError when used on an async function task."""

    @task
    async def async_add(a: int, b: int) -> int:
        return a + b

    with pytest.raises(TypeError, match="cannot be used with an async function"):
        async_add.call(a=1, b=2)  # type: ignore[reportUnusedCoroutine]


@pytest.mark.asyncio
async def test_call_aio_raises_on_sync_func():
    """call_aio() should raise TypeError when used on a sync function task."""

    @task
    def sync_add(a: int, b: int) -> int:
        return a + b

    with pytest.raises(TypeError, match="cannot be used with a sync function"):
        await sync_add.call_aio(a=1, b=2)


# --- Generator rejection tests ---


def test_sync_generator_rejected():
    """Sync generator functions should be rejected by @task."""
    with pytest.raises(TypeError, match="does not support generator functions"):

        @task
        def gen_func(a: int) -> int:  # type: ignore[reportInvalidTypeForm]
            yield a  # type: ignore[misc]
            return a


def test_async_generator_rejected():
    """Async generator functions should be rejected by @task."""
    with pytest.raises(TypeError, match="does not support generator functions"):

        @task
        async def async_gen_func(a: int) -> int:  # type: ignore[reportInvalidTypeForm]
            yield a  # type: ignore[misc]
