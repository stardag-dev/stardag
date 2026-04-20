"""Decorator API for defining tasks as functions.

Provides a subset of the functionality of the class-based ``Task`` API, allowing
simple tasks to be defined with ``@task`` instead of writing a full class.

Sync functions use ``run()``; async functions use ``run_aio()``, mirroring the
class API's ``Task.run`` / ``Task.run_aio`` split.

Example (sync)::

    @task
    def add(a: int, b: int) -> int:
        return a + b

Example (async)::

    @task
    async def fetch(url: str) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                return await resp.text()
"""

import asyncio
import inspect
import typing

from pydantic import create_model

from stardag._core.base_task import BaseTask, LoadableTask
from stardag._core.task import Task
from stardag._core.task_loads import TaskLoads

LoadedT = typing.TypeVar("LoadedT")
# The return type of the decorated function — may be ``LoadedT`` (sync) or
# ``Coroutine[Any, Any, LoadedT]`` (async). At runtime we always extract the
# unwrapped return annotation via ``inspect.signature``, so the ``_FunctionTask``
# is always parameterized with the actual ``LoadedT``. This TypeVar exists to
# make explicit that the decorator signatures accept both sync and async callables.
_FuncReturnT = typing.TypeVar("_FuncReturnT")
FuncT = typing.TypeVar("FuncT", bound=typing.Callable)

_PWrapped = typing.ParamSpec("_PWrapped")


class _FunctionTask(Task[LoadedT], typing.Generic[LoadedT, _PWrapped]):
    """Base class for tasks created by the ``@task`` decorator.

    Wraps a plain function (sync or async) as a ``Task`` subclass.
    The decorated function's parameters become Pydantic model fields,
    and dependencies are resolved automatically before calling the function.
    """

    _func: typing.Callable[_PWrapped, LoadedT]
    # Set to True when ``_func`` is a coroutine function (async def).
    _is_async: bool = False

    if typing.TYPE_CHECKING:

        def __init__(
            self,
            # TODO not really possible to type hint this (?) :/ Below would only allow
            # the same signature as the function, not TaskLoads[<type>]
            #    *args: _PWrapped.args, **kwargs: _PWrapped.kwargs
            # and if the user is forced to type hint the function with
            # <type> | TaskLoads[<type>], then it doesn't make sense inside the function
            **kwargs: typing.Any,
        ) -> None: ...

    @classmethod
    def call(cls, *args: _PWrapped.args, **kwargs: _PWrapped.kwargs) -> LoadedT:
        """Call the underlying sync function directly.

        Raises TypeError if the decorated function is async — use ``call_aio`` instead.
        """
        if cls._is_async:
            raise TypeError(
                f"{cls.__name__}.call() cannot be used with an async function. "
                f"Use 'await {cls.__name__}.call_aio(...)' instead."
            )
        return cls._func(*args, **kwargs)  # type: ignore

    @classmethod
    async def call_aio(
        cls, *args: _PWrapped.args, **kwargs: _PWrapped.kwargs
    ) -> LoadedT:
        """Call the underlying async function directly.

        Raises TypeError if the decorated function is sync — use ``call`` instead.
        """
        if not cls._is_async:
            raise TypeError(
                f"{cls.__name__}.call_aio() cannot be used with a sync function. "
                f"Use '{cls.__name__}.call(...)' instead."
            )
        return await cls._func(*args, **kwargs)  # type: ignore

    def requires(self) -> typing.Mapping[str, BaseTask] | None:
        requires = {
            name: getattr(self, name)
            for name in self.__class__.model_fields.keys()
            if isinstance(getattr(self, name), BaseTask)
        }
        return requires or None

    def run(self) -> None:
        if self._is_async:
            # Async function: delegate to run_aio(). Mirrors BaseTask.run()'s
            # fallback for async-only tasks, but we handle it here explicitly
            # since _FunctionTask defines both run() and run_aio().
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                asyncio.run(self.run_aio())
                return
            raise RuntimeError(
                f"Cannot call {type(self).__name__}.run() from within an async "
                f"context. Use 'await task.run_aio()' instead."
            )
        result = self.call(**self._get_inputs())  # type: ignore
        self._save(result)

    async def run_aio(self) -> None:
        """Async execution path — used when the decorated function is async."""
        if not self._is_async:
            # Sync function: just run synchronously
            self.run()
            return
        result = await self.call_aio(**await self._get_inputs_aio())  # type: ignore
        await self._save_aio(result)

    def _get_inputs(self) -> _PWrapped.kwargs:  # type: ignore
        """Resolve dependency inputs synchronously.

        Task parameters that are ``LoadableTask`` instances (validated by pydantic
        via ``TaskLoads[T]``) are loaded; plain values are passed through as-is.
        """

        def get_input(name):
            value = getattr(self, name)
            if isinstance(value, LoadableTask):
                return value.load()
            return value

        return {
            name: get_input(name)
            for name in self.__class__.model_fields.keys()
            if name != "version"
        }

    async def _get_inputs_aio(self) -> _PWrapped.kwargs:  # type: ignore
        """Resolve dependency inputs asynchronously via ``load_aio``."""

        async def get_input(name):
            value = getattr(self, name)
            if isinstance(value, LoadableTask):
                return await value.load_aio()
            return value

        return {
            name: await get_input(name)
            for name in self.__class__.model_fields.keys()
            if name != "version"
        }


class _TaskWrapper(typing.Protocol):
    def __call__(
        self,
        _func: typing.Callable[_PWrapped, _FuncReturnT],
    ) -> typing.Type[_FunctionTask[_FuncReturnT, _PWrapped]]: ...


_RelpathOverride = str | typing.Callable[[Task[LoadedT]], str]


class RelpathSettings(typing.TypedDict):
    base: _RelpathOverride
    extra: _RelpathOverride
    filename: _RelpathOverride
    extension: _RelpathOverride


@typing.overload
def task(
    _func: typing.Callable[_PWrapped, _FuncReturnT],
    *,
    name: str | None = None,
    version: str = "",
    relpath: RelpathSettings | _RelpathOverride | None = None,
    target_root_key: str | None = None,
) -> typing.Type[_FunctionTask[_FuncReturnT, _PWrapped]]: ...


@typing.overload
def task(
    *,
    name: str | None = None,
    version: str = "",
    relpath: RelpathSettings | _RelpathOverride | None = None,
    target_root_key: str | None = None,
) -> _TaskWrapper: ...


def task(
    _func: typing.Callable[_PWrapped, _FuncReturnT] | None = None,
    *,
    name: str | None = None,
    version: str = "",
    relpath: RelpathSettings | _RelpathOverride | None = None,
    target_root_key: str | None = None,
) -> typing.Type[_FunctionTask[_FuncReturnT, _PWrapped]] | _TaskWrapper:
    def wrapper(
        _func: typing.Callable[_PWrapped, _FuncReturnT],
    ) -> typing.Type[_FunctionTask[_FuncReturnT, _PWrapped]]:
        """Decorator to turn a function into a task.

        Supports both sync and async functions. Async functions (``async def``)
        will use ``run_aio()`` for execution; sync functions use ``run()``.
        """

        is_async = inspect.iscoroutinefunction(_func)

        # Reject generator functions — dynamic dependencies require the class API
        if inspect.isgeneratorfunction(_func) or inspect.isasyncgenfunction(_func):
            raise TypeError(
                f"@task does not support generator functions (got {_func.__name__}). "
                "Dynamic dependencies require the class-based Task API — "
                "implement run()/run_aio() as a generator method on a Task subclass."
            )

        signature = inspect.signature(_func)
        return_type = signature.return_annotation
        if return_type == inspect.Parameter.empty:
            raise ValueError("Return type must be annotated")
        args = signature.parameters
        if any(arg.annotation == inspect.Parameter.empty for arg in args.values()):
            raise ValueError("All arguments must have annotations")

        task_class = create_model(
            name or _func.__name__,
            __base__=_FunctionTask[return_type, _PWrapped],
            __module__=_func.__module__,
            version=(str, version),
            **{  # type: ignore
                name: (
                    _get_param_annotation(arg.annotation),
                    arg.default if arg.default != inspect.Parameter.empty else ...,
                )
                for name, arg in args.items()
            },
        )
        task_class._func = _func
        task_class._is_async = is_async
        task_class.__version__ = version

        # extra properties
        if relpath is not None:
            if isinstance(relpath, dict):
                for key in ["base", "extra", "filename", "extension"]:
                    value = relpath.get(key)
                    if value is not None:
                        if callable(value):
                            setattr(task_class, f"_relpath_{key}", property(value))
                        elif isinstance(value, str):
                            setattr(task_class, f"_relpath_{key}", value)
                        else:
                            raise ValueError(f"Invalid relpath value for {key}")
            elif callable(relpath):
                task_class._relpath = property(relpath)
            elif isinstance(relpath, str):
                task_class._relpath = relpath
            else:
                raise ValueError("Invalid relpath type")

        if target_root_key is not None:
            task_class._target_root_key = target_root_key

        return task_class

    if _func is None:
        return wrapper  # type: ignore

    return wrapper(_func)


_DependsT = typing.TypeVar("_DependsT")


class _DependsOnMarker:
    pass


Depends = typing.Annotated[_DependsT, _DependsOnMarker]


def _get_param_annotation(func_annotation: typing.Any) -> typing.Any:
    args = typing.get_args(func_annotation)
    if _DependsOnMarker in args:
        stripped_args = tuple(arg for arg in args if arg != _DependsOnMarker)
        if len(stripped_args) > 1:
            stripped_annotation = typing.Annotated[*stripped_args]  # type: ignore
        else:
            stripped_annotation = stripped_args[0]  # type: ignore
        return TaskLoads[stripped_annotation]
    return func_annotation | TaskLoads[func_annotation]
