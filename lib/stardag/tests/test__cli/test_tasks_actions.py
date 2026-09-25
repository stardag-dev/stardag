"""``stardag tasks retry`` and ``tasks cancel``: the confirmation prompt
and ``--yes``, and ``--build`` defaulting to the build holding the task's
claim (``claim_build_id`` from ``GET /tasks/{id}``)."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from stardag._cli import app as cli

runner = CliRunner(env={"COLUMNS": "240"})


def invoke(*args: object, input: str | None = None):
    return runner.invoke(cli, [str(a) for a in args], input=input)


class TestConfirmation:
    def test_cancel_asks_and_a_no_changes_nothing(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        result = invoke("tasks", "cancel", leaf, input="n\n")
        assert result.exit_code == 1
        assert f"Cancel task {leaf} in build {running_build.build_id}?" in (
            result.output
        )
        assert fake_registry.status_of(leaf) == "running"
        assert not fake_registry.called("member_cancel")

    def test_cancel_proceeds_on_yes_at_the_prompt(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        result = invoke("tasks", "cancel", leaf, input="y\n")
        assert result.exit_code == 0, result.output
        assert fake_registry.status_of(leaf) == "cancelled"

    def test_retry_asks_too(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        build = str(running_build.build_id)
        invoke("tasks", "cancel", leaf, "--yes")
        result = invoke("tasks", "retry", leaf, "--build", build, input="n\n")
        assert result.exit_code == 1
        assert "to PENDING" in result.output
        assert fake_registry.status_of(leaf) == "cancelled"

    def test_json_without_yes_is_refused_before_any_call(
        self, fake_registry, running_build
    ):
        leaf = str(running_build.leaf.id)
        before = len(fake_registry.calls)
        result = invoke("tasks", "cancel", leaf, "--json")
        assert result.exit_code == 1
        assert "pass --yes" in result.output
        assert len(fake_registry.calls) == before


class TestBuildDefault:
    def test_cancel_defaults_to_the_claim_holder(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        result = invoke("tasks", "cancel", leaf, "--yes", "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["build_id"] == str(running_build.build_id)
        assert payload["status"] == "cancelled"

    def test_build_overrides_the_default(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        other = fake_registry.build_create(root_task_ids=["x"]).id
        result = invoke("tasks", "cancel", leaf, "--build", other, "--yes")
        # The other build has no plan: the override was used, not the holder.
        assert result.exit_code == 1
        assert f"build {other} has no active plan" in result.output
        assert fake_registry.status_of(leaf) == "running"

    def test_a_task_without_a_claim_needs_build(self, fake_registry, running_build):
        root = str(running_build.root.id)
        result = invoke("tasks", "retry", root, "--yes")
        assert result.exit_code == 1
        assert "holds no claim" in result.output
        assert "--build" in result.output

    def test_help_says_where_the_default_comes_from(self):
        for command in ("retry", "cancel"):
            result = invoke("tasks", command, "--help")
            assert "claim_build_id" in result.output
            assert "--yes" in result.output
