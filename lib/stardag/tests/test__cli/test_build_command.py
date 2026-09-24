"""``stardag build``: root resolution from ``module:attr``, settings
validation, and ``--dry-run`` on a small DAG."""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from typer.testing import CliRunner

from stardag._cli import app as cli
from stardag._cli._roots import RefError, parse_params, resolve_roots
from stardag.utils.testing.simple_dag import LeafTask, get_simple_dag

runner = CliRunner(env={"COLUMNS": "240"})

SIMPLE_DAG = "stardag.utils.testing.simple_dag:get_simple_dag"


class TestDryRun:
    def test_discovers_the_dag_and_writes_nothing(
        self, default_in_memory_fs_target, fake_registry
    ):
        result = runner.invoke(cli, ["build", SIMPLE_DAG, "--dry-run", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        root = get_simple_dag()
        assert payload["roots"] == [str(root.id)]
        # root, parent, two leaves; post-order puts the root last.
        assert len(payload["tasks"]) == 4
        assert payload["tasks"][-1]["task_id"] == str(root.id)
        assert payload["to_run"] == 4
        assert fake_registry.calls == []

    def test_renders_a_table(self, default_in_memory_fs_target):
        result = runner.invoke(cli, ["build", SIMPLE_DAG, "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "4 task(s): 4 to run" in result.output

    def test_resolves_roots_under_the_requested_profile(
        self, default_in_memory_fs_target, monkeypatch
    ):
        """--dry-run used to return before --stardag-profile was applied, and
        roots were resolved (imported/constructed) before entering the
        profile context — so a dry run never actually walked targets under
        the requested profile."""
        import stardag._cli.build as build_module

        real_resolve_roots = build_module.resolve_roots
        seen: list[str | None] = []

        def spy(refs, params):
            seen.append(os.environ.get("STARDAG_PROFILE"))
            return real_resolve_roots(refs, params)

        monkeypatch.setattr(build_module, "resolve_roots", spy)
        result = runner.invoke(
            cli,
            [
                "build",
                SIMPLE_DAG,
                "--dry-run",
                "--stardag-profile",
                "a-test-profile",
            ],
        )
        assert result.exit_code == 0, result.output
        assert seen == ["a-test-profile"]
        # restored afterward
        assert os.environ.get("STARDAG_PROFILE") is None

    def test_settings_are_applied_before_roots_are_resolved(
        self, default_in_memory_fs_target
    ):
        """--settings must be in the environment while ``resolve_roots``
        imports and constructs the roots (a root factory may read it), not
        only later inside the build itself -- for both --dry-run and the
        real build."""
        from stardag.utils.testing.simple_dag import LEAF_FROM_ENV_VAR, LeafTask

        result = runner.invoke(
            cli,
            [
                "build",
                "stardag.utils.testing.simple_dag:leaf_from_env",
                "--settings",
                f"{LEAF_FROM_ENV_VAR}=from-settings",
                "--dry-run",
                "--json",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        expected = LeafTask(param_a=1, param_b="from-settings")
        assert payload["roots"] == [str(expected.id)]
        # restored afterward
        assert os.environ.get(LEAF_FROM_ENV_VAR) is None

    def test_bare_resume_installs_the_resumed_builds_stored_settings(
        self, default_in_memory_fs_target, fake_registry
    ):
        """A bare ``--resume`` (no --settings) must resolve the resumed
        build's own stored settings and install them before resolve_roots
        runs, the same way an explicit --settings does -- otherwise a root
        factory that reads the environment (like ``leaf_from_env``)
        constructs roots under the ambient environment instead of the
        resumed build's, which can produce different task ids/graph and
        get the resume rejected, or acted on under the wrong scope."""
        from stardag.build._registration import register_plan_aio, walk_aio
        from stardag.registry import registry_provider
        from stardag.utils.testing.helper_tasks import SyncOnlyTask
        from stardag.utils.testing.simple_dag import LEAF_FROM_ENV_VAR, LeafTask

        placeholder = SyncOnlyTask(name="placeholder")
        build_id = fake_registry.build_create(root_task_ids=[str(placeholder.id)]).id
        deployment_id = fake_registry.add_deployment()
        walk = asyncio.run(walk_aio([placeholder]))
        asyncio.run(
            register_plan_aio(
                fake_registry,
                build_id,
                walk,
                deployment_id=deployment_id,
                settings={LEAF_FROM_ENV_VAR: "from-stored"},
            )
        )

        calls_before = len(fake_registry.calls)
        with registry_provider.override(fake_registry):
            result = runner.invoke(
                cli,
                [
                    "build",
                    "stardag.utils.testing.simple_dag:leaf_from_env",
                    "--resume",
                    str(build_id),
                    "--dry-run",
                    "--json",
                ],
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        expected = LeafTask(param_a=1, param_b="from-stored")
        assert payload["roots"] == [str(expected.id)]
        # The payload must report the settings actually installed for the
        # walk (the resumed build's stored ones), not the empty `checked`
        # that a bare resume leaves behind -- otherwise the JSON claims a
        # plan made under no settings while the walk ran under stored ones.
        assert payload["settings"] == {LEAF_FROM_ENV_VAR: "from-stored"}
        # A bare resume's dry run MAY read the build's stored settings (one
        # frontier read, no more) -- the documented exception to
        # --dry-run's "no registry call" contract -- but writes nothing.
        assert fake_registry.methods_called()[calls_before:] == ["build_get_frontier"]
        # restored afterward
        assert os.environ.get(LEAF_FROM_ENV_VAR) is None


class TestRealBuild:
    """Non-dry-run: ``sd.build`` actually runs. The CLI's own
    ``resident_settings`` context (installed for ``resolve_roots``) must
    not still be open when ``sd.build`` starts -- it installs ``to_install``
    again itself, for the build's own duration."""

    def test_real_build_with_explicit_settings_completes(
        self, default_in_memory_fs_target, fake_registry
    ):
        from stardag.registry import registry_provider

        with registry_provider.override(fake_registry):
            result = runner.invoke(
                cli,
                [
                    "build",
                    "stardag.utils.testing.simple_dag:leaf_from_env",
                    "--settings",
                    "SIMPLE_DAG_LEAF_FROM_ENV_PARAM_B=explicit",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output
        assert os.environ.get("SIMPLE_DAG_LEAF_FROM_ENV_PARAM_B") is None

    def test_bare_resume_does_not_re_resolve_settings_a_second_time(
        self, default_in_memory_fs_target, fake_registry, monkeypatch
    ):
        """A bare ``--resume`` installs the resumed build's stored settings
        once (for ``resolve_roots``) and must hand that same value to
        ``sd.build`` rather than leaving it to resolve them again from the
        registry. A second, independent read could in principle answer
        differently -- e.g. another trigger landing between the two -- and
        the old code held the CLI's own ``resident_settings`` context open
        through the dispatch, so that later, different read collided with
        it and refused the build with ``SettingsError`` before a single
        task ran. Simulate the race by making the registry's settings
        lookup answer differently on this second read; only the buggy path
        reaches it a second time at all."""
        from stardag.build._registration import register_plan_aio, walk_aio
        from stardag.registry import registry_provider
        from stardag.registry._models import SettingsInfo
        from stardag.utils.testing.simple_dag import LEAF_FROM_ENV_VAR, LeafTask

        registry = fake_registry
        monkeypatch.setenv(LEAF_FROM_ENV_VAR, "from-stored")
        root = LeafTask(param_a=1, param_b="from-stored")
        build_id = registry.build_create(root_task_ids=[str(root.id)]).id
        deployment_id = registry.add_deployment()
        walk = asyncio.run(walk_aio([root]))
        asyncio.run(
            register_plan_aio(
                registry,
                build_id,
                walk,
                deployment_id=deployment_id,
                settings={LEAF_FROM_ENV_VAR: "from-stored"},
            )
        )
        monkeypatch.delenv(LEAF_FROM_ENV_VAR, raising=False)

        async def changed_settings_get_aio(settings_hash: str) -> SettingsInfo:
            return SettingsInfo(
                hash=settings_hash, body={LEAF_FROM_ENV_VAR: "changed-in-between"}
            )

        monkeypatch.setattr(registry, "settings_get_aio", changed_settings_get_aio)

        with registry_provider.override(registry):
            result = runner.invoke(
                cli,
                [
                    "build",
                    "stardag.utils.testing.simple_dag:leaf_from_env",
                    "--resume",
                    str(build_id),
                ],
            )
        assert result.exit_code == 0, result.output
        assert "1 succeeded" in result.output


class TestRefusals:
    def test_reserved_settings_keys_are_refused(self):
        result = runner.invoke(
            cli, ["build", SIMPLE_DAG, "--settings", "STARDAG_X=1", "--dry-run"]
        )
        assert result.exit_code == 1
        assert "STARDAG_" in result.output

    def test_reactive_needs_an_app(self):
        result = runner.invoke(cli, ["build", SIMPLE_DAG, "--reactive"])
        assert result.exit_code == 1
        assert "--reactive needs --app" in result.output


class TestResolveRoots:
    def test_a_task_class_takes_params(self):
        (task,) = resolve_roots(
            ["stardag.utils.testing.simple_dag:LeafTask"],
            parse_params(["param_a=3", "param_b=x"]),
        )
        assert task == LeafTask(param_a=3, param_b="x")

    def test_params_without_a_class_are_refused(self):
        with pytest.raises(RefError):
            resolve_roots([SIMPLE_DAG], {"x": 1})

    def test_a_non_task_is_refused(self):
        with pytest.raises(RefError):
            resolve_roots(["json:dumps"], {})

    def test_a_missing_attribute_is_refused(self):
        with pytest.raises(RefError):
            resolve_roots(["stardag.utils.testing.simple_dag:nope"], {})
