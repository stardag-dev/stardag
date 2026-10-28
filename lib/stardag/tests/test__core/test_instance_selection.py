"""Choosing an instance when none is pinned: for a completed task, the
instance of the execution that completed it, even after a later build
re-registered the task under another deployment (a newer instance whose
non-significant fields never ran); otherwise the newest.

Against ``InMemoryRegistry``, seeded directly: no build, no registry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from typer.testing import CliRunner

import stardag as sd
from stardag._cli import app as cli
from stardag.registry._instance_select import select_instance
from stardag.testing import InMemoryRegistry
from stardag.testing._registry_state import ExecutionRow, PlanRow

sd.auto_namespace(__name__)


class Model(sd.Task[str]):
    size: int
    notes: Annotated[str, sd.StardagField(significant=False)] = ""

    def run(self):
        self._save("m")


RAN = Model(size=1, notes="the run that completed it")
LATER = Model(size=1, notes="re-registered by a later deployment")


def _registry(
    *, completed: bool = True, executed: bool = True, claim_outcome: str = "completed"
):
    registry = InMemoryRegistry()
    ran, later = uuid4(), uuid4()
    registry.add_instance(body=RAN.instance_body(), task_id=RAN.id, instance_id=ran)
    registry.add_instance(body=LATER.instance_body(), task_id=RAN.id, instance_id=later)
    if completed:
        registry.tasks[str(RAN.id)].status = "completed"
    if executed:
        plan = PlanRow(
            id=uuid4(),
            build_id=uuid4(),
            deployment_id=uuid4(),
            settings_hash="s",
            generation=1,
        )
        registry.plans[plan.id] = plan
        execution = ExecutionRow(
            id=uuid4(),
            task_id=str(RAN.id),
            plan_id=plan.id,
            instance_id=ran,
            started_at=datetime.now(timezone.utc),
            ended_at=datetime.now(timezone.utc),
            outcome="completed",
            claim_outcome=claim_outcome,
        )
        registry.executions[execution.id] = execution
    return registry, ran, later


def test_a_completed_task_selects_the_completing_executions_instance():
    registry, ran, _ = _registry()
    chosen = select_instance(registry, str(RAN.id))
    assert chosen.instance.id == ran
    assert chosen.explanation == f"instance {ran} (the execution that completed it)"


def test_without_a_completed_execution_the_newest_is_selected():
    registry, _, later = _registry(executed=False)
    chosen = select_instance(registry, str(RAN.id))
    assert chosen.instance.id == later
    assert "no execution recorded for its current completion" in chosen.explanation


def test_a_rejected_late_completion_report_is_not_the_completing_execution():
    """A late report records outcome "completed" but did not complete the
    task (its claim had moved on): not the instance that produced it."""
    registry, _, later = _registry(claim_outcome="taken_over")
    chosen = select_instance(registry, str(RAN.id))
    assert chosen.instance.id == later
    assert "no execution recorded for its current completion" in chosen.explanation


def test_a_task_not_completed_selects_the_newest():
    registry, _, later = _registry(completed=False)
    assert select_instance(registry, str(RAN.id)).instance.id == later


def test_from_registry_and_an_unpinned_ref_load_the_instance_that_ran():
    registry, _, _ = _registry()
    loaded = Model.from_registry(RAN.id, registry=registry)
    assert isinstance(loaded, Model) and loaded.notes == RAN.notes
    assert Model.ref(RAN.id).hydrate(registry).notes == RAN.notes


runner = CliRunner(env={"COLUMNS": "240"})


def _patched(registry, monkeypatch):
    for module in ("tasks", "tasks_actions"):
        monkeypatch.setattr(
            f"stardag._cli.{module}._resolve_registry", lambda *a: registry
        )


def test_tasks_ref_pins_the_completing_executions_instance(monkeypatch):
    registry, ran, _ = _registry()
    _patched(registry, monkeypatch)
    result = runner.invoke(cli, ["tasks", "ref", str(RAN.id)])
    assert result.exit_code == 0, result.output
    assert f'instance_id="{ran}"' in result.stdout
    assert "(the execution that completed it)" in result.stderr


def test_tasks_ref_instance_newest_overrides(monkeypatch):
    registry, _, later = _registry()
    _patched(registry, monkeypatch)
    result = runner.invoke(cli, ["tasks", "ref", str(RAN.id), "--instance", "newest"])
    assert result.exit_code == 0, result.output
    assert f'instance_id="{later}"' in result.stdout
    assert "--instance newest" in result.stderr


def test_tasks_ref_write_records_the_selection(monkeypatch, tmp_path):
    registry, ran, _ = _registry()
    _patched(registry, monkeypatch)
    result = runner.invoke(
        cli,
        ["tasks", "ref", str(RAN.id), "--write", "--store", str(tmp_path), "--json"],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["selection"] == "completed_execution"
    (path,) = list(tmp_path.rglob("*.json"))
    (source,) = json.loads(path.read_text())["sources"]
    assert source["instance_id"] == str(ran)
    assert source["selection"] == "completed_execution"


def test_tasks_show_names_the_completing_executions_instance(monkeypatch):
    registry, ran, _ = _registry()
    _patched(registry, monkeypatch)
    result = runner.invoke(cli, ["tasks", "show", str(RAN.id)])
    assert result.exit_code == 0, result.output
    assert f"which ran instance {ran}" in result.stdout
    payload = json.loads(
        runner.invoke(cli, ["tasks", "show", str(RAN.id), "--json"]).stdout
    )
    assert payload["completed_instance_id"] == str(ran)


def test_an_unpinned_ref_prefers_the_stored_completing_instance(tmp_path):
    from stardag._core.task_store_write import write_from_registry
    from stardag.config.project import ProjectConfig, project_config_override

    registry, ran, later = _registry()
    with project_config_override(ProjectConfig(task_store=tmp_path)):
        write_from_registry(RAN.id, instance_id=later, registry=registry)
        write_from_registry(RAN.id, registry=registry)  # chooses `ran`
        sd.clear_hydration_memo()
        assert Model.ref(RAN.id).hydrate().notes == RAN.notes


def test_a_completion_observed_after_an_invalidation_is_not_the_old_executions():
    """The task was invalidated and then observed complete at discovery:
    its completed_at is later than the old execution's end, which did not
    produce the current output. The newest instance is used, and tasks show
    names no completing execution."""
    from datetime import timedelta

    registry, _, later = _registry()
    (execution,) = registry.executions.values()
    assert execution.ended_at is not None
    registry.tasks[str(RAN.id)].completed_at = execution.ended_at + timedelta(hours=1)
    chosen = select_instance(registry, str(RAN.id))
    assert chosen.instance.id == later
    assert "no execution recorded for its current completion" in chosen.explanation


def test_the_completing_execution_matches_its_completion_time():
    registry, ran, _ = _registry()
    (execution,) = registry.executions.values()
    registry.tasks[str(RAN.id)].completed_at = execution.ended_at
    assert select_instance(registry, str(RAN.id)).instance.id == ran


def test_a_pinned_load_keeps_the_missing_class_error_type():
    """A body naming an unimported class, loaded by instance id: the
    TaskClassNotRegisteredError subtype survives the wrapping."""
    import pytest

    registry = InMemoryRegistry()
    iid = uuid4()
    body = {**RAN.instance_body(), "__name": "NotImported"}
    registry.add_instance(body=body, task_id=RAN.id, instance_id=iid)
    with pytest.raises(sd.TaskClassNotRegisteredError):
        Model.from_registry(RAN.id, registry=registry, instance_id=iid)
