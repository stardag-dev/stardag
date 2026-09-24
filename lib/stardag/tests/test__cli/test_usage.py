"""The usage block in ``stardag._cli.__doc__`` names every registry-backed
command and the flags each one takes, and every one of them takes
``--json`` (the contract the block states)."""

from __future__ import annotations

import pytest
import typer.main

import stardag._cli as cli_module
from stardag._cli import app

REGISTRY_GROUPS = (
    "builds",
    "executions",
    "plans",
    "deployments",
    "concurrency-limits",
    "tasks",
)
# Options the block states once, for every command, instead of per line.
STATED_ONCE = {"-p", "--stardag-profile", "-e", "--stardag-env", "--help"}


def _commands():
    root = typer.main.get_command(app)
    groups = root.commands  # type: ignore[attr-defined]
    yield "build", groups["build"]
    for group in REGISTRY_GROUPS:
        for name, command in groups[group].commands.items():
            yield f"{group} {name}", command


# Accepted only to be refused (exit 1, see ``tasks check --help``): not
# advertised in the block.
REFUSED = {"tasks check": {"--report"}}


def _usage_of(path: str) -> str:
    """The command's usage in the block: its line and the indented
    continuation lines up to the next command or a blank line."""
    lines = (cli_module.__doc__ or "").splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"stardag {path} ") or line.strip() == (
            f"stardag {path}"
        ):
            block = [line]
            for more in lines[i + 1 :]:
                if not more.strip() or more.strip().startswith("stardag "):
                    break
                block.append(more)
            return "\n".join(block)
    raise AssertionError(f"`stardag {path}` is missing from the usage block")


@pytest.mark.parametrize("path,command", list(_commands()), ids=lambda v: str(v))
def test_the_usage_block_names_the_command_and_its_flags(path, command):
    block = _usage_of(path)
    for param in command.params:
        if param.param_type_name != "option":
            continue
        names = set(param.opts) | set(param.secondary_opts)
        if names & (STATED_ONCE | REFUSED.get(path, set())):
            continue
        assert any(n in block for n in names), f"{path}: {sorted(names)} not listed"


@pytest.mark.parametrize("path,command", list(_commands()), ids=lambda v: str(v))
def test_every_registry_backed_command_takes_json(path, command):
    assert any("--json" in p.opts for p in command.params), path
