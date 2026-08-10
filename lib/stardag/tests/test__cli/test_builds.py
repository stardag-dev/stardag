"""Tests for the `stardag builds` CLI.

Commands are exercised with typer's ``CliRunner`` against a mocked
registry client, so no network / real registry is required.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest import mock
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from stardag._cli.builds import app
from stardag.exceptions import APIError, NotFoundError, SDKVersionUnsupportedError
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

    def test_older_than_rejects_a_non_running_status(self):
        """Only RUNNING has a SQL predicate; the server 422s the rest."""
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["list", "--status", "failed", "--older-than", "24h"]
            )
        assert result.exit_code == 1
        assert "--status running" in result.output
        registry.build_list.assert_not_called()

    def test_older_than_allows_status_running(self):
        registry = _mock_registry(
            build_list=BuildListPage(builds=[_build()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(
                app, ["list", "--status", "running", "--older-than", "24h"]
            )
        assert result.exit_code == 0, result.output
        assert registry.build_list.call_args.kwargs["idle_for_seconds"] == 24 * 3600

    def test_warns_loudly_when_the_server_ignored_the_filter(self):
        """A CLI can be newer than its registry; FastAPI drops unknown params.

        The rows must NOT be filtered locally: the server paginated and
        counted without the filter, so cutting the page here would drop rows
        from a page already chosen wrong — under-reporting exactly the oldest
        builds, which are the population --older-than exists to find.
        """
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
        assert "does not support --older-than" in result.output
        assert "results below are unfiltered" in result.output
        # Both rows are still shown — nothing is silently dropped.
        assert "spring-otter-42" in result.output
        assert "fresh" in result.output

    def test_missing_activity_timestamp_counts_as_unsupported(self):
        """A server that cannot report the field cannot have filtered on it."""
        registry = _mock_registry(
            build_list=BuildListPage(
                builds=[_build(last_activity_at=None)], total=1, page=1, page_size=20
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert "does not support --older-than" in result.output

    def test_no_warning_when_the_server_honoured_the_filter(self):
        registry = _mock_registry(
            build_list=BuildListPage(builds=[_build()], total=1, page=1, page_size=20)
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--older-than", "24h"])
        assert result.exit_code == 0, result.output
        assert "does not support" not in result.output

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
        # The payload is the server's page verbatim — the warning went to
        # stderr and changed nothing about what was emitted.
        assert len(payload["builds"]) == 2
        assert payload["total"] == 2


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

    @pytest.mark.parametrize("in_build", [True, False])
    def test_guidance_is_by_status_not_by_plan_membership(self, in_build: bool):
        """Plan closure makes the blocker this build's own task, so what
        happens next follows from its status. The two-remedy split — one
        addressed to this build, one to the build that owns the task — is
        gone, and the same guidance is printed either way."""
        registry = _mock_registry(
            build_get_frontier=_frontier(
                blocked_by_external=[_blocker(blocking_in_build=in_build)]
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        output = " ".join(result.output.split())  # rich wraps the paragraph
        assert "What happens next depends on the status" in output
        assert "another build holds the execution claim" in output
        assert "re-trigger the build" in output
        assert "not in this build's task set" not in output

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

    def test_yes_alone_is_still_a_dry_run(self):
        """-y only silences the prompt; --apply is the sole switch to acting.

        On a command that is a dry run by default, `-y` alone turning into a
        cascade of cancellations is precisely the surprise a destructive
        command may not have.
        """
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=True))
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h", "--yes"])
        assert result.exit_code == 0, result.output
        assert registry.build_bulk_cancel.call_args.kwargs["dry_run"] is True
        assert "Dry run" in result.output

    def test_apply_with_yes_runs_unattended(self):
        registry = _mock_registry(build_bulk_cancel=_bulk_result(dry_run=False))
        with _patch_resolve(registry):
            # No input available: a prompt here would fail the invocation.
            result = runner.invoke(
                app, ["cleanup", "--older-than", "24h", "--apply", "--yes"]
            )
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

    def test_a_finished_build_is_not_called_possibly_stuck(self):
        """A terminal build has nothing actionable and nothing running because
        it is *over*, not because it is wedged.

        Found in a live E2E run: a build that had completed successfully was
        reported as "genuinely stuck (a tick will fail it)" — alarming, and
        exactly the kind of misleading output this command exists to replace.
        """
        registry = _mock_registry(
            build_get_frontier=_frontier(
                build_status="completed", actionable=[], running=[]
            )
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["frontier", BUILD_ID])
        assert result.exit_code == 0, result.output
        assert "genuinely stuck" not in result.output
        assert "not applicable" in result.output

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


class TestRegistryTooOld:
    """A registry predating an endpoint must not read as "not found".

    FastAPI answers an unknown path with the generic ``{"detail": "Not
    Found"}``, which this CLI otherwise renders as "resource not found" —
    so the user reads a version-skew problem as a bad build id and goes
    looking for a build that is fine. Both commands name the real cause.
    """

    def test_ticks_says_the_registry_is_too_old(self):
        registry = mock.MagicMock()
        registry.build_list_tick_summaries.side_effect = NotFoundError(
            "List tick summaries: resource not found", detail="Not Found"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert result.exit_code == 1
        assert "does not support 'stardag builds ticks'" in result.output
        assert "tick-summaries endpoint is missing" in result.output
        assert "Upgrade stardag-api" in result.output
        registry.close.assert_called_once()

    def test_cleanup_says_the_registry_is_too_old_and_offers_the_fallback(self):
        registry = mock.MagicMock()
        registry.build_bulk_cancel.side_effect = NotFoundError(
            "Bulk-cancel builds: resource not found", detail="Not Found"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["cleanup", "--older-than", "24h"])
        assert result.exit_code == 1
        assert "does not support 'stardag builds cleanup'" in result.output
        assert "bulk-cancel endpoint is missing" in result.output
        # A one-at-a-time workaround exists, so say so rather than leaving
        # the user with only "upgrade".
        assert "stardag builds cancel" in result.output
        registry.close.assert_called_once()

    def test_a_real_missing_build_still_reads_as_not_found(self):
        """The narrow check must not swallow app-level 404s."""
        registry = mock.MagicMock()
        registry.build_list_tick_summaries.side_effect = NotFoundError(
            "List tick summaries: resource not found", detail="Build not found"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["ticks", BUILD_ID])
        assert result.exit_code == 1
        assert "Build not found" in result.output
        assert "does not support" not in result.output

    def test_sdk_too_old_prints_the_servers_own_sentence(self):
        """The 426 message is authored server-side; print it, don't repr it."""
        message = (
            "This Stardag server requires stardag SDK 2.0.0 or newer, but this "
            "request came from stardag 1.2.3. Upgrade with: pip install "
            '--upgrade "stardag>=2.0.0"'
        )
        registry = mock.MagicMock()
        registry.build_list.side_effect = SDKVersionUnsupportedError(
            message=message, sdk_version="1.2.3", minimum_sdk_version="2.0.0"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "SDK too old for this registry" in result.output
        assert message in result.output
        registry.close.assert_called_once()
