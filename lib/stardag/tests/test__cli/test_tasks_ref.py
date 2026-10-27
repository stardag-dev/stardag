"""``stardag tasks ref``: the import and ``Task.ref(...)`` call for a
registered task, always pinned to an instance. Against the in-memory
registry."""

from __future__ import annotations

import json
from uuid import uuid4

from typer.testing import CliRunner

import stardag as sd
from stardag._cli import app as cli
from stardag.testing._registry_state import InstanceRow, TaskRow

runner = CliRunner(env={"COLUMNS": "240"})


@sd.task(name="CliRefRenamedTask")
def add_one(x: int) -> int:
    return x + 1


def invoke(*args: object):
    return runner.invoke(cli, [str(a) for a in args])


def _instances_of(registry, task_id: str) -> list[str]:
    """Newest first, as the registry serves them."""
    return [
        str(row.id)
        for row in reversed(registry.instances.values())
        if row.task_id == task_id
    ]


def test_prints_the_import_and_a_pinned_ref(fake_registry, running_build):
    leaf = running_build.leaf
    result = invoke("tasks", "ref", leaf.id)
    assert result.exit_code == 0, result.output
    (newest, *_) = _instances_of(fake_registry, str(leaf.id))
    assert result.stdout.splitlines() == [
        "from stardag.utils.testing.helper_tasks import SyncOnlyTask",
        f'SyncOnlyTask.ref("{leaf.id}", instance_id="{newest}")',
    ]


def test_pins_the_named_instance(fake_registry, running_build):
    leaf = running_build.leaf
    older = uuid4()
    fake_registry.instances[older] = InstanceRow(
        id=older,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash="h",
        task_id=str(leaf.id),
        body=leaf.instance_body(),
    )
    result = invoke("tasks", "ref", leaf.id, "--instance", older, "--json")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["instance_id"] == str(older)
    assert payload["ref"] == (f'SyncOnlyTask.ref("{leaf.id}", instance_id="{older}")')
    assert payload["module"] == "stardag.utils.testing.helper_tasks"
    assert payload["module_located"] is True


def test_an_unknown_instance_is_refused(fake_registry, running_build):
    result = invoke("tasks", "ref", running_build.leaf.id, "--instance", uuid4())
    assert result.exit_code == 1
    assert "no instance" in result.output


def test_an_unknown_task_is_refused(fake_registry):
    result = invoke("tasks", "ref", uuid4())
    assert result.exit_code == 1


def test_an_unregistered_class_prints_no_import(fake_registry):
    task_id = str(uuid4())
    fake_registry.tasks[task_id] = TaskRow(
        task_id=task_id,
        task_namespace="some_project.models",
        task_name="TrainedModel",
        version="",
        output_uri=None,
    )
    instance_id = uuid4()
    fake_registry.instances[instance_id] = InstanceRow(
        id=instance_id,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash="h",
        task_id=task_id,
        body={"__namespace": "some_project.models", "__name": "TrainedModel"},
    )
    result = invoke("tasks", "ref", task_id)
    assert result.exit_code == 0, result.output
    # The namespace is never read as a module path.
    assert result.stdout.splitlines() == [
        f'TrainedModel.ref("{task_id}", instance_id="{instance_id}")',
    ]
    assert "-m <module>" in result.stderr


def test_a_renamed_function_task_prints_the_name_it_is_bound_to(fake_registry):
    task = add_one(x=1)
    fake_registry.tasks[str(task.id)] = TaskRow(
        task_id=str(task.id),
        task_namespace=task.get_namespace(),
        task_name=task.get_name(),
        version=task.version,
        output_uri=None,
    )
    instance_id = uuid4()
    fake_registry.instances[instance_id] = InstanceRow(
        id=instance_id,
        deployment_id=uuid4(),
        settings_hash="s",
        instance_hash="h",
        task_id=str(task.id),
        body=task.instance_body(),
    )
    result = invoke("tasks", "ref", task.id)
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        f"from {__name__} import add_one",
        f'add_one.ref("{task.id}", instance_id="{instance_id}")',
    ]


def test_an_instance_of_another_task_is_refused(fake_registry, running_build):
    (root_instance, *_) = _instances_of(fake_registry, str(running_build.root.id))
    result = invoke("tasks", "ref", running_build.leaf.id, "--instance", root_instance)
    assert result.exit_code == 1
    assert "belongs to task" in result.output
