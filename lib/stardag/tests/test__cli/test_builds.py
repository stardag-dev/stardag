"""Tests for the `stardag builds` CLI.

Commands are exercised with typer's ``CliRunner`` against a mocked
registry client, so no network / real registry is required.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import uuid4

from typer.testing import CliRunner

from stardag._cli.builds import app
from stardag.exceptions import APIError
from stardag.registry import (
    BuildCancelResult,
    BuildFrontier,
    BuildListPage,
    BuildSummary,
    BulkCancelBuildRef,
    BulkCancelResult,
    FrontierExternalBlocker,
    FrontierTaskRef,
    TickSummaryRecord,
)

# Rich soft-wraps to the terminal width, which would split table cells and
# prose mid-word and make output assertions depend on the wrap point. A wide
# console keeps the rendered text on one line per row.
runner = CliRunner(env={"COLUMNS": "240"})

BUILD_ID = "11111111-1111-1111-1111-111111111111"
OTHER_BUILD_ID = "22222222-2222-2222-2222-222222222222"


def _mock_registry(**methods):
    registry = mock.MagicMock()
    for name, value in methods.items():
        getattr(registry, name).return_value = value
    return registry


def _patch_resolve(registry):
    return mock.patch("stardag._cli.builds._resolve_registry", return_value=registry)


def _ago(**kwargs) -> datetime:
    return datetime.now(timezone.utc) - timedelta(**kwargs)


def _build(**overrides) -> BuildSummary:
    data = {
        "id": BUILD_ID,
        "name": "spring-otter-42",
        "status": "running",
        "last_activity_at": _ago(days=3),
        "last_active_at": _ago(days=3),
        "created_at": _ago(days=4),
        "root_task_ids": ["root-task-1"],
    }
    data.update(overrides)
    return BuildSummary.model_validate(data)


def _frontier(**overrides) -> BuildFrontier:
    data = {
        "build_id": BUILD_ID,
        "build_status": "running",
        "needs_tick": False,
        "root_task_ids": ["root-task-1"],
        "roots": [],
        "status_counts": {"completed": 2, "suspended": 1},
        "actionable": [],
    }
    data.update(overrides)
    return BuildFrontier.model_validate(data)


def _blocker(**overrides) -> FrontierExternalBlocker:
    data = {
        "task_id": "blocked-task-1",
        "blocking_task_id": "blocking-task-9",
        "blocking_task_namespace": "demo.pipeline",
        "blocking_task_name": "TrainModel",
        "blocking_status": "running",
        "blocking_status_at": _ago(hours=5),
        "blocking_status_build_id": OTHER_BUILD_ID,
        "blocking_in_build": False,
    }
    data.update(overrides)
    return FrontierExternalBlocker.model_validate(data)


class TestList:
    def test_happy_path(self):
        registry = _mock_registry(
            build_list=BuildListPage(builds=[_build()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--status", "running"])
        assert result.exit_code == 0, result.output
        assert "spring-otter-42" in result.output
        registry.build_list.assert_called_once_with(
            page=1,
            page_size=20,
            status="running",
            reactive_app_name=None,
            idle_for_seconds=None,
        )
        registry.close.assert_called_once()

    def test_empty_prints_hint(self):
        registry = _mock_registry(build_list=BuildListPage())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "No builds match" in result.output
        assert "stardag builds list --status running" in result.output

    def test_older_than_converted_to_seconds(self):
        registry = _mock_registry(
            build_list=BuildListPage(builds=[_build()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert registry.build_list.call_args.kwargs["idle_for_seconds"] == 24 * 3600

    def test_older_than_rejects_bad_grammar(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "soon"])
        assert result.exit_code == 1
        assert "Invalid duration" in result.output
        registry.build_list.assert_not_called()

    def test_older_than_rejects_below_server_minimum(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "30s"])
        assert result.exit_code == 1
        assert "at least 60s" in result.output
        registry.build_list.assert_not_called()

    def test_older_than_reapplied_when_server_ignores_it(self):
        """An older server ignores the unknown param; the cut still holds."""
        fresh = _build(
            id=OTHER_BUILD_ID, name="fresh", last_activity_at=_ago(minutes=1)
        )
        registry = _mock_registry(
            build_list=BuildListPage(
                builds=[_build(), fresh], total=2, page=1, page_size=20
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert "spring-otter-42" in result.output
        assert "fresh" not in result.output
        assert "did not apply --older-than" in result.output

    def test_json_is_parseable_and_uncontaminated(self):
        registry = _mock_registry(
            build_list=BuildListPage(builds=[_build()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["total"] == 1
        assert payload["builds"][0]["name"] == "spring-otter-42"
        # No table borders / hints leaked into stdout.
        assert "─" not in result.stdout

    def test_json_stdout_clean_even_when_warning_is_emitted(self):
        """The server-ignored-filter warning must not break `| jq`."""
        fresh = _build(id=OTHER_BUILD_ID, last_activity_at=_ago(minutes=1))
        registry = _mock_registry(
            build_list=BuildListPage(
                builds=[_build(), fresh], total=2, page=1, page_size=20
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["list", "--older-than", "24h", "--json"], catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert len(payload["builds"]) == 1


class TestShow:
    def test_happy_path(self):
        registry = _mock_registry(build_get_summary=_build(reactive_app_name="my-app"))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "spring-otter-42" in result.output
        assert "my-app" in result.output
        assert "root-task-1" in result.output

    def test_rejects_non_uuid(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", "not-a-uuid"])
        assert result.exit_code == 1
        assert "not a valid build ID" in result.output
        registry.build_get_summary.assert_not_called()

    def test_json(self):
        registry = _mock_registry(build_get_summary=_build())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["show", BUILD_ID, "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout)["id"] == BUILD_ID


class TestFrontier:
    def test_renders_blocker_with_identity_age_and_owner(self):
        registry = _mock_registry(
            build_get_frontier=_frontier(blocked_by_external=[_blocker()])
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        # Human-readable identity, not just an opaque id.
        assert "demo.pipeline.TrainModel" in result.output
        assert "blocked-task-1" in result.output
        assert "running" in result.output
        assert "5h" in result.output  # how long it has been held up
        assert OTHER_BUILD_ID in result.output

    def test_out_of_build_blocker_gets_its_own_remedy(self):
        registry = _mock_registry(
            build_get_frontier=_frontier(
                blocked_by_external=[_blocker(blocking_in_build=False)]
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "not in this build's task set" in result.output
        assert "stardag tasks cancel" in result.output

    def test_in_build_blocker_gets_a_different_remedy(self):
        registry = _mock_registry(
            build_get_frontier=_frontier(
                blocked_by_external=[_blocker(blocking_in_build=True)]
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "in this build's task set" in result.output
        assert "a retry from here would not release" in result.output

    def test_truncation_is_surfaced(self):
        registry = _mock_registry(
            build_get_frontier=_frontier(
                blocked_by_external=[_blocker()],
                blocked_by_external_truncated=True,
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "truncated" in result.output

    def test_empty_list_on_a_progressing_build_does_not_claim_no_blockers(self):
        """The server only computes blockers for a stalled build."""
        registry = _mock_registry(
            build_get_frontier=_frontier(
                actionable=[
                    FrontierTaskRef(task_id="t1", latest_status="pending"),
                ],
                running=[FrontierTaskRef(task_id="t2", latest_status="running")],
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "not evaluated" in result.output
        assert "progressing" in result.output

    def test_empty_list_on_a_stalled_build_says_genuinely_stuck(self):
        registry = _mock_registry(build_get_frontier=_frontier())
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "No external blockers reported" in result.output
        assert "genuinely stuck" in result.output

    def test_json(self):
        registry = _mock_registry(
            build_get_frontier=_frontier(blocked_by_external=[_blocker()])
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID, "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["blocked_by_external"][0]["blocking_task_name"] == "TrainModel"


class TestTicks:
    def test_renders_unknown_summary_fields(self):
        """The summary is an open blob; a field this SDK never heard of shows."""
        registry = _mock_registry(
            build_list_tick_summaries=[
                TickSummaryRecord.model_validate(
                    {
                        "id": str(uuid4()),
                        "build_id": BUILD_ID,
                        "outcome": "lingered_out",
                        "summary": {
                            "outcome": "lingered_out",
                            "spawned": 2,
                            "some_future_counter": 7,
                            "claim_denied": 0,
                        },
                        "created_at": _ago(minutes=3),
                    }
                )
            ]
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "lingered_out" in result.output
        assert "some_future_counter=7" in result.output
        # Zero-valued counters are noise in a table.
        assert "claim_denied" not in result.output

    def test_empty_prints_hint(self):
        registry = _mock_registry(build_list_tick_summaries=[])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "No tick summaries" in result.output
        assert "reactively-scheduled" in result.output


class TestCancel:
    def test_cascade_reports_released_claims(self):
        registry = _mock_registry(
            build_cancel=BuildCancelResult.model_validate(
                {
                    "id": BUILD_ID,
                    "name": "spring-otter-42",
                    "cascaded_task_ids": ["task-a", "task-b"],
                    "cascaded_task_count": 2,
                }
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--cascade", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Released 2 task claim(s)" in result.output
        assert "task-a" in result.output
        registry.build_cancel.assert_called_once()
        assert registry.build_cancel.call_args.kwargs == {"cascade": True}

    def test_cascade_with_nothing_to_release_says_so(self):
        registry = _mock_registry(
            build_cancel=BuildCancelResult.model_validate(
                {"id": BUILD_ID, "name": "spring-otter-42"}
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--cascade", "-y"])
        assert result.exit_code == 0, result.output
        assert "No task claims to release" in result.output

    def test_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID], input="n\n")
        assert result.exit_code != 0
        registry.build_cancel.assert_not_called()

    def test_no_cascade_by_default(self):
        registry = _mock_registry(build_cancel=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cancel", BUILD_ID, "--yes"])
        assert result.exit_code == 0, result.output
        assert registry.build_cancel.call_args.kwargs == {"cascade": False}


def _bulk_result(*, dry_run: bool, **overrides) -> BulkCancelResult:
    data = {
        "dry_run": dry_run,
        "builds": [
            BulkCancelBuildRef.model_validate(
                {
                    "build_id": BUILD_ID,
                    "name": "spring-otter-42",
                    "last_activity_at": _ago(days=3),
                    "cascaded_task_ids": ["task-a", "task-b"],
                }
            )
        ],
        "build_count": 1,
        "task_count": 2,
    }
    data.update(overrides)
    return BulkCancelResult.model_validate(data)


class TestCleanup:
    def test_requires_a_filter(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup"])
        assert result.exit_code == 1
        assert "not a cleanup operation" in result.output
        registry.build_bulk_cancel.assert_not_called()

    def test_defaults_to_dry_run(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=True))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert "Dry run" in result.output
        assert "Would cancel 1 build(s), releasing 2 task claim(s)" in result.output
        assert "Re-run with --apply" in result.output
        kwargs = registry.build_bulk_cancel.call_args.kwargs
        assert kwargs["dry_run"] is True
        assert kwargs["idle_for_seconds"] == 24 * 3600
        assert kwargs["cascade"] is True

    def test_dry_run_shows_skip_reasons_and_truncation(self):
        registry = _mock_registry(
            build_bulk_cancel=_bulk_result(
                dry_run=True,
                skipped={OTHER_BUILD_ID: "not_running"},
                truncated=True,
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert OTHER_BUILD_ID in result.output
        assert "already terminal" in result.output
        assert "More builds matched than --limit" in result.output

    def test_apply_confirms_then_cancels(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=False))
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["cleanup", "--older-than", "24h", "--apply"], input="y\n"
            )
        assert result.exit_code == 0, result.output
        assert "Cancelled 1 build(s), releasing 2 task claim(s)" in result.output
        assert registry.build_bulk_cancel.call_args.kwargs["dry_run"] is False

    def test_apply_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["cleanup", "--older-than", "24h", "--apply"], input="n\n"
            )
        assert result.exit_code != 0
        registry.build_bulk_cancel.assert_not_called()

    def test_yes_implies_apply(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=False))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h", "--yes"])
        assert result.exit_code == 0, result.output
        assert registry.build_bulk_cancel.call_args.kwargs["dry_run"] is False

    def test_json_apply_refuses_to_prompt(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["cleanup", "--older-than", "24h", "--apply", "--json"]
            )
        assert result.exit_code == 1
        assert "refusing to prompt in --json mode" in result.output
        registry.build_bulk_cancel.assert_not_called()

    def test_json_dry_run_is_parseable(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=True))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h", "--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert payload["build_count"] == 1
        # Raw skip codes stay machine-readable in --json.
        assert "skipped" in payload

    def test_explicit_build_ids_need_no_older_than(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=True))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--build-id", BUILD_ID])
        assert result.exit_code == 0, result.output
        kwargs = registry.build_bulk_cancel.call_args.kwargs
        assert kwargs["build_ids"] == [BUILD_ID]
        assert kwargs["idle_for_seconds"] is None


class TestErrorHandling:
    def test_api_error_reported_and_client_closed(self):
        registry = mock.MagicMock()
        registry.build_list.side_effect = APIError(
            "List builds failed", status_code=403, detail="denied"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        registry.close.assert_called_once()

    def test_frontier_error_reported(self):
        registry = mock.MagicMock()
        registry.build_get_frontier.side_effect = APIError(
            "Get frontier failed", status_code=404, detail="Build not found"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 1
        assert "Error:" in result.output
        registry.close.assert_called_once()
