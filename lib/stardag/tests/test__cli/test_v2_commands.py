"""The v2 CLI groups against the in-memory registry: ``builds``
(list/show/complete/fail), ``executions``, ``plans``, ``deployments`` and
``tasks``; ``stardag build`` in ``test_build_command.py``."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from stardag._cli import app as cli
from stardag.artifact import MarkdownArtifact
from stardag.exceptions import APIError

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

    def test_list_orders_by_last_active_at_not_insertion_order(self, fake_registry):
        """The server orders GET /builds most-recently-active first
        (Build.last_active_at.desc()). A build resumed after a newer one
        was created must sort ahead of it — insertion order alone gets
        this backwards."""
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ticks = iter(t0 + timedelta(seconds=i) for i in range(10))
        fake_registry.clock = lambda: next(ticks)

        older = fake_registry.build_create(root_task_ids=["a"]).id  # t=0
        newer = fake_registry.build_create(root_task_ids=["b"]).id  # t=1
        fake_registry.build_cancel(older)  # t=2
        fake_registry.build_cancel(newer)  # t=3
        # Touch `older` last: it should now sort ahead of `newer`.
        fake_registry.build_resume(older)  # t=4

        result = invoke("builds", "list", "--json")
        assert result.exit_code == 0, result.output
        ids = [b["id"] for b in json.loads(result.stdout)["builds"]]
        assert ids == [str(older), str(newer)]

    def test_list_running_orders_by_last_active_at_not_insertion_order(
        self, fake_registry
    ):
        """``build_list_running`` (used by the reactive watchdog, not the
        CLI) must delegate to ``build_list`` rather than scan
        ``self.builds`` in insertion order, or it drifts from both the
        real client (which delegates the same way) and ``builds list``
        above. Both builds stay "running" throughout, so this also
        exercises the status filter ``build_list_running`` adds on top of
        the shared ordering: created in insertion order older-then-newer,
        the most-recently-active-first order puts `newer` first --
        insertion order alone gets this backwards."""
        from datetime import datetime, timedelta, timezone

        t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        ticks = iter(t0 + timedelta(seconds=i) for i in range(10))
        fake_registry.clock = lambda: next(ticks)

        older = fake_registry.build_create(root_task_ids=["a"]).id  # t=0
        newer = fake_registry.build_create(root_task_ids=["b"]).id  # t=1

        assert fake_registry.build_list_running() == [newer, older]
        assert fake_registry.build_list_running(limit=1) == [newer]

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
        # A listing row carries no "created" (only a create call answers it).
        assert all("created" not in r for r in rows)
        table = invoke("deployments", "list")
        assert "current" in table.output and "local" in table.output

    def test_rejects_an_unknown_kind(self, fake_registry):
        result = invoke("deployments", "list", "--kind", "cloud")
        assert result.exit_code == 1

    def test_a_local_deployment_is_never_current(self, fake_registry):
        """Local rows are born activated (design.md, "The deterministic
        scope"), but that never makes one current — the server's
        ``current_deployment_id`` returns ``None`` outright for
        ``kind=local``, and the fake must match it."""
        local_id = fake_registry.add_deployment(kind="local", code_id="d" * 20)
        modal_id = fake_registry.add_deployment(
            kind="modal", app_name="app", code_id="e" * 20
        )
        result = invoke("deployments", "list", "--json")
        assert result.exit_code == 0, result.output
        rows = {r["id"]: r for r in json.loads(result.stdout)["deployments"]}
        assert rows[str(local_id)]["is_current"] is False
        assert rows[str(modal_id)]["is_current"] is True
        # --current excludes it too, not just the marker in the listing.
        current_result = invoke("deployments", "list", "--current", "--json")
        current_ids = {
            r["id"] for r in json.loads(current_result.stdout)["deployments"]
        }
        assert str(local_id) not in current_ids
        assert str(modal_id) in current_ids

    def test_activation_conflict_mirrors_the_server(self, fake_registry):
        """The server raises 409 ``deployment_activation_conflict`` with
        every clashing field (services/deployments.py, activate_deployment)
        — not the singular ``field`` of a ``deployment_mismatch``, which is
        a different 409 (a yield's deployment_id not matching its plan)."""
        deployment_id = fake_registry.add_deployment(
            kind="modal", app_name="app", code_id="a" * 20, activated=False
        )
        fake_registry.deployment_activate(
            deployment_id, modal_app_id="ap-1", image_id="im-1"
        )
        with pytest.raises(APIError) as excinfo:
            fake_registry.deployment_activate(
                deployment_id, modal_app_id="ap-2", image_id="im-1"
            )
        err = excinfo.value
        assert err.status_code == 409
        assert err.code == "deployment_activation_conflict"
        assert err.payload is not None
        assert err.payload["fields"] == ["modal_app_id"]


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
        # The HTTP contract normalises a markdown body to {"content": ...}
        # (stardag.registry._api_routes._artifacts_body); the fake mirrors
        # it rather than returning the raw markdown string.
        assert payload["artifacts"][0]["body"] == {"content": "# hi"}
        assert "TASK_STRUCTURE_DIVERGED" in payload["notes"][0]
        # The real GET /tasks/{id}/artifacts response mints an `id` and
        # `created_at` for every row (services/artifacts.py); the fake must
        # match that contract rather than leaving both at their model
        # defaults (None).
        artifact = payload["artifacts"][0]
        assert artifact["id"] is not None
        assert artifact["created_at"] is not None

    def test_artifact_upsert_keeps_id_and_created_at_but_replaces_body(
        self, fake_registry, running_build
    ):
        """A re-upload of the same (type, name) is an upsert -- the server's
        `on_conflict_do_update` only touches `body_json` -- so `id` and
        `created_at` must survive unchanged while `body` is replaced."""
        leaf = str(running_build.leaf.id)
        fake_registry.task_upload_artifacts(
            running_build.plan_id,
            leaf,
            [MarkdownArtifact(name="report", body="# hi")],
        )
        (first,) = fake_registry.task_list_artifacts(leaf)

        fake_registry.task_upload_artifacts(
            running_build.plan_id,
            leaf,
            [MarkdownArtifact(name="report", body="# updated")],
        )
        (second,) = fake_registry.task_list_artifacts(leaf)

        assert second.id == first.id
        assert second.created_at == first.created_at
        assert second.body == {"content": "# updated"}

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
        assert payload["roots_excluded"] == [str(running_build.root.id)]
        assert payload["build_failed"] is True

    def test_exclude_reports_what_this_call_did(self, fake_registry, running_build):
        """Once a root is excluded, excluding another member no longer says
        "a root was excluded: the build failed" — the message follows the
        call, not the plan."""
        root, leaf = running_build.root.id, running_build.leaf.id
        first = invoke(
            "tasks", "exclude", running_build.plan_id, root, "--reason", "x", "--yes"
        )
        assert first.exit_code == 0, first.output
        assert f"reached root(s) {root}: the build failed" in first.output

        later = invoke(
            "tasks", "exclude", running_build.plan_id, leaf, "--reason", "y", "--yes"
        )
        assert later.exit_code == 0, later.output
        assert "Excluded 1 member(s)" in later.output
        assert "root" not in later.output and "failed" not in later.output
