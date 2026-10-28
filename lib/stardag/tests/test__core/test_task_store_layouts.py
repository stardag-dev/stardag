"""Which package's task store a call reads, in fresh processes: monorepos
of several packages, scripts loaded by file path, notebook-like callers,
installed packages, references hydrated at build time, and the nested-drift
diagnostics.

Every scenario runs in a subprocess with ``STARDAG_NO_REGISTRY=1`` and a
temporary ``HOME``: no registry, profile or user config is reachable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

MODELS = textwrap.dedent(
    """
    import stardag as sd

    sd.auto_namespace(__name__)


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


def _env(tmp_path: Path, *paths: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("STARDAG_")}
    (tmp_path / "home").mkdir(exist_ok=True)
    env.update(
        PYTHONPATH=os.pathsep.join(str(p) for p in paths),
        HOME=str(tmp_path / "home"),
        STARDAG_NO_REGISTRY="1",
        STARDAG_MODAL_LIVE_TESTS="0",
    )
    return env


def _run(
    tmp_path: Path, cwd: Path, code: str, *paths: Path, script: Path | None = None
) -> str:
    args = (
        [sys.executable, str(script)]
        if script
        else [sys.executable, "-c", textwrap.dedent(code)]
    )
    result = subprocess.run(
        args,
        cwd=cwd,
        env=_env(tmp_path, *paths),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _package(root: Path, name: str, table: str | None, models: str = MODELS) -> Path:
    """A package project ``root`` with ``src/<name>/models.py`` and a
    ``pyproject.toml`` (with a ``[tool.stardag]`` table when given)."""
    source = root / "src" / name
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("")
    (source / "models.py").write_text(models)
    text = f'[project]\nname = "{name}"\n'
    if table is not None:
        text += f"\n[tool.stardag]\n{table}\n"
    (root / "pyproject.toml").write_text(text)
    return root / "src"


def _pin(tmp_path: Path, package_root: Path, src: Path, name: str, size: int) -> str:
    """Write ``TrainedModel(data=Dataset(size))`` into the package's
    configured store (CLI-like: from the package root) and return its id."""
    return _run(
        tmp_path,
        package_root,
        f"""
        import stardag as sd
        from {name}.models import Dataset, TrainedModel

        model = TrainedModel(data=Dataset(size={size}))
        sd.write_to_store(model)
        print(model.id)
        """,
        src,
    )


def test_a_pinned_constant_reads_its_own_packages_store_from_another_package(
    tmp_path,
):
    """Package B imports package A's module-level from_store constant and
    runs from B's directory, which has its own store: A's store is read."""
    repo = tmp_path / "repo"
    (repo).mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "monorepo"\n')  # no table
    src_a = _package(
        repo / "pkg_a", "pkg_a", 'task_store = "src/pkg_a/stardag_task_store"'
    )
    src_b = _package(repo / "pkg_b", "pkg_b", 'task_store = "stardag_task_store"')
    model_id = _pin(tmp_path, repo / "pkg_a", src_a, "pkg_a", 3)
    (src_a / "pkg_a" / "pins.py").write_text(
        "from pkg_a.models import TrainedModel\n"
        f'BASELINE = TrainedModel.from_store("{model_id}")\n'
    )
    (src_b / "pkg_b" / "use.py").write_text(
        "from pkg_a.pins import BASELINE\nprint(BASELINE.data.size)\n"
    )
    assert (repo / "pkg_a" / "src" / "pkg_a" / "stardag_task_store").is_dir()
    for cwd in (repo / "pkg_b", tmp_path):
        out = _run(tmp_path, cwd, "import pkg_b.use", src_a, src_b)
        assert out == "3"


def test_nested_packages_each_read_their_own_store(tmp_path):
    """A package nested inside another (both with [tool.stardag]) under a
    repo root without one: each module reads its nearest package's store."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "monorepo"\n')
    outer_src = _package(repo / "outer", "outer_pkg", 'task_store = "pins"')
    inner_src = _package(
        repo / "outer" / "libs" / "inner", "inner_pkg", 'task_store = "pins"'
    )
    outer_id = _pin(tmp_path, repo / "outer", outer_src, "outer_pkg", 1)
    inner_id = _pin(
        tmp_path, repo / "outer" / "libs" / "inner", inner_src, "inner_pkg", 2
    )
    assert (repo / "outer" / "pins").is_dir()
    assert (repo / "outer" / "libs" / "inner" / "pins").is_dir()
    (outer_src / "outer_pkg" / "pins.py").write_text(
        f'from outer_pkg.models import TrainedModel\nX = TrainedModel.from_store("{outer_id}")\n'
    )
    (inner_src / "inner_pkg" / "pins.py").write_text(
        f'from inner_pkg.models import TrainedModel\nX = TrainedModel.from_store("{inner_id}")\n'
    )
    out = _run(
        tmp_path,
        tmp_path,
        "import outer_pkg.pins as o, inner_pkg.pins as i; print(o.X.data.size, i.X.data.size)",
        outer_src,
        inner_src,
    )
    assert out == "1 2"


def test_a_script_loaded_by_file_path_resolves_through_the_task_class(tmp_path):
    """A script in ``scripts/`` beside the package, loaded with
    spec_from_file_location (no package), walks up to a repo-root
    pyproject.toml without [tool.stardag]: the class's module decides."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text('[project]\nname = "monorepo"\n')
    src = _package(repo / "pkg", "script_pkg", 'task_store = "pins"')
    model_id = _pin(tmp_path, repo / "pkg", src, "script_pkg", 4)
    scripts = repo / "scripts"
    scripts.mkdir()
    (scripts / "train_runs.py").write_text(
        "from script_pkg.models import TrainedModel\n"
        f'print(TrainedModel.from_store("{model_id}").data.size)\n'
    )
    loader = tmp_path / "loader.py"
    loader.write_text(
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('train_runs', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
    )
    result = subprocess.run(
        [sys.executable, str(loader), str(scripts / "train_runs.py")],
        cwd=tmp_path,
        env=_env(tmp_path, src),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "4"


def test_a_notebook_like_caller_resolves_through_the_task_class(tmp_path):
    """``__main__`` without ``__file__`` (a REPL, a notebook, ``-c``) and a
    working directory outside every package: the class's module decides."""
    src = _package(tmp_path / "pkg", "nb_pkg", 'task_store = "pins"')
    model_id = _pin(tmp_path, tmp_path / "pkg", src, "nb_pkg", 5)
    elsewhere = tmp_path / "notebooks"
    elsewhere.mkdir()
    out = _run(
        tmp_path,
        elsewhere,
        f"""
        from nb_pkg.models import TrainedModel
        print(TrainedModel.from_store("{model_id}").data.size)
        """,
        src,
    )
    assert out == "5"


def test_an_installed_package_reads_the_store_it_ships(tmp_path):
    """A package installed in site-packages (a venv inside a repository
    whose pyproject.toml configures another store) reads the store shipped
    in the package, found by its marker file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "consumer"\n\n[tool.stardag]\ntask_store = "decoy"\n'
    )
    build = tmp_path / "build"
    src = _package(build, "inst_pkg", 'task_store = "src/inst_pkg/stardag_task_store"')
    model_id = _pin(tmp_path, build, src, "inst_pkg", 6)
    site = repo / ".venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    import shutil

    shutil.copytree(src / "inst_pkg", site / "inst_pkg")  # what a wheel ships
    assert (
        site / "inst_pkg" / "stardag_task_store" / "stardag_task_store.toml"
    ).exists()
    (site / "inst_pkg" / "pins.py").write_text(
        f'from inst_pkg.models import TrainedModel\nX = TrainedModel.from_store("{model_id}")\n'
    )
    out = _run(tmp_path, repo, "import inst_pkg.pins as p; print(p.X.data.size)", site)
    assert out == "6"
    assert not (repo / "decoy").exists()


def test_a_pkg_spec_names_a_store_inside_a_package(tmp_path):
    src = _package(
        tmp_path / "pkg", "spec_pkg", 'task_store = "pkg:spec_pkg/stardag_task_store"'
    )
    model_id = _pin(tmp_path, tmp_path / "pkg", src, "spec_pkg", 7)
    assert (src / "spec_pkg" / "stardag_task_store").is_dir()
    out = _run(
        tmp_path,
        tmp_path,
        f"""
        import stardag as sd
        from spec_pkg.models import TrainedModel
        print(sd.task_from_store(TrainedModel, "{model_id}", store="pkg:spec_pkg/stardag_task_store").data.size)
        """,
        src,
    )
    assert out == "7"


def test_a_reference_is_hydrated_from_the_store_of_the_code_that_created_it(
    tmp_path,
):
    """Package A pins a reference; package B resolves it (as a build does at
    start) from B's directory, which has its own (empty) store, and no
    registry: A's store serves it."""
    src_a = _package(tmp_path / "pkg_a", "ref_a", 'task_store = "pins"')
    src_b = _package(tmp_path / "pkg_b", "ref_b", 'task_store = "pins"')
    model_id = _pin(tmp_path, tmp_path / "pkg_a", src_a, "ref_a", 8)
    (src_a / "ref_a" / "refs.py").write_text(
        f'from ref_a.models import TrainedModel\nREF = TrainedModel.ref("{model_id}")\n'
    )
    out = _run(
        tmp_path,
        tmp_path / "pkg_b",
        """
        import stardag as sd
        from ref_a.refs import REF
        print(sd.resolve_refs(REF).data.size)
        """,
        src_a,
        src_b,
    )
    assert out == "8"


def test_a_nested_drift_names_the_deepest_moved_task(tmp_path):
    src = _package(tmp_path / "pkg", "drift_pkg", 'task_store = "pins"')
    model_id = _pin(tmp_path, tmp_path / "pkg", src, "drift_pkg", 3)
    # Dataset gains a significant field without compat_default.
    (src / "drift_pkg" / "models.py").write_text(
        MODELS.replace("    size: int\n", "    size: int\n    seed: int = 0\n")
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from drift_pkg.models import TrainedModel\n"
            f'TrainedModel.from_store("{model_id}")\n',
        ],
        cwd=tmp_path / "pkg",
        env=_env(tmp_path, src),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0
    assert "The deepest nested task whose id moved: data (drift_pkg.models.Dataset" in (
        result.stderr
    )
    assert "compat_default" in result.stderr


def test_the_cli_resolves_from_the_working_directory(tmp_path):
    """``stardag tasks ref --write`` (no registry here: its store lookup)
    uses the package found from the working directory."""
    src = _package(tmp_path / "pkg", "cli_pkg", 'task_store = "pins"')
    out = _run(
        tmp_path,
        tmp_path / "pkg",
        """
        from stardag._core.anchor import anchored
        from stardag._core.task_store import resolve_store
        with anchored():
            print(resolve_store(None).root)
        """,
        src,
    )
    assert Path(out) == (tmp_path / "pkg" / "pins").absolute()
    json.dumps(out)  # printable


def _install(tmp_path: Path, src: Path, name: str) -> Path:
    import shutil

    site = tmp_path / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src / name, site / name)
    return site


def test_an_installed_package_uses_the_task_modules_in_its_marker(tmp_path):
    """A stale module hint falls back to the package's task_modules, which
    an installed copy reads from the store's marker (no pyproject.toml)."""
    build = tmp_path / "build"
    src = _package(
        build,
        "mark_pkg",
        'task_store = "src/mark_pkg/stardag_task_store"\ntask_modules = ["mark_pkg.models"]',
    )
    model_id = _pin(tmp_path, build, src, "mark_pkg", 2)
    for path in (src / "mark_pkg" / "stardag_task_store").rglob("*.json"):
        data = json.loads(path.read_text())
        data["module"] = "moved.away"
        path.write_text(json.dumps(data))
    site = _install(tmp_path, src, "mark_pkg")
    (site / "mark_pkg" / "pins.py").write_text(
        f'import stardag as sd\nX = sd.task_from_store(sd.BaseTask, "{model_id}")\n'
    )
    out = _run(
        tmp_path, tmp_path, "import mark_pkg.pins as p; print(p.X.data.size)", site
    )
    assert out == "2"


def test_an_explicit_store_bypasses_an_ambiguous_installed_package(tmp_path):
    build = tmp_path / "build"
    src = _package(build, "amb_pkg", 'task_store = "src/amb_pkg/stardag_task_store"')
    model_id = _pin(tmp_path, build, src, "amb_pkg", 4)
    second = src / "amb_pkg" / "other_tasks"
    second.mkdir()
    (second / "stardag_task_store.toml").write_text("format = 1\n")
    site = _install(tmp_path, src, "amb_pkg")
    (site / "amb_pkg" / "pins.py").write_text(
        "from pathlib import Path\n"
        "from amb_pkg.models import TrainedModel\n"
        f'X = TrainedModel.from_store("{model_id}", store=Path(__file__).parent / "stardag_task_store")\n'
    )
    out = _run(
        tmp_path, tmp_path, "import amb_pkg.pins as p; print(p.X.data.size)", site
    )
    assert out == "4"


def test_a_reference_read_from_the_registry_uses_its_packages_task_modules(
    tmp_path,
):
    """With no store body, a reference created in package A is read from
    the registry in package B's process; its nested class lives in a module
    nothing imported, and A's task_modules (not B's) import it."""
    src_a = _package(tmp_path / "pkg_a", "ref_reg", 'task_modules = ["ref_reg.*"]')
    (src_a / "ref_reg" / "data.py").write_text(MODELS.split("class TrainedModel")[0])
    (src_a / "ref_reg" / "models.py").write_text(
        MODELS.split("class Dataset")[0] + "class TrainedModel(sd.Task[str]):\n"
        "    data: sd.TaskLoads[list[int]]\n\n"
        "    def run(self):\n"
        '        self._save("model")\n'
    )
    written = json.loads(
        _run(
            tmp_path,
            tmp_path,
            """
            import json
            from ref_reg.data import Dataset
            from ref_reg.models import TrainedModel
            m = TrainedModel(data=Dataset(size=5))
            print(json.dumps({"id": str(m.id), "body": m.instance_body()}))
            """,
            src_a,
        )
    )
    (src_a / "ref_reg" / "refs.py").write_text(
        f'from ref_reg.models import TrainedModel\nREF = TrainedModel.ref("{written["id"]}", instance_id="{"0" * 8}-0000-0000-0000-{"0" * 12}")\n'
    )
    _package(tmp_path / "pkg_b", "ref_b2", "")
    out = _run(
        tmp_path,
        tmp_path / "pkg_b",
        f"""
        import json, sys
        from stardag.testing import InMemoryRegistry
        from ref_reg.refs import REF
        assert "ref_reg.data" not in sys.modules
        registry = InMemoryRegistry()
        registry.add_instance(body=json.loads({json.dumps(json.dumps(written["body"]))}),
                              task_id=REF.task_id, instance_id=REF.instance_id)
        print(REF.hydrate(registry).data.size)
        """,
        src_a,
    )
    assert out == "5"


def test_a_source_package_shipped_without_its_pyproject_finds_its_store(tmp_path):
    """A package copied into an image as source (no pyproject.toml) finds
    the store it ships through the store's marker."""
    import shutil

    build = tmp_path / "build"
    src = _package(build, "img_pkg", 'task_store = "src/img_pkg/stardag_tasks"')
    model_id = _pin(tmp_path, build, src, "img_pkg", 9)
    image = tmp_path / "image" / "app"
    image.mkdir(parents=True)
    shutil.copytree(src / "img_pkg", image / "img_pkg")
    (image / "img_pkg" / "pins.py").write_text(
        f'from img_pkg.models import TrainedModel\nX = TrainedModel.from_store("{model_id}")\n'
    )
    out = _run(
        tmp_path, tmp_path, "import img_pkg.pins as p; print(p.X.data.size)", image
    )
    assert out == "9"


def test_from_registry_imports_the_task_classs_package_modules_from_anywhere(
    tmp_path,
):
    """from_registry called from a script outside the task's package: a
    nested class miss imports that package's task_modules."""
    src = _package(tmp_path / "pkg", "fr_pkg", 'task_modules = ["fr_pkg.*"]')
    (src / "fr_pkg" / "data.py").write_text(MODELS.split("class TrainedModel")[0])
    (src / "fr_pkg" / "models.py").write_text(
        MODELS.split("class Dataset")[0] + "class TrainedModel(sd.Task[str]):\n"
        "    data: sd.TaskLoads[list[int]]\n\n"
        "    def run(self):\n"
        '        self._save("model")\n'
    )
    written = json.loads(
        _run(
            tmp_path,
            tmp_path,
            """
            import json
            from fr_pkg.data import Dataset
            from fr_pkg.models import TrainedModel
            m = TrainedModel(data=Dataset(size=6))
            print(json.dumps({"id": str(m.id), "body": m.instance_body()}))
            """,
            src,
        )
    )
    elsewhere = tmp_path / "notebooks"
    elsewhere.mkdir()
    out = _run(
        tmp_path,
        elsewhere,
        f"""
        import json, sys, uuid
        from stardag.testing import InMemoryRegistry
        from fr_pkg.models import TrainedModel
        registry = InMemoryRegistry()
        registry.add_instance(body=json.loads({json.dumps(json.dumps(written["body"]))}),
                              task_id="{written["id"]}", instance_id=uuid.uuid4())
        assert "fr_pkg.data" not in sys.modules
        print(TrainedModel.from_registry("{written["id"]}", registry=registry).data.size)
        """,
        src,
    )
    assert out == "6"
