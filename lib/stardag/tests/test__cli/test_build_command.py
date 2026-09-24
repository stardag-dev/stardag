"""``stardag build``: root resolution from ``module:attr``, settings
validation, and ``--dry-run`` on a small DAG."""

from __future__ import annotations

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
