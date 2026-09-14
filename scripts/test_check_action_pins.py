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

from check_action_pins import check, comment_of, has_version_comment  # noqa: E402

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


def test_an_alias_is_refused_rather_than_checked_at_the_anchors_line():
    problems = check(
        PATH,
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        f"      - uses: &pin actions/checkout@{SHA} # v7.0.1\n"
        "      - uses: *pin\n",
    )
    assert len(problems) == 1
    assert "YAML ALIAS" in problems[0]


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


# --- comment_of: a `#` is only a comment when it is one ---------------------


@pytest.mark.parametrize(
    "line, expected",
    [
        ("uses: a/b@x # v7.0.1", "# v7.0.1"),
        ("uses: a/b@x  #v7", "#v7"),
        ("# a whole-line comment", "# a whole-line comment"),
        ("uses: a/b@x", None),
        # A `#` inside a quoted scalar is not a comment.
        ("- {uses: a/b@x, with: {n: '# v1'}}", None),
        ('- {uses: a/b@x, with: {n: "# v1"}}', None),
        # An escaped quote does not end the scalar, so the `#` is still inside.
        ('- {uses: a/b@x, with: {n: "it\\" # v1"}}', None),
        # YAML's doubled single quote is an escaped quote too.
        ("- {uses: a/b@x, with: {n: 'it'' # v1'}}", None),
        # A real comment after a flow mapping closes.
        ("- {uses: a/b@x, with: {n: 'q'}} # v1.0.0", "# v1.0.0"),
        # A `#` that does not follow whitespace is part of the token.
        ("uses: a/b@x#notacomment", None),
    ],
)
def test_comment_of(line, expected):
    assert comment_of(line) == expected


def test_has_version_comment_requires_the_version_at_the_comment_start():
    assert has_version_comment("uses: a/b@x # v7.0.1")
    assert not has_version_comment("uses: a/b@x # see v7.0.1 notes")
