"""Import resolution in a fresh process: module hints recorded in the task
store, and the project's ``[project] task_modules`` imported lazily on a
rehydration miss. Each scenario runs in a subprocess that never imports
the task module by hand.

The subprocesses run with ``STARDAG_NO_REGISTRY=1`` and a temporary
``HOME``, so no registry, profile or user config is reachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

TASKS = textwrap.dedent(
    """
    import stardag as sd

    # A namespace unrelated to the module path: never read as one.
    sd.namespace("identity.only", __name__)


    class Dataset(sd.Task[list[int]]):
        size: int

        def run(self):
            self._save(list(range(self.size)))


    class TrainedModel(sd.Task[str]):
        data: sd.TaskLoads[list[int]]

        def run(self):
            self._save("model")
    """
)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project root with an importable package ``storetest_pkg`` (its
    task classes in ``storetest_pkg.models``) and no config yet."""
    package = tmp_path / "src" / "storetest_pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text(TASKS)
    (tmp_path / "home").mkdir()
    return tmp_path


def _run(project: Path, code: str, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {k: v for k, v in os.environ.items() if not k.startswith("STARDAG_")}
    environment.update(
        PYTHONPATH=str(project / "src"),
        HOME=str(project / "home"),
        STARDAG_NO_REGISTRY="1",
        STARDAG_MODAL_LIVE_TESTS="0",
        **env,
    )
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=project,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _ok(result: subprocess.CompletedProcess[str]) -> str:
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _write_model(project: Path) -> dict[str, str]:
    """Write a model and its dataset into ``<project>/store``; returns
    the task id and the model's instance body."""
    return json.loads(
        _ok(
            _run(
                project,
                """
                import json
                import stardag as sd
                from storetest_pkg.models import Dataset, TrainedModel

                model = TrainedModel(data=Dataset(size=3))
                sd.write_to_store(model, store="store")
                print(json.dumps({"id": str(model.id), "body": model.instance_body()}))
                """,
            )
        )
    )


def _configure(project: Path, table: str) -> None:
    (project / ".stardag").mkdir(exist_ok=True)
    (project / ".stardag" / "config.toml").write_text(f"[project]\n{table}\n")


def test_module_hints_make_from_store_work_with_no_imports(project):
    written = _write_model(project)
    out = _ok(
        _run(
            project,
            f"""
            import sys
            import stardag as sd

            assert "storetest_pkg.models" not in sys.modules
            task = sd.task_from_store(sd.BaseTask, "{written["id"]}", store="store")
            print(type(task).__module__, type(task).__name__, task.data.size)
            """,
        )
    )
    assert out == "storetest_pkg.models TrainedModel 3"


def test_the_configured_store_resolves_against_the_project_root(project):
    _configure(project, 'task_store = "store"')
    sub = project / "notebooks"
    sub.mkdir()
    written = _write_model(project)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import stardag as sd; "
            f"print(sd.task_from_store(sd.BaseTask, '{written['id']}').id)",
        ],
        cwd=sub,
        env={
            **{k: v for k, v in os.environ.items() if not k.startswith("STARDAG_")},
            "PYTHONPATH": str(project / "src"),
            "HOME": str(project / "home"),
            "STARDAG_NO_REGISTRY": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert _ok(result) == written["id"]


def test_task_modules_are_imported_lazily_on_a_miss(project):
    written = _write_model(project)
    _configure(project, 'task_modules = ["storetest_pkg.*"]')
    out = _ok(
        _run(
            project,
            f"""
            import json, sys
            import stardag as sd

            body = json.loads({json.dumps(json.dumps(written["body"]))})
            assert "storetest_pkg.models" not in sys.modules
            task = sd.task_from_body(body, expected_task_id="{written["id"]}")
            print(type(task).__name__)
            """,
        )
    )
    assert out == "TrainedModel"


def test_a_stale_module_hint_falls_back_to_task_modules(project):
    written = _write_model(project)
    for path in (project / "store").rglob("*.json"):
        data = json.loads(path.read_text())
        data["module"] = "moved.away"  # outside the body: not content
        path.write_text(json.dumps(data))
    _configure(project, 'task_modules = ["storetest_pkg.models"]')
    out = _ok(
        _run(
            project,
            f"""
            import stardag as sd

            print(sd.task_from_store(sd.BaseTask, "{written["id"]}", store="store").id)
            """,
        )
    )
    assert out == written["id"]


def test_without_task_modules_the_error_says_how_to_configure_them(project):
    written = _write_model(project)
    result = _run(
        project,
        f"""
        import json
        import stardag as sd

        sd.task_from_body(json.loads({json.dumps(json.dumps(written["body"]))}))
        """,
    )
    assert result.returncode != 0
    assert "TaskClassNotRegisteredError" in result.stderr
    assert "identity.only" in result.stderr
    assert "[project] task_modules" in result.stderr


def test_uncovering_task_modules_are_named_in_the_error(project):
    written = _write_model(project)
    (project / "src" / "storetest_pkg" / "broken.py").write_text("import nope\n")
    _configure(project, 'task_modules = ["storetest_pkg.broken"]')
    result = _run(
        project,
        f"""
        import json
        import stardag as sd

        sd.task_from_body(json.loads({json.dumps(json.dumps(written["body"]))}))
        """,
    )
    assert result.returncode != 0
    assert "['storetest_pkg.broken']" in result.stderr
    assert "ModuleNotFoundError" in result.stderr


def test_env_vars_override_the_project_config(project):
    written = _write_model(project)
    _configure(project, 'task_store = "elsewhere"\ntask_modules = ["nothing.here"]')
    out = _ok(
        _run(
            project,
            f"""
            import json
            import stardag as sd

            body = json.loads({json.dumps(json.dumps(written["body"]))})
            print(type(sd.task_from_body(body)).__name__)
            print(sd.task_from_store(sd.BaseTask, "{written["id"]}").id)
            """,
            STARDAG_TASK_STORE=str(project / "store"),
            STARDAG_TASK_MODULES="storetest_pkg.*",
        )
    )
    assert out.splitlines() == ["TrainedModel", written["id"]]


def test_a_concurrent_miss_waits_for_the_import_and_retries(monkeypatch):
    """A second thread missing a class while the first imports the project's
    modules waits for that import and is told to retry, rather than failing
    against a half-imported process."""
    import threading
    import time

    from stardag._core import task_imports
    from stardag.build import _task_modules
    from stardag.config.project import ProjectConfig, project_config_provider

    started = threading.Event()

    def slow_import(modules):
        started.set()
        time.sleep(0.3)
        return _task_modules.TaskModuleImportReport(imported=tuple(modules))

    monkeypatch.setattr(_task_modules, "import_task_modules", slow_import)
    results: list[bool] = []
    with project_config_provider.override(ProjectConfig(task_modules=("json",))):
        first = threading.Thread(
            target=lambda: results.append(task_imports.import_project_task_modules())
        )
        first.start()
        started.wait(5)
        results.append(task_imports.import_project_task_modules())
        first.join()
    assert results == [True, True]
