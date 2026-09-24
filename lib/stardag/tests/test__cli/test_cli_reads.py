"""The CLI on the v2 reads, against the in-memory registry: ``tasks list``,
``tasks show`` (claim holder, executions, events), ``builds list`` paging,
``builds show`` and ``builds frontier`` additions, ``deployments show``
and ``executions list --task/--include-ended``."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from stardag._cli import app as cli
from stardag.testing._registry_state import Event

runner = CliRunner(env={"COLUMNS": "240"})


def invoke(*args: object):
    return runner.invoke(cli, [str(a) for a in args])


class TestTasksList:
    def test_status_filter_names_the_claim_holder(self, fake_registry, running_build):
        result = invoke("tasks", "list", "--status", "running", "--json")
        assert result.exit_code == 0, result.output
        (task,) = json.loads(result.stdout)["tasks"]
        assert task["task_id"] == str(running_build.leaf.id)
        assert task["claim_build_id"] == str(running_build.build_id)

    def test_pages_with_a_cursor(self, fake_registry, running_build):
        first = invoke("tasks", "list", "--limit", "1")
        assert first.exit_code == 0, first.output
        assert "Next page: --cursor" in first.output
        page = json.loads(invoke("tasks", "list", "--limit", "1", "--json").stdout)
        rest = json.loads(
            invoke("tasks", "list", "--cursor", page["next_cursor"], "--json").stdout
        )
        ids = [t["task_id"] for t in page["tasks"] + rest["tasks"]]
        assert sorted(ids) == sorted(
            [str(running_build.leaf.id), str(running_build.root.id)]
        )

    def test_help_says_which_v1_filters_are_gone(self):
        assert "--older-than" in invoke("tasks", "list", "--help").output


class TestTasksShow:
    def test_claim_executions_and_events(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        result = invoke("tasks", "show", leaf)
        assert result.exit_code == 0, result.output
        assert f"build {running_build.build_id}" in result.output
        assert "(live)" in result.output
        assert str(running_build.execution_id) in result.output
        assert "TASK_STARTED" in result.output
        assert "not served" not in result.output

    def test_include_ended_lists_ended_executions(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        fake_registry.member_complete(
            running_build.plan_id, leaf, execution_id=running_build.execution_id
        )
        unended = json.loads(invoke("tasks", "show", leaf, "--json").stdout)
        assert unended["executions"] == []
        assert unended["claim_build_id"] is None
        ended = json.loads(
            invoke("tasks", "show", leaf, "--include-ended", "--json").stdout
        )
        assert [e["outcome"] for e in ended["executions"]] == ["completed"]

    def test_a_structure_divergence_is_called_out(self, fake_registry, running_build):
        root = str(running_build.root.id)
        fake_registry.events.append(
            Event(
                "TASK_STRUCTURE_DIVERGED",
                root,
                plan_id=running_build.plan_id,
                detail={"added_upstreams": ["h-new"]},
            )
        )
        result = invoke("tasks", "show", root, "--events", "0")
        assert result.exit_code == 0, result.output
        assert "Structure diverged 1 time(s)" in result.output
        assert "h-new" in result.output
        payload = json.loads(
            invoke("tasks", "show", root, "--events", "0", "--json").stdout
        )
        # --events bounds the JSON listing too; divergences come whole.
        assert payload["events"] == []
        (event,) = payload["structure_diverged"]
        assert event["event_metadata"] == {"added_upstreams": ["h-new"]}


class TestBuilds:
    def test_list_pages_with_total_and_cursor(self, fake_registry):
        for _ in range(3):
            fake_registry.build_create(root_task_ids=["t"])
        first = json.loads(invoke("builds", "list", "-n", "2", "--json").stdout)
        assert (len(first["builds"]), first["total"]) == (2, 3)
        rest = json.loads(
            invoke("builds", "list", "--cursor", first["next_cursor"], "--json").stdout
        )
        assert len(rest["builds"]) == 1 and rest["next_cursor"] is None
        rendered = invoke("builds", "list", "-n", "2")
        assert "Showing 2 of 3" in rendered.output
        assert "Last active" in rendered.output
        assert "by last activity" in rendered.output

    def test_reactive_app_is_an_alias_of_app(self, fake_registry, running_build):
        fake_registry.build_set_reactive_meta(running_build.build_id, app_name="a1")
        fake_registry.build_create(root_task_ids=["x"])
        for flag in ("--app", "--reactive-app"):
            payload = json.loads(invoke("builds", "list", flag, "a1", "--json").stdout)
            assert [b["id"] for b in payload["builds"]] == [str(running_build.build_id)]

    def test_show_the_failure_reason_and_last_activity(
        self, fake_registry, running_build
    ):
        fake_registry.build_fail(running_build.build_id, "a root was excluded")
        result = invoke("builds", "show", running_build.build_id)
        assert result.exit_code == 0, result.output
        assert "a root was excluded" in result.output
        assert "Last active" in result.output
        payload = json.loads(
            invoke("builds", "show", running_build.build_id, "--json").stdout
        )
        assert payload["error_message"] == "a root was excluded"

    def test_frontier_needs_tick_counts_and_roots(self, fake_registry, running_build):
        fake_registry.build_set_reactive_meta(running_build.build_id, app_name="a1")
        fake_registry.build_notify(running_build.build_id)
        result = invoke("builds", "frontier", running_build.build_id)
        assert result.exit_code == 0, result.output
        assert "Needs tick" in result.output
        assert "pending=1, running=1" in result.output
        assert "0/1 completed" in result.output
        payload = json.loads(
            invoke("builds", "frontier", running_build.build_id, "--json").stdout
        )
        assert payload["needs_tick"] is True
        assert payload["member_counts"] == {"pending": 1, "running": 1}
        assert (payload["roots_completed"], payload["roots_total"]) == (0, 1)
        # The frontier read leaves the flag for the tick.
        assert fake_registry.builds[running_build.build_id].needs_tick


class TestDeploymentsShow:
    def test_show(self, fake_registry, running_build):
        result = invoke("deployments", "show", running_build.deployment_id, "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["id"] == str(running_build.deployment_id)
        assert payload["kind"] == "local"
        rendered = invoke("deployments", "show", running_build.deployment_id)
        assert "cli-test" in rendered.output

    def test_an_unknown_deployment(self, fake_registry):
        result = invoke("deployments", "show", "00000000-0000-0000-0000-000000000000")
        assert result.exit_code == 1


class TestExecutionsList:
    def test_by_task_and_include_ended(self, fake_registry, running_build):
        leaf = str(running_build.leaf.id)
        fake_registry.member_complete(
            running_build.plan_id, leaf, execution_id=running_build.execution_id
        )
        unended = json.loads(
            invoke("executions", "list", "--task", leaf, "--json").stdout
        )
        assert unended["executions"] == []
        ended = json.loads(
            invoke(
                "executions", "list", "--task", leaf, "--include-ended", "--json"
            ).stdout
        )
        assert [e["id"] for e in ended["executions"]] == [
            str(running_build.execution_id)
        ]
        by_build = json.loads(
            invoke(
                "executions",
                "list",
                "--build",
                running_build.build_id,
                "--include-ended",
                "--json",
            ).stdout
        )
        assert len(by_build["executions"]) == 1
        rendered = invoke("executions", "list", "--task", leaf, "--include-ended")
        assert "completed" in rendered.output

    def test_exactly_one_of_build_and_task(self, fake_registry, running_build):
        assert invoke("executions", "list").exit_code == 1
        both = invoke(
            "executions",
            "list",
            "--build",
            running_build.build_id,
            "--task",
            running_build.leaf.id,
        )
        assert both.exit_code == 1
        orphans = invoke(
            "executions",
            "list",
            "--task",
            running_build.leaf.id,
            "--not-in-current-plan",
        )
        assert orphans.exit_code == 1
