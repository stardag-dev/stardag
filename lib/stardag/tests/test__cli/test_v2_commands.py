"""The v2 CLI groups against the in-memory registry: ``builds``
(list/show/complete/fail), ``executions``, ``plans``, ``deployments`` and
``tasks``; ``stardag build`` in ``test_build_command.py``."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from stardag._cli import app as cli
from stardag.artifact import MarkdownArtifact

runner = CliRunner(env={"COLUMNS": "240"})


def invoke(*args: str):
    return runner.invoke(cli, [str(a) for a in args])


class TestBuilds:
    def test_list_filters_by_status_and_app(self, fake_registry, running_build):
        other = fake_registry.build_create(root_task_ids=["x"]).id
        fake_registry.build_cancel(other)
        result = invoke("builds", "list", "--status", "running", "--json")
        assert result.exit_code == 0, result.output
        ids = [b["id"] for b in json.loads(result.stdout)["builds"]]
        assert ids == [str(running_build.build_id)]
        (call,) = fake_registry.calls_to("build_list")
        assert call["status"] == "running"

    def test_show_names_the_active_plan_and_counts(self, fake_registry, running_build):
        result = invoke("builds", "show", running_build.build_id, "--json")
        assert result.exit_code == 0, result.output
        plan = json.loads(result.stdout)["active_plan"]
        assert plan["plan_id"] == str(running_build.plan_id)
        assert plan["deployment_id"] == str(running_build.deployment_id)
        assert plan["sealed"] is True
        assert plan["running"] == 1
        assert plan["unended_executions"] == 1
        assert plan["orphaned_executions"] == 0

    def test_show_renders(self, fake_registry, running_build):
        result = invoke("builds", "show", running_build.build_id)
        assert result.exit_code == 0, result.output
        assert str(running_build.plan_id) in result.output
        assert "1 running" in result.output

    def test_complete_is_refused_while_members_are_outstanding(
        self, fake_registry, running_build
    ):
        result = invoke("builds", "complete", running_build.build_id)
        assert result.exit_code == 1
        assert "plan_incomplete" in result.output
        forced = invoke("builds", "complete", running_build.build_id, "--force")
        assert forced.exit_code == 0, forced.output
        assert fake_registry.builds[running_build.build_id].status == "completed"

    def test_fail_records_the_message(self, fake_registry, running_build):
        result = invoke("builds", "fail", running_build.build_id, "-m", "why", "-y")
        assert result.exit_code == 0, result.output
        build = fake_registry.builds[running_build.build_id]
        assert (build.status, build.error_message) == ("failed", "why")


class TestExecutions:
    def test_list(self, fake_registry, running_build):
        result = invoke("executions", "list", "--build", running_build.build_id)
        assert result.exit_code == 0, result.output
        assert str(running_build.execution_id) in result.output
        assert "fc-leaf" in result.output

    def test_orphans_json(self, fake_registry, running_build):
        result = invoke(
            "executions",
            "list",
            "-b",
            running_build.build_id,
            "--not-in-current-plan",
            "--json",
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["executions"] == []


class TestPlans:
    def test_show_the_active_plan(self, fake_registry, running_build):
        result = invoke("plans", "show", running_build.plan_id, "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["active"] is True and payload["sealed"] is True
        assert payload["build_id"] == str(running_build.build_id)
        assert [r["task_id"] for r in payload["roots"]] == [str(running_build.root.id)]
        assert payload["outstanding"]["running"] == 1

    def test_show_renders(self, fake_registry, running_build):
        result = invoke("plans", "show", running_build.plan_id)
        assert result.exit_code == 0, result.output
        assert "SyncOnlyTask" in result.output


class TestDeployments:
    def test_list_marks_current_and_filters_by_kind(self, fake_registry):
        fake_registry.add_deployment(kind="modal", app_name="app", code_id="a" * 20)
        new = fake_registry.add_deployment(
            kind="modal", app_name="app", code_id="b" * 20
        )
        fake_registry.add_deployment(kind="local", code_id="c" * 20)
        result = invoke("deployments", "list", "--kind", "modal", "--json")
        assert result.exit_code == 0, result.output
        rows = json.loads(result.stdout)["deployments"]
        assert {r["kind"] for r in rows} == {"modal"}
        assert [r["id"] for r in rows if r["is_current"]] == [str(new)]
        table = invoke("deployments", "list")
        assert "current" in table.output and "local" in table.output

    def test_rejects_an_unknown_kind(self, fake_registry):
        result = invoke("deployments", "list", "--kind", "cloud")
        assert result.exit_code == 1


class TestTasks:
    def test_show_task_instances_and_artifacts(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        fake_registry.task_upload_artifacts(
            running_build.plan_id,
            leaf,
            [MarkdownArtifact(name="report", body="# hi")],
        )
        result = invoke("tasks", "show", leaf, "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["status"] == "running"
        assert payload["execution_id"] == str(running_build.execution_id)
        assert len(payload["instances"]) == 1
        assert [a["name"] for a in payload["artifacts"]] == ["report"]
        assert "TASK_STRUCTURE_DIVERGED" in payload["notes"][0]

    def test_check_observes_the_target_locally(self, fake_registry, running_build):
        root = str(running_build.root.id)
        result = invoke(
            "tasks", "check", root, "-m", "stardag.utils.testing.helper_tasks", "--json"
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["observed_complete"] is False
        assert payload["registry_status"] == "pending"
        assert payload["agrees"] is True and payload["reported"] is False

    def test_check_report_is_refused(self, fake_registry, running_build):
        result = invoke(
            "tasks", "check", running_build.root.id, "-m", "json", "--report"
        )
        assert result.exit_code == 1
        assert "trigger a build to let the registry observe" in result.output

    def test_cancel_then_retry_through_the_active_plan(
        self, fake_registry, running_build
    ):
        leaf = str(running_build.leaf.id)
        build = str(running_build.build_id)
        cancelled = invoke("tasks", "cancel", leaf, "--build", build)
        assert cancelled.exit_code == 0, cancelled.output
        assert fake_registry.status_of(leaf) == "cancelled"
        retried = invoke("tasks", "retry", leaf, "--build", build)
        assert retried.exit_code == 0, retried.output
        assert fake_registry.status_of(leaf) == "pending"

    def test_exclude_cascades_and_fails_the_build(self, fake_registry, running_build):
        result = invoke(
            "tasks",
            "exclude",
            running_build.plan_id,
            running_build.root.id,
            "--reason",
            "no longer wanted",
            "--yes",
            "--json",
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["excluded"] == [str(running_build.root.id)]
        assert payload["build_failed"] is True
