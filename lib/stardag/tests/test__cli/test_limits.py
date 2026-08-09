"""Tests for the `stardag concurrency-limits` CLI.

Commands are exercised with typer's ``CliRunner`` against a mocked
registry client, so no network / real registry is required.
"""

from unittest import mock

from typer.testing import CliRunner

from stardag._cli.limits import app
from stardag.exceptions import APIError

runner = CliRunner()


def _mock_registry(**methods):
    """Build a MagicMock APIRegistry with the given method return values."""
    registry = mock.MagicMock()
    for name, value in methods.items():
        getattr(registry, name).return_value = value
    return registry


def _patch_resolve(registry):
    """Patch ``_resolve_registry`` to return the given mock registry."""
    return mock.patch("stardag._cli.limits._resolve_registry", return_value=registry)


class TestList:
    def test_list_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_list=[
                {"key": "shards", "max_concurrent": 3},
                {"key": "gpu", "max_concurrent": 1},
            ]
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "shards" in result.output
        assert "gpu" in result.output
        registry.concurrency_limit_list.assert_called_once_with()
        registry.close.assert_called_once()

    def test_list_empty(self):
        registry = _mock_registry(concurrency_limit_list=[])
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 0, result.output
        assert "No concurrency limits" in result.output

    def test_list_with_holders(self):
        registry = _mock_registry(
            concurrency_limit_list=[{"key": "shards", "max_concurrent": 3}],
            concurrency_limit_holders={"key": "shards", "holders": [], "total": 2},
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list", "--holders"])
        assert result.exit_code == 0, result.output
        assert "Holders" in result.output
        # holder count fetched per key
        registry.concurrency_limit_holders.assert_called_once_with("shards", limit=1)
        assert "2" in result.output


class TestSet:
    def test_set_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_set={"key": "shards", "max_concurrent": 5}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["set", "shards", "5"])
        assert result.exit_code == 0, result.output
        assert "max_concurrent=5" in result.output
        registry.concurrency_limit_set.assert_called_once_with("shards", 5)

    def test_set_rejects_below_one(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["set", "shards", "0"])
        assert result.exit_code == 1
        assert "must be >= 1" in result.output
        registry.concurrency_limit_set.assert_not_called()


class TestDelete:
    def test_delete_with_yes(self):
        registry = _mock_registry(concurrency_limit_delete=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["delete", "shards", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Deleted concurrency limit 'shards'" in result.output
        registry.concurrency_limit_delete.assert_called_once_with("shards")

    def test_delete_aborts_without_confirm(self):
        registry = _mock_registry(concurrency_limit_delete=None)
        with _patch_resolve(registry):
            result = runner.invoke(app, ["delete", "shards"], input="n\n")
        assert result.exit_code != 0
        registry.concurrency_limit_delete.assert_not_called()


class TestHolders:
    def test_holders_happy_path(self):
        registry = _mock_registry(
            concurrency_limit_holders={
                "key": "shards",
                "holders": [
                    {
                        "task_id": "task-abc",
                        "task_namespace": "walkthrough",
                        "task_name": "ProcessShard",
                        "latest_status_at": "2026-07-14T10:00:00Z",
                        "latest_executor": "modal",
                    }
                ],
                "total": 1,
            }
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["holders", "shards"])
        assert result.exit_code == 0, result.output
        assert "task-abc" in result.output
        assert "walkthrough.ProcessShard" in result.output
        assert "modal" in result.output
        registry.concurrency_limit_holders.assert_called_once_with("shards", limit=100)

    def test_holders_empty(self):
        registry = _mock_registry(
            concurrency_limit_holders={"key": "shards", "holders": [], "total": 0}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["holders", "shards"])
        assert result.exit_code == 0, result.output
        assert "No current holders" in result.output


class TestEvict:
    def test_evict_with_yes(self):
        registry = _mock_registry(
            concurrency_limit_evict={"task_id": "task-abc", "status": "failed"}
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["evict", "shards", "task-abc", "--yes"])
        assert result.exit_code == 0, result.output
        assert "Evicted" in result.output
        assert "task-abc" in result.output
        registry.concurrency_limit_evict.assert_called_once_with("shards", "task-abc")

    def test_evict_aborts_without_confirm(self):
        registry = _mock_registry()
        with _patch_resolve(registry):
            result = runner.invoke(app, ["evict", "shards", "task-abc"], input="n\n")
        assert result.exit_code != 0
        registry.concurrency_limit_evict.assert_not_called()


class TestErrorHandling:
    def test_api_error_reported(self):
        registry = mock.MagicMock()
        registry.concurrency_limit_list.side_effect = APIError(
            "List concurrency limits failed", status_code=403, detail="denied"
        )
        with _patch_resolve(registry):
            result = runner.invoke(app, ["list"])
        assert result.exit_code == 1
        assert "Error:" in result.output
        # client still closed on failure
        registry.close.assert_called_once()
