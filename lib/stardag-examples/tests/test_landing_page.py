"""Tests for landing page code examples.

Discovers and executes all .py files from app/stardag-ui/src/code-examples/.
Full file content (including # -- hidden -- section) is executed,
which is where test assertions live.
"""

from pathlib import Path

import pytest

CODE_EXAMPLES_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "stardag-ui" / "src" / "code-examples"
)

SKIP_FILES: dict[str, str] = {
    "ml-pipeline.py": "display-only snippet (no imports)",
    "configure-env/profile.py": "display-only TOML documentation",
    "configure-env/customize.py": "overrides target factory (display-only)",
    "async-io/decorator-api.py": "makes real HTTP calls",
    "async-io/class-api.py": "makes real HTTP calls",
}


def discover_example_files() -> list[Path]:
    """Discover all .py example files, returning paths relative to CODE_EXAMPLES_DIR."""
    if not CODE_EXAMPLES_DIR.is_dir():
        return []
    files = sorted(CODE_EXAMPLES_DIR.rglob("*.py"))
    return [f.relative_to(CODE_EXAMPLES_DIR) for f in files]


def _file_id(path: Path) -> str:
    return str(path)


@pytest.mark.parametrize("example_file", discover_example_files(), ids=_file_id)
def test_landing_page_example(example_file: Path, default_in_memory_fs_target):
    rel = str(example_file)
    for skip_key, reason in SKIP_FILES.items():
        if rel == skip_key:
            pytest.skip(reason)

    full_path = CODE_EXAMPLES_DIR / example_file
    code = full_path.read_text()
    compiled = compile(code, str(full_path), "exec")
    namespace: dict = {}
    exec(compiled, namespace)
