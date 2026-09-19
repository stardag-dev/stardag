"""The three levels of parameter significance, and the build config that
supplies the two that are not identity (``docs/design/scope-keyed-dependency-structure.md``)."""

from __future__ import annotations

import os
from typing import Annotated
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError, WrapSerializer

import stardag as sd
from stardag.base_model import CONTEXT_MODE_KEY, StardagField, field_significance
from stardag.build._scope import (
    STARDAG_CODE_ID_ENV,
    _reset_for_tests,
    code_id,
    is_synthetic_scope,
    scope_code_id,
    structure_scope_key,
)
from stardag.build_config import (
    BuildConfigError,
    build_config_scope,
    canonical_structure_config,
    get_build_config,
    rebind_to_build_config,
    structure_config_hash,
)
from stardag.registry import NoOpRegistry
from stardag.target import InMemoryTarget


def _round_in_hash_mode(value, handler, info):
    """A hash-only serializer: the standard way a float is made hash-stable."""
    if info.context and info.context.get(CONTEXT_MODE_KEY) == "hash":
        return round(value, 1)
    return handler(value)


Rounded = Annotated[float, WrapSerializer(_round_in_hash_mode)]


class Fanout(sd.Task[int]):
    __namespace__ = "sig_tests"
    __version__ = "1"

    key: str
    partition_size: Annotated[int, StardagField(significance="dependencies_only")] = 100
    threads: Annotated[int, StardagField(significance="execution_only")] = 1
    # A float that only counts to one decimal in the hash — the standard
    # hash control, here on a level 2 field.
    ratio: Annotated[Rounded, StardagField(significance="dependencies_only")] = 0.5

    def run(self) -> None:
        self.target().save(self.partition_size)

    def target(self) -> InMemoryTarget[int]:  # type: ignore[override]
        return InMemoryTarget(key=str(self.id))


KEY = "sig_tests.Fanout"


class TestSignificanceOnTheModel:
    def test_defaults_apply_without_a_build_config(self):
        task = Fanout(key="a")
        assert (task.partition_size, task.threads) == (100, 1)
        assert get_build_config() is None

    @pytest.mark.parametrize("field", ["partition_size", "threads"])
    def test_non_identity_fields_cannot_be_passed_at_init(self, field: str):
        with pytest.raises(ValidationError, match="build config"):
            # Plain validation is init: only ``mode="compat"`` (registry data)
            # tolerates the field being present.
            Fanout.model_validate({"key": "a", field: 7})

    def test_values_are_resolved_from_the_build_config(self):
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            task = Fanout(key="a")
        assert (task.partition_size, task.threads) == (250, 8)

    def test_levels_two_and_three_do_not_move_the_id(self):
        plain = Fanout(key="a")
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            configured = Fanout(key="a")
        assert configured.id == plain.id

    def test_registry_mode_dump_carries_identity_only(self):
        with build_config_scope({KEY: {"partition_size": 250, "threads": 8}}):
            task = Fanout(key="a")
        data = task.model_dump(mode="json", context={CONTEXT_MODE_KEY: "registry"})
        assert "partition_size" not in data and "threads" not in data
        assert data["key"] == "a"
        assert data["__name"] == "Fanout"
        # ...and the ordinary dump still shows the effective values.
        assert task.model_dump()["partition_size"] == 250

    def test_compat_validation_strips_a_stale_value(self):
        """Registry data written before significance existed carries the
        value; rehydration drops it and resolves from the config instead."""
        with build_config_scope({KEY: {"partition_size": 9}}):
            task = Fanout.model_validate(
                {"key": "a", "version": "1", "partition_size": 42, "threads": 3},
                context={CONTEXT_MODE_KEY: "compat"},
            )
        assert task.partition_size == 9
        assert task.threads == 1

    def test_rebind_re_resolves_from_the_installed_config(self):
        task = Fanout(key="a")
        with build_config_scope({KEY: {"partition_size": 3}}):
            rebound = rebind_to_build_config(task)
        assert isinstance(rebound, Fanout)
        assert rebound.partition_size == 3
        assert rebound.id == task.id

    def test_compat_default_is_refused_on_a_non_identity_field(self):
        with pytest.raises(ValueError, match="compat_default"):
            StardagField(compat_default=1, significance="execution_only")

    def test_hash_exclude_reads_as_execution_only(self):
        with pytest.warns(DeprecationWarning):
            legacy = StardagField(hash_exclude=True)
        assert legacy.effective_significance == "execution_only"
        assert not legacy.is_identity
        assert field_significance(Fanout.model_fields["key"]) == "identity"
        assert (
            field_significance(Fanout.model_fields["partition_size"])
            == "dependencies_only"
        )


class TestSignificanceIsChecked:
    def test_a_typo_is_refused_at_field_creation(self):
        """A Literal is a hint; an unchecked typo would read as non-identity
        on the model and as execution-only in the structure hash."""
        with pytest.raises(ValueError, match="dependencies_only") as excinfo:
            StardagField(significance="dependencies-only")  # type: ignore[arg-type]
        assert "identity" in str(excinfo.value)
        assert "execution_only" in str(excinfo.value)


class TestStructureConfigHash:
    def test_empty_and_none_hash_alike(self):
        assert structure_config_hash(None) == structure_config_hash({})

    def test_execution_only_overrides_do_not_change_the_hash(self):
        assert structure_config_hash({KEY: {"threads": 16}}) == structure_config_hash(
            None
        )

    def test_dependencies_only_overrides_change_the_hash(self):
        assert structure_config_hash(
            {KEY: {"partition_size": 250}}
        ) != structure_config_hash(None)

    def test_an_override_equal_to_the_default_is_a_no_op(self):
        assert structure_config_hash(
            {KEY: {"partition_size": 100}}
        ) == structure_config_hash(None)

    def test_the_fields_hash_controls_apply(self):
        """Two ratios that the field's own hash-mode serializer collapses
        produce one scope; hashing the raw JSON would not."""
        assert canonical_structure_config({KEY: {"ratio": 0.76}})[KEY] == {"ratio": 0.8}
        assert structure_config_hash({KEY: {"ratio": 0.76}}) == structure_config_hash(
            {KEY: {"ratio": 0.84}}
        )
        # ...and rounding onto the default makes the override a no-op.
        assert structure_config_hash({KEY: {"ratio": 0.51}}) == structure_config_hash(
            None
        )

    def test_unknown_class_field_and_identity_fields_are_refused(self):
        with pytest.raises(BuildConfigError, match="not registered"):
            structure_config_hash({"sig_tests.Nope": {"x": 1}})
        with pytest.raises(BuildConfigError, match="no such field"):
            structure_config_hash({KEY: {"nope": 1}})
        with pytest.raises(BuildConfigError, match="identity"):
            structure_config_hash({KEY: {"key": "b"}})
        with pytest.raises(BuildConfigError, match="not a valid"):
            structure_config_hash({KEY: {"partition_size": "many"}})


class TestScopeKey:
    def test_code_id_honours_the_environment(self, monkeypatch: pytest.MonkeyPatch):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "abc123")
        assert code_id() == "abc123"
        assert structure_scope_key("abc123", None).startswith("abc123:")
        assert not is_synthetic_scope(structure_scope_key("abc123", None))
        assert is_synthetic_scope(f"build:{uuid4()}")
        assert is_synthetic_scope(None)

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


class _Recording(NoOpRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.starts: list[dict] = []

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
        # The config is not left installed after the build.
        assert get_build_config() is None
        # ...and the task that ran read it: it wrote its partition size.
        assert Fanout(key="build-me").target().load() == 250
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

    def test_os_environ_code_id_is_read_each_call(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        _reset_for_tests()
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "one")
        assert code_id() == "one"
        monkeypatch.setenv(STARDAG_CODE_ID_ENV, "two")
        assert code_id() == "two"
        assert os.environ[STARDAG_CODE_ID_ENV] == "two"
