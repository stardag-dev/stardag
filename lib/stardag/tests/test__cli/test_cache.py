"""``stardag cache clear``: the active profile's partition, or every one
with ``--all``. Makes no registry call; the registry client is mocked at
``stardag._cli.cache._resolve_registry``."""

from __future__ import annotations

from unittest import mock
from uuid import uuid4

from typer.testing import CliRunner

from stardag._cli import app
from stardag.registry import TaskInstanceInfo, _instance_cache

runner = CliRunner(env={"COLUMNS": "240"})


def _entry(partition: str) -> None:
    _instance_cache.write(
        partition,
        TaskInstanceInfo(
            id=uuid4(),
            task_id="t",
            deployment_id=uuid4(),
            settings_hash="s",
            instance_hash="h",
            body={},
        ),
    )


def _patch_partition(partition: str | None):
    registry = mock.MagicMock()
    registry.instance_cache_partition.return_value = partition
    return mock.patch("stardag._cli.cache._resolve_registry", return_value=registry)


def test_clears_only_the_active_partition():
    _entry("mine")
    _entry("mine")
    _entry("theirs")
    with _patch_partition("mine"):
        result = runner.invoke(app, ["cache", "clear"])
    assert result.exit_code == 0, result.output
    assert "Removed 2 cached instance bodies" in result.output
    with _patch_partition("theirs"):
        assert (
            "Removed 1 cached instance body "
            in runner.invoke(app, ["cache", "clear"]).output
        )


def test_all_clears_every_partition_without_a_registry():
    _entry("mine")
    _entry("theirs")
    with mock.patch("stardag._cli.cache._resolve_registry") as resolve:
        result = runner.invoke(app, ["cache", "clear", "--all"])
    assert result.exit_code == 0, result.output
    assert "Removed 2 cached instance bodies" in result.output
    resolve.assert_not_called()


def test_a_profile_without_a_partition_points_at_all():
    with _patch_partition(None):
        result = runner.invoke(app, ["cache", "clear"])
    assert result.exit_code == 1
    assert "--all" in result.output
