"""``stardag concurrency-limits``: set/delete/list/holders against the
in-memory registry — mirrors the pattern ``test_v2_commands.py`` uses for
the other v2 CLI groups (``fake_registry``, the full CLI, ``--json``)."""

from __future__ import annotations

import asyncio
import json

from typer.testing import CliRunner

from stardag._cli import app as cli
from stardag._cli._output import short
from stardag.build._registration import new_id, register_plan_aio, walk_aio
from stardag.testing import InMemoryRegistry
from stardag.utils.testing.helper_tasks import SyncOnlyTask

runner = CliRunner(env={"COLUMNS": "240"})


def invoke(*args: str, **kwargs):
    return runner.invoke(cli, [str(a) for a in args], **kwargs)


def _start_holder(
    registry: InMemoryRegistry, monkeypatch, *, key: str, name: str
) -> tuple:
    """A second, independent running claim carrying ``key`` — for tests
    that need more than one holder of a limit."""
    monkeypatch.setenv("STARDAG_CODE_ID", "cli-test")
    deployment = registry.add_deployment(kind="local", code_id="cli-test")
    task = SyncOnlyTask(name=name)
    build_id = registry.build_create(root_task_ids=[str(task.id)]).id
    walk = asyncio.run(walk_aio([task]))
    plan = asyncio.run(
        register_plan_aio(
            registry, build_id, walk, deployment_id=deployment, settings={}
        )
    )
    execution_id = new_id()
    registry.member_start(
        plan.id, str(task.id), execution_id=execution_id, limit_keys=[key]
    )
    return build_id, plan.id, task, execution_id


class TestSet:
    def test_upserts_the_cap(self, fake_registry):
        result = invoke("concurrency-limits", "set", "gpu", "3")
        assert result.exit_code == 0, result.output
        assert fake_registry.limits == {"gpu": 3}
        assert "gpu" in result.output and "3" in result.output

        result = invoke("concurrency-limits", "set", "gpu", "1")
        assert result.exit_code == 0, result.output
        assert fake_registry.limits == {"gpu": 1}

    def test_zero_is_a_valid_cap(self, fake_registry):
        """v2's ``ConcurrencyLimitSet.max_concurrent`` is ``ge=0`` — 0
        blocks the key entirely, unlike v1's ``>= 1``."""
        result = invoke("concurrency-limits", "set", "gpu", "0")
        assert result.exit_code == 0, result.output
        assert fake_registry.limits == {"gpu": 0}

    def test_rejects_a_negative_cap_without_calling_the_registry(self, fake_registry):
        # ``--`` stops option parsing, or click reads ``-1`` as an unknown
        # option (a short-flag collision, not this command's own bug).
        result = invoke("concurrency-limits", "set", "gpu", "--", "-1")
        assert result.exit_code == 1
        assert fake_registry.limits == {}


class TestDelete:
    def test_aborts_without_confirm(self, fake_registry):
        fake_registry.limits["gpu"] = 2
        result = invoke("concurrency-limits", "delete", "gpu", input="n\n")
        assert result.exit_code != 0
        assert fake_registry.limits == {"gpu": 2}

    def test_yes_deletes(self, fake_registry):
        fake_registry.limits["gpu"] = 2
        result = invoke("concurrency-limits", "delete", "gpu", "--yes")
        assert result.exit_code == 0, result.output
        assert fake_registry.limits == {}

    def test_unknown_key_fails(self, fake_registry):
        result = invoke("concurrency-limits", "delete", "missing", "--yes")
        assert result.exit_code == 1
        assert "unknown_limit" in result.output or "Error" in result.output


class TestList:
    def test_empty_prints_a_hint(self, fake_registry):
        result = invoke("concurrency-limits", "list")
        assert result.exit_code == 0, result.output
        assert "No concurrency limits" in result.output
        assert "concurrency-limits set" in result.output

    def test_json_carries_in_use(self, fake_registry, running_build):
        fake_registry.limits["gpu"] = 2
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        result = invoke("concurrency-limits", "list", "--json")
        assert result.exit_code == 0, result.output
        limits = json.loads(result.stdout)["limits"]
        assert limits == [
            {"key": "gpu", "max_concurrent": 2, "in_use": 1, "holders": None}
        ]

    def test_table_renders_key_cap_and_in_use(self, fake_registry, running_build):
        fake_registry.limits["gpu"] = 2
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        result = invoke("concurrency-limits", "list")
        assert result.exit_code == 0, result.output
        assert "gpu" in result.output and "In use" in result.output

    def test_holders_flag_adds_a_table_per_key(self, fake_registry, running_build):
        fake_registry.limits["gpu"] = 2
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        result = invoke("concurrency-limits", "list", "--holders")
        assert result.exit_code == 0, result.output
        assert "Holders of 'gpu'" in result.output
        assert "SyncOnlyTask" in result.output
        assert short(str(running_build.leaf.id)) in result.output

    def test_holders_flag_json_includes_holder_detail(
        self, fake_registry, running_build
    ):
        fake_registry.limits["gpu"] = 2
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        result = invoke("concurrency-limits", "list", "--holders", "--json")
        assert result.exit_code == 0, result.output
        (limit,) = json.loads(result.stdout)["limits"]
        (holder,) = limit["holders"]
        assert holder["task_id"] == str(running_build.leaf.id)
        assert holder["build_id"] == str(running_build.build_id)
        assert holder["execution_id"] == str(running_build.execution_id)


class TestHolders:
    def test_lists_the_keys_holders_oldest_first(
        self, fake_registry, running_build, monkeypatch
    ):
        fake_registry.limits["gpu"] = 5
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        _, _, second, second_execution = _start_holder(
            fake_registry, monkeypatch, key="gpu", name="second-holder"
        )

        result = invoke("concurrency-limits", "holders", "gpu", "--json")
        assert result.exit_code == 0, result.output
        payload = json.loads(result.stdout)
        assert payload["total"] == 2
        ids = [h["task_id"] for h in payload["holders"]]
        # The fixture's leaf claimed first, so it is oldest-running.
        assert ids == [str(running_build.leaf.id), str(second.id)]

        table_result = invoke("concurrency-limits", "holders", "gpu")
        assert table_result.exit_code == 0, table_result.output
        assert short(str(running_build.leaf.id)) in table_result.output
        assert short(str(second.id)) in table_result.output
        assert str(second_execution) is not None  # sanity: fixture wired up

    def test_unconfigured_key_reports_no_limit(self, fake_registry):
        result = invoke("concurrency-limits", "holders", "missing")
        assert result.exit_code == 0, result.output
        assert "No concurrency limit 'missing' is configured" in result.output

    def test_no_holders_reports_empty(self, fake_registry):
        fake_registry.limits["gpu"] = 2
        result = invoke("concurrency-limits", "holders", "gpu")
        assert result.exit_code == 0, result.output
        assert "No current holders" in result.output

    def test_limit_truncates_and_hints(self, fake_registry, running_build, monkeypatch):
        fake_registry.limits["gpu"] = 5
        fake_registry.tasks[str(running_build.leaf.id)].limit_keys = {"gpu"}
        _start_holder(fake_registry, monkeypatch, key="gpu", name="second-holder")

        result = invoke("concurrency-limits", "holders", "gpu", "--limit", "1")
        assert result.exit_code == 0, result.output
        assert "Showing 1 of 2 holders" in result.output
