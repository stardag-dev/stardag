"""``stardag builds tick`` and the wake-up every CLI write sends after it
lands (STA-123), against the in-memory registry and a recording spawner."""

from __future__ import annotations

import json
from unittest import mock

from typer.testing import CliRunner

from stardag._cli.builds import app as builds_app
from stardag._cli.tasks import app as tasks_app

runner = CliRunner(env={"COLUMNS": "240"})


def _reactive(registry, build_id, app_name: str = "app") -> None:
    registry.build_set_reactive_meta(build_id, app_name=app_name)


def _no_spawner():
    return mock.patch("stardag._cli.builds_tick.modal_spawner", return_value=None)


class TestTick:
    def test_spawns_a_tick_for_a_build_with_no_scheduler(
        self, fake_registry, running_build, tick_spawner
    ):
        _reactive(fake_registry, running_build.build_id)
        result = runner.invoke(
            builds_app, ["tick", str(running_build.build_id), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "spawned"
        assert tick_spawner.spawned == [(running_build.build_id, "app")]
        assert fake_registry.builds[running_build.build_id].needs_tick

    def test_leaves_a_live_scheduler_to_its_flag(
        self, fake_registry, running_build, tick_spawner
    ):
        _reactive(fake_registry, running_build.build_id)
        fake_registry.scheduler_lease_acquire(
            running_build.build_id, owner_id="tick", ttl_seconds=60
        )
        result = runner.invoke(
            builds_app, ["tick", str(running_build.build_id), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "scheduler_live"
        assert tick_spawner.spawned == []
        assert fake_registry.builds[running_build.build_id].needs_tick

    def test_a_build_that_is_not_reactive_is_not_spawned_for(
        self, fake_registry, running_build, tick_spawner
    ):
        result = runner.invoke(
            builds_app, ["tick", str(running_build.build_id), "--json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["outcome"] == "not_reactive"
        assert tick_spawner.spawned == []

    def test_a_failed_spawn_exits_non_zero(
        self, fake_registry, running_build, tick_spawner
    ):
        _reactive(fake_registry, running_build.build_id)
        tick_spawner.fail_with = RuntimeError("app not found")
        result = runner.invoke(builds_app, ["tick", str(running_build.build_id)])
        assert result.exit_code == 1
        assert "app not found" in result.output

    def test_without_modal_it_says_what_is_missing(self, fake_registry, running_build):
        with _no_spawner():
            result = runner.invoke(builds_app, ["tick", str(running_build.build_id)])
        assert result.exit_code == 1
        assert "modal" in result.output.lower()
        assert not fake_registry.called("build_notify")

    def test_flagged_drains_every_unserved_build(self, fake_registry, tick_spawner):
        flagged = []
        for app_name in ("a", "b"):
            build_id = fake_registry.build_create(root_task_ids=["x"]).id
            _reactive(fake_registry, build_id, app_name)
            fake_registry.builds[build_id].needs_tick = True
            flagged.append((build_id, app_name))
        result = runner.invoke(builds_app, ["tick", "--flagged", "--json"])
        assert result.exit_code == 0, result.output
        assert sorted(tick_spawner.spawned) == sorted(flagged)
        assert len(json.loads(result.stdout)["spawned"]) == 2

    def test_needs_exactly_one_of_an_id_and_flagged(self, fake_registry):
        assert runner.invoke(builds_app, ["tick"]).exit_code == 1
        both = runner.invoke(builds_app, ["tick", "--flagged", "x"])
        assert both.exit_code == 1


class TestWritesWakeWhatTheyChanged:
    def test_a_retry_starts_the_builds_tick(
        self, fake_registry, running_build, tick_spawner
    ):
        """The registry does not flag the build a write goes through, so
        before this a retried task sat PENDING with no tick to run it."""
        _reactive(fake_registry, running_build.build_id)
        fake_registry.member_fail(
            running_build.plan_id,
            str(running_build.leaf.id),
            execution_id=running_build.execution_id,
            error_message="boom",
        )
        fake_registry.builds[running_build.build_id].needs_tick = False
        result = runner.invoke(
            tasks_app,
            [
                "retry",
                str(running_build.leaf.id),
                "--build",
                str(running_build.build_id),
                "--yes",
            ],
        )
        assert result.exit_code == 0, result.output
        assert tick_spawner.spawned == [(running_build.build_id, "app")]
        assert "Spawned a scheduler tick" in result.output

    def test_a_cancelled_build_drains_its_neighbours(
        self, fake_registry, running_build, tick_spawner
    ):
        neighbour = fake_registry.build_create(root_task_ids=["x"]).id
        _reactive(fake_registry, neighbour, "other")
        fake_registry.builds[neighbour].needs_tick = True
        result = runner.invoke(
            builds_app, ["cancel", str(running_build.build_id), "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert tick_spawner.spawned == [(neighbour, "other")]

    def test_json_output_stays_one_document(
        self, fake_registry, running_build, tick_spawner
    ):
        neighbour = fake_registry.build_create(root_task_ids=["x"]).id
        _reactive(fake_registry, neighbour, "other")
        fake_registry.builds[neighbour].needs_tick = True
        result = runner.invoke(
            builds_app, ["cancel", str(running_build.build_id), "--yes", "--json"]
        )
        assert result.exit_code == 0, result.output
        json.loads(result.stdout)

    def test_without_modal_the_build_is_flagged_unstamped_and_the_fallback_named(
        self, fake_registry, running_build
    ):
        _reactive(fake_registry, running_build.build_id)
        fake_registry.member_fail(
            running_build.plan_id,
            str(running_build.leaf.id),
            execution_id=running_build.execution_id,
            error_message="boom",
        )
        with mock.patch("stardag._cli._wake.modal_spawner", return_value=None):
            result = runner.invoke(
                tasks_app,
                [
                    "retry",
                    str(running_build.leaf.id),
                    "--build",
                    str(running_build.build_id),
                    "--yes",
                ],
            )
        assert result.exit_code == 0, result.output
        assert "stardag builds tick" in result.output
        (notified,) = fake_registry.calls_to(
            "build_notify", build_id=running_build.build_id
        )
        assert notified["can_spawn"] is False
        # Unstamped: the next drain from anyone hands it out.
        assert [c.build_id for c in fake_registry.build_wake_candidates()] == [
            running_build.build_id
        ]

    def test_a_failing_wake_up_does_not_fail_the_write(
        self, fake_registry, running_build, tick_spawner
    ):
        _reactive(fake_registry, running_build.build_id)
        with mock.patch.object(
            fake_registry, "build_notify", side_effect=RuntimeError("down")
        ):
            result = runner.invoke(
                builds_app, ["cancel", str(running_build.build_id), "--yes"]
            )
        assert result.exit_code == 0, result.output
        assert fake_registry.builds[running_build.build_id].status == "cancelled"


class TestStalled:
    def _registry(self, stalled):
        registry = mock.MagicMock()
        registry.build_list_stalled.return_value = stalled
        return mock.patch(
            "stardag._cli.builds_tick._resolve_registry", return_value=registry
        ), registry

    def test_lists_what_the_registry_reports(self):
        from datetime import datetime, timezone
        from uuid import uuid4

        from stardag.registry import StalledBuild

        stalled = StalledBuild(
            build_id=uuid4(),
            reactive_app_name="app",
            reason="lease_lapsed",
            since=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        patch, registry = self._registry([stalled])
        with patch:
            result = runner.invoke(
                builds_app, ["stalled", "--older-than", "90s", "--json"]
            )
        assert result.exit_code == 0, result.output
        registry.build_list_stalled.assert_called_once_with(
            older_than_seconds=90, limit=200
        )
        payload = json.loads(result.stdout)
        assert payload["older_than_seconds"] == 90
        assert payload["builds"][0]["reason"] == "lease_lapsed"

    def test_nothing_stalled_says_so(self):
        patch, _ = self._registry([])
        with patch:
            result = runner.invoke(builds_app, ["stalled"])
        assert result.exit_code == 0, result.output
        assert "No reactive build" in result.output

    def test_a_bad_duration_is_a_usage_error(self):
        patch, registry = self._registry([])
        with patch:
            result = runner.invoke(builds_app, ["stalled", "--older-than", "soon"])
        assert result.exit_code == 1
        registry.build_list_stalled.assert_not_called()
