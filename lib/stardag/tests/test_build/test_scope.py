"""Unit tests for ``stardag.build._scope``: the code id, the structure scope
key it opens, and the synthetic placeholder the server assigns a build that
has not fixed one. Plus the engines handing their scope to the registry.
Design: ``docs/design/scope-keyed-dependency-structure.md``.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Annotated
from uuid import UUID, uuid4

import pytest

import stardag as sd
from stardag.base_model import StardagField
from stardag.build._scope import (
    STARDAG_CODE_ID_ENV,
    _reset_for_tests,
    code_id,
    is_synthetic_scope,
    scope_code_id,
    scope_config_hash,
    structure_scope_key,
)
from stardag.build_config import get_build_config
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryTarget


class Fanout(sd.Task[int]):
    """A probe, not a model DAG: ``run`` writes the level 2 value it
    resolved so a test can observe which config reached it. A real task's
    output must not depend on a ``dependencies_only`` field."""

    __namespace__ = "scope_tests"
    __version__ = "1"

    key: str
    partition_size: Annotated[int, StardagField(significance="dependencies_only")] = 100
    threads: Annotated[int, StardagField(significance="execution_only")] = 1

    def run(self) -> None:
        self.target().save(self.partition_size)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


KEY = "scope_tests.Fanout"


class TestScopeKey:
    def test_code_id_honours_the_environment(self, monkeypatch: pytest.MonkeyPatch):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "abc123")
        assert code_id() == "abc123"
        assert structure_scope_key("abc123", None).startswith("abc123:")
        assert not is_synthetic_scope(structure_scope_key("abc123", None))
        assert is_synthetic_scope(f"build:{uuid4()}")
        assert is_synthetic_scope(None)

    def test_the_config_half_travels_with_a_worker(self):
        """A worker registers its yields under its own code id and the config
        half it was handed; the placeholder has no config half."""
        assert scope_config_hash("abc123:" + "f" * 16) == "f" * 16
        assert scope_config_hash(f"build:{uuid4()}") == ""
        assert scope_config_hash("abc123") == ""

    def test_the_code_id_half_is_what_a_container_compares(self):
        """A tick or worker checks only the code half of a scope: the config
        half is derived from the build's own config, so recomputing it would
        verify nothing and would need every configured class importable."""
        key = structure_scope_key("abc123", None)
        assert scope_code_id(key) == "abc123"
        assert scope_code_id("abc123:" + "f" * 16) == "abc123"

    def test_only_the_servers_exact_shape_is_synthetic(self):
        """A prefix test would call a real scope of code id ``build``
        synthetic — the one case that skips the code-id check."""
        assert is_synthetic_scope(f"build:{uuid4()}")
        assert is_synthetic_scope(f"build:{uuid4()}".upper())
        assert not is_synthetic_scope("build:" + "f" * 16)
        assert not is_synthetic_scope("build:")
        assert not is_synthetic_scope("build:not-a-uuid")

    def test_bound_to_a_build_only_its_own_placeholder_is_synthetic(self):
        """The registry accepts any claimed scope, so ``build:<other id>``
        must read as foreign where the build id is known."""
        mine, other = uuid4(), uuid4()
        assert is_synthetic_scope(f"build:{mine}", build_id=mine)
        assert is_synthetic_scope(f"build:{mine}".upper(), build_id=str(mine))
        assert not is_synthetic_scope(f"build:{other}", build_id=mine)
        assert not is_synthetic_scope("build:" + "f" * 16, build_id=mine)
        assert is_synthetic_scope(None, build_id=mine)

    def test_an_env_code_id_must_be_usable(self, monkeypatch: pytest.MonkeyPatch):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "a:b")
        with pytest.raises(ValueError, match=STARDAG_CODE_ID_ENV):
            code_id()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "  ")
        with pytest.raises(ValueError, match=STARDAG_CODE_ID_ENV):
            code_id()
        _reset_for_tests()

    def test_code_id_is_stable_within_a_process(self, monkeypatch: pytest.MonkeyPatch):
        _reset_for_tests()
        monkeypatch.delenv(STARDAG_CODE_ID_ENV, raising=False)
        first = code_id()
        assert code_id() == first
        assert first  # a SHA or a one-off UUID, never empty
        _reset_for_tests()

    def test_os_environ_code_id_is_read_each_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "one")
        assert code_id() == "one"
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "two")
        assert code_id() == "two"
        assert os.environ[STARDAG_CODE_ID_ENV] == "two"


class _Recording(NoOpRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.starts: list[dict] = []
        self.registration_scopes: list[str | None] = []

    async def task_register_bulk_aio(
        self,
        build_id,
        tasks,
        *,
        limit_keys=None,
        declared_dependencies=None,
        scope_key=None,
    ):
        self.registration_scopes.append(scope_key)
        return None

    async def build_start_aio(
        self,
        root_tasks=None,
        description=None,
        executor_metadata=None,
        *,
        scope_key=None,
        build_config=None,
    ) -> UUID:
        self.starts.append({"scope_key": scope_key, "build_config": build_config})
        return uuid4()


class TestBuildPassesItsScope:
    async def test_build_aio_starts_the_build_with_scope_and_config(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "codeXYZ")
        registry = _Recording()
        config = {KEY: {"partition_size": 250, "threads": 2}}
        task = Fanout(key="build-me")

        summary = await sd.build_aio([task], registry=registry, build_config=config)

        assert summary.status.name == "SUCCESS"
        (start,) = registry.starts
        assert start["scope_key"] == structure_scope_key("codeXYZ", config)
        assert start["build_config"] == config
        # Every registration names the scope explicitly — the scope of the
        # code that evaluated the edges — rather than leaving it to the
        # server's notion of the build's current scope.
        assert registry.registration_scopes
        assert set(registry.registration_scopes) == {
            structure_scope_key("codeXYZ", config)
        }
        # The config is not left installed after the build.
        assert get_build_config() is None
        # ...and the task that ran read it: it wrote its partition size.
        assert Fanout(key="build-me").target().load() == 250
        _reset_for_tests()

    async def test_the_config_is_stored_in_its_json_form(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A Python value the field accepts (a datetime) is settled into JSON
        at the entry, so the registry body and the worker environment carry
        the form they can serialise — and the task still reads a datetime."""

        class Dated(sd.Task[int]):
            __namespace__ = "scope_tests"

            key: str
            since: Annotated[
                datetime, StardagField(significance="dependencies_only")
            ] = datetime(2026, 1, 1, tzinfo=timezone.utc)

            def run(self) -> None:
                self.target().save(self.since.year)

            def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
                return InMemoryTarget(key=str(self.id))

        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "codeJSON")
        registry = _Recording()
        at = datetime(2030, 3, 4, tzinfo=timezone.utc)
        key = "scope_tests.Dated"

        summary = await sd.build_aio(
            [Dated(key="d")], registry=registry, build_config={key: {"since": at}}
        )

        assert summary.status.name == "SUCCESS"
        (start,) = registry.starts
        assert start["build_config"] == {key: {"since": "2030-03-04T00:00:00Z"}}
        assert start["scope_key"] == structure_scope_key(
            "codeJSON", {key: {"since": at}}
        )
        assert Dated(key="d").target().load() == 2030
        _reset_for_tests()

    @pytest.mark.parametrize("mode", ["warn", "raise"])
    async def test_a_resume_refused_by_a_too_old_server_propagates(
        self, monkeypatch: pytest.MonkeyPatch, mode: str
    ):
        """The resume call sits inside the engines' registry-error handler,
        and ``on_registry_failure="warn"`` used to shrug the refusal off as
        an outage and run the build unscoped. A refusal is not an outage."""
        from stardag.exceptions import RegistryTooOldError

        class TooOld(_Recording):
            async def build_resume_aio(  # type: ignore[override]
                self, build_id, *args, **kwargs
            ):
                raise RegistryTooOldError("predates scopes", operation="resume")

        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "codeOLD")
        with pytest.raises(RegistryTooOldError):
            await sd.build_aio(
                [Fanout(key="resume-me")],
                registry=TooOld(),
                resume_build_id=uuid4(),
                on_registry_failure=mode,  # type: ignore[arg-type]
            )
        _reset_for_tests()

    def test_build_sequential_passes_scope_too(self, monkeypatch: pytest.MonkeyPatch):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "codeSEQ")

        class Sync(NoOpRegistry):
            def __init__(self) -> None:
                super().__init__()
                self.starts: list[dict] = []

            def build_start(
                self,
                root_tasks=None,
                description=None,
                executor_metadata=None,
                *,
                scope_key=None,
                build_config=None,
            ) -> UUID:
                self.starts.append(
                    {"scope_key": scope_key, "build_config": build_config}
                )
                return uuid4()

        registry = Sync()
        sd.build_sequential(
            [Fanout(key="seq")], registry=registry, build_config={KEY: {"threads": 3}}
        )
        (start,) = registry.starts
        assert start["scope_key"] == structure_scope_key(
            "codeSEQ", {KEY: {"threads": 3}}
        )
        # An execution-only override leaves the structure hash untouched.
        assert start["scope_key"] == structure_scope_key("codeSEQ", None)
        _reset_for_tests()
