"""Tests for the action-pin checker.

Every rule the checker advertises is a test here, for one reason: this code
fails in the safe-looking direction. A bypass does not raise, it returns
"fine" — the pre-commit hook stays green while an unpinned action sits in the
job that publishes to PyPI. Each rule below was a real bypass at some point in
review; the tests are what stop one being reopened by a later edit.

Run: `python -m pytest scripts/test_check_action_pins.py --noconftest`
(--noconftest because the repo-root conftest imports stardag, which this
hook's environment deliberately does not have.)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import check_action_pins  # noqa: E402
from check_action_pins import check, comments_by_line  # noqa: E402

SHA = "3d3c42e5aac5ba805825da76410c181273ba90b1"
DIGEST = "sha256:" + "a" * 64
PATH = Path(".github/workflows/example.yml")


def run(body: str) -> list[str]:
    """Check a workflow given as its `steps:` body, indented two levels in."""
    return check(PATH, "jobs:\n  build:\n    steps:\n" + body)


# --- the basic contract ----------------------------------------------------


def test_a_pinned_ref_with_a_version_comment_passes():
    assert run(f"      - uses: actions/checkout@{SHA} # v7.0.1\n") == []


def test_a_floating_tag_is_reported():
    problems = run("      - uses: actions/checkout@v7\n")
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]


def test_a_pinned_ref_without_a_comment_is_reported():
    problems = run(f"      - uses: actions/checkout@{SHA}\n")
    assert len(problems) == 1
    assert "NO VERSION COMMENT" in problems[0]


def test_a_trailing_comment_naming_no_version_is_not_a_version_comment():
    problems = run(f"      - uses: actions/checkout@{SHA} # pinned, see docs\n")
    assert "NO VERSION COMMENT" in problems[0]


def test_the_reported_line_is_the_refs_own_line():
    problems = run(
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
        "      - uses: actions/cache@v6\n"
    )
    assert len(problems) == 1
    # jobs:/build:/steps: are lines 1-3, so the second step is line 5.
    assert f"{PATH}:5" in problems[0]


# --- what must not be treated as an action ---------------------------------


def test_a_local_action_is_exempt():
    assert run("      - uses: ./.github/actions/local-thing\n") == []


def test_a_with_input_named_uses_is_data_not_an_action():
    """`with:` holds user-defined keys; flagging one would block a valid commit."""
    assert (
        run(
            f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
            "        with:\n"
            "          uses: some-setting-value\n"
        )
        == []
    )


def test_an_env_var_named_uses_is_data_not_an_action():
    assert (
        run(
            f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
            "        env:\n"
            "          uses: v1\n"
        )
        == []
    )


# --- a reusable workflow call is a ref too ---------------------------------


def test_a_job_level_reusable_workflow_call_is_checked():
    problems = check(
        PATH, "jobs:\n  call:\n    uses: owner/repo/.github/workflows/x.yml@v1\n"
    )
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]


# --- the forms a line-oriented check could not see -------------------------


def test_a_flow_style_step_is_seen():
    problems = run("      - {uses: sneaky/flow-style@v1}\n")
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]


def test_a_quoted_uses_key_is_seen():
    problems = run('      - "uses": sneaky/quoted-key@v2\n')
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]


# --- the ref is what follows the FIRST @ -----------------------------------


def test_a_second_at_does_not_smuggle_a_branch_past_the_sha_pattern():
    """`owner/repo@branch@<sha>` resolves to the branch, not the commit."""
    problems = run(f"      - uses: actions/cache@branch@{SHA} # v6.1.0\n")
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]


# --- every occurrence, not any ---------------------------------------------


def test_a_repeated_sha_must_carry_the_comment_every_time():
    """These SHAs repeat; one commented copy must not vouch for the others."""
    problems = run(
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
        f"      - uses: actions/checkout@{SHA}\n"
    )
    assert len(problems) == 1
    assert "NO VERSION COMMENT" in problems[0]
    # The commented copy is on line 4; the bare one below it is line 5.
    assert f"{PATH}:5" in problems[0]


def test_an_aliased_ref_is_refused_rather_than_checked_at_the_anchors_line():
    problems = check(
        PATH,
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        f"      - uses: &pin actions/checkout@{SHA} # v7.0.1\n"
        "      - uses: *pin\n",
    )
    assert [p for p in problems if "UNSUPPORTED YAML" in p]


def test_an_anchor_defined_outside_jobs_is_still_refused():
    """The alias occurs once, so counting duplicate nodes never saw it."""
    problems = check(
        PATH,
        f"defaults: &pin actions/checkout@{SHA} # v7.0.1\n"
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - uses: *pin\n",
    )
    assert [p for p in problems if "UNSUPPORTED YAML" in p]


def test_a_merge_key_is_refused():
    """`<<: *step` leaves the step with no `uses` key of its own to find."""
    problems = check(
        PATH,
        "defaults: &step {uses: actions/checkout@v7}\n"
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - <<: *step\n",
    )
    assert [p for p in problems if "UNSUPPORTED YAML" in p]


# --- container actions -----------------------------------------------------


def test_a_container_action_on_a_mutable_tag_is_reported():
    problems = run("      - uses: docker://ghcr.io/org/action:latest\n")
    assert len(problems) == 1
    assert "UNPINNED" in problems[0]
    assert "pinact does not resolve these" in problems[0]


def test_a_container_action_pinned_by_digest_passes():
    assert run(f"      - uses: docker://ghcr.io/org/ok@{DIGEST} # v1.2.3\n") == []


# --- refs that cannot be checked at all ------------------------------------


def test_an_expression_ref_is_reported_rather_than_assumed_fine():
    problems = run("      - uses: owner/repo@${{ env.REF }}\n")
    assert len(problems) == 1
    assert "UNVERIFIABLE" in problems[0]


# --- comments come from the scanner, not from counting quotes ---------------


def comment_on(fragment: str) -> str | None:
    """The comment on the single line of `fragment`, as the checker sees it."""
    return comments_by_line(fragment).get(1)


@pytest.mark.parametrize(
    "line, expected",
    [
        ("uses: a/b@x # v7.0.1", "# v7.0.1"),
        ("uses: a/b@x  #v7", "#v7"),
        ("# a whole-line comment", "# a whole-line comment"),
        ("uses: a/b@x", None),
        # A `#` inside a quoted scalar is not a comment.
        ("{uses: a/b@x, with: {n: '# v1'}}", None),
        ('{uses: a/b@x, with: {n: "# v1"}}', None),
        # An escaped quote does not end the scalar, so the `#` is still inside.
        ('{uses: a/b@x, with: {n: "it\\" # v1"}}', None),
        # YAML's doubled single quote is an escaped quote too.
        ("{uses: a/b@x, with: {n: 'it'' # v1'}}", None),
        # An apostrophe in a *plain* scalar opens no scalar at all, so the
        # real comment after it must still be found.
        ("{uses: a/b@x, with: {n: O'Reilly}} # v7.0.1", "# v7.0.1"),
        # A real comment after a flow mapping closes.
        ("{uses: a/b@x, with: {n: 'q'}} # v1.0.0", "# v1.0.0"),
        # A `#` that does not follow whitespace is part of the token.
        ("uses: a/b@x#notacomment", None),
    ],
)
def test_comments_by_line(line, expected):
    assert comment_on(line) == expected


# --- one ref per line, so a comment names one release ----------------------


def test_two_refs_on_one_line_are_refused():
    """A single trailing comment cannot say which release it names."""
    problems = check(
        PATH,
        f"jobs:\n  b:\n    steps: [{{uses: o/a@{SHA}}}, {{uses: o/b@{SHA}}}] # v1.0.0\n",
    )
    assert len(problems) == 1
    assert "MULTIPLE REFS ON ONE LINE" in problems[0]


# --- aliases: refuse the aliased ref, not the whole workflow ----------------


def test_an_alias_anywhere_is_refused_even_if_it_reaches_no_ref():
    """The deliberate trade: a wrong refusal costs one edit to a file that
    could not be verified anyway; a wrong acceptance is an unpinned action in
    the job that publishes to PyPI. Narrowing this to aliases provably
    reaching a `uses` is what let an anchor outside `jobs` through."""
    problems = check(
        PATH,
        "x: &anchor 1\n"
        "y: *anchor\n"
        "jobs:\n"
        "  b:\n"
        "    steps:\n"
        f"      - uses: o/a@{SHA} # v1.0.0\n",
    )
    assert [p for p in problems if "UNSUPPORTED YAML" in p]


# --- the hint matches the problem ------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        "pypa/gh-action-pypi-publish@release/v1",  # a branch, with a slash
        "actions/checkout@main",  # a branch, without one
        "actions/checkout@v7",  # a floating tag
    ],
)
def test_every_movable_ref_gets_the_same_true_remediation(ref):
    """The string alone does not say branch from tag, so the advice must hold
    for both — an earlier slash heuristic called `@main` a tag confidently."""
    problems = run(f"      - uses: {ref}\n")
    assert len(problems) == 1
    assert "pinact run" in problems[0]
    assert "for a branch ref it refuses" in problems[0]


# --- duplicate keys: stop rather than read a different document ------------


def test_a_duplicated_uses_key_is_refused():
    """`_mapping_get` takes the first; GitHub takes the last. Neither is safe
    to report on, so the check stops instead."""
    problems = run(
        f"      - uses: actions/checkout@{SHA} # v7.0.1\n"
        "        uses: evil/unpinned@v1\n"
    )
    assert len(problems) == 1
    assert "DUPLICATE KEY" in problems[0]


def test_a_duplicated_key_anywhere_is_refused():
    problems = check(
        PATH,
        "jobs:\n  b:\n    steps: []\njobs:\n  c:\n    steps: []\n",
    )
    assert [p for p in problems if "DUPLICATE KEY" in p]


# --- main(): the discovery the parser tests never touch ---------------------


def _workflow_repo(tmp_path: Path, files: dict[str, str]) -> Path:
    """A throwaway git repo holding `files`, since discovery is `git ls-files`.

    The environment is scrubbed of `GIT_*` deliberately. Run from a git hook —
    which is exactly where this hook runs — `GIT_INDEX_FILE` and `GIT_DIR` are
    set and inherited, so `git add` here would write into *this* repository's
    index while reading the temporary worktree, staging the whole checkout as
    deleted. That is not hypothetical; it happened once while writing these.
    """
    import os
    import subprocess

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    for name, content in files.items():
        path = tmp_path / ".github" / "workflows" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, env=env)
    return tmp_path


PINNED = f"jobs:\n  b:\n    steps:\n      - uses: actions/checkout@{SHA} # v7.0.1\n"
UNPINNED = "jobs:\n  b:\n    steps:\n      - uses: actions/checkout@v7\n"


def test_main_passes_when_every_discovered_workflow_is_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(
        check_action_pins, "ROOT", _workflow_repo(tmp_path, {"a.yml": PINNED})
    )
    assert check_action_pins.main() == 0


def test_main_finds_both_yml_and_yaml(tmp_path, monkeypatch):
    """Two pathspecs, and a regression in either would silently inspect less."""
    monkeypatch.setattr(
        check_action_pins,
        "ROOT",
        _workflow_repo(tmp_path, {"a.yml": PINNED, "b.yaml": UNPINNED}),
    )
    assert check_action_pins.main() == 1


def test_main_fails_on_an_unpinned_workflow(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        check_action_pins, "ROOT", _workflow_repo(tmp_path, {"a.yml": UNPINNED})
    )
    assert check_action_pins.main() == 1
    assert "UNPINNED" in capsys.readouterr().err


def test_main_ignores_an_untracked_workflow(tmp_path, monkeypatch):
    """Discovery is `git ls-files`, so an unstaged file is not yet ours."""
    root = _workflow_repo(tmp_path, {"a.yml": PINNED})
    (root / ".github" / "workflows" / "scratch.yml").write_text(
        UNPINNED, encoding="utf-8"
    )
    monkeypatch.setattr(check_action_pins, "ROOT", root)
    assert check_action_pins.main() == 0
