#!/usr/bin/env python3
"""Verify every GitHub Action in .github/workflows/ is pinned immutably.

A ref is a label, not an address: whoever controls an action's repository can
repoint ``v7`` at different code at any time, and the next run executes it with
no commit, diff, or notification here. A commit ID is a fingerprint of the
code, so changing what runs means changing a tracked file — a change that gets
reviewed. See tj-actions/changed-files, March 2025.

This exists because pinning is not self-maintaining. The refs were pinned in
one pass; a single unpinned ``uses:`` added months later, in a workflow that
happens to hold ``id-token: write``, undoes it silently. The whole property is
"every one of them", so it has to be asserted rather than remembered.

The version comment is required too, not cosmetic: it is what makes the pin
readable in review, and what dependabot reads to know which release a SHA
corresponds to.

Everything here defers to the YAML parser, and each place it does is somewhere
a text-matching version was actually bypassed during review:

* **Refs come from the document, not from lines.** ``uses`` is a mapping key,
  so ``- {uses: owner/repo@v1}`` and a quoted ``"uses":`` are both valid and
  both invisible to a regex over line starts.
* **The walk follows the workflow schema**, not every mapping key named
  ``uses``. ``with:`` and ``env:`` hold user-defined keys, so an input that
  happens to be called ``uses`` is workflow *data*, and reporting it would
  block a valid commit.
* **Each occurrence is located by its own line mark**, so a ref appearing twice
  is checked twice. Searching the source for the ref text meant one commented
  copy vouched for every other — and these SHAs repeat.
* **Comments come from the scanner's token marks.** Anything after the last
  token on a line is a comment or nothing; deciding that by tracking quotes by
  hand got both an escaped quote and a plain-scalar apostrophe wrong.

See ``scripts/test_check_action_pins.py``; every rule above is a test, because
this is the kind of code that fails silently and in the safe-looking direction.

To pin or refresh: ``pinact run`` (brew install pinact). It will not resolve a
**branch** ref or a ``docker://`` image; those are resolved by hand, and the
failure message says which applies.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# The ref as GitHub resolves it: everything after the *first* `@`. Anchoring on
# the end of the string instead would accept `owner/repo@branch@<40-hex>`,
# which resolves to the branch.
COMMIT_SHA = re.compile(r"^[^@]+@[0-9a-f]{40}$")
# Container actions pin by image digest, which is the same guarantee.
IMAGE_DIGEST = re.compile(r"^docker://[^@]+@sha256:[0-9a-f]{64}$")
VERSION_COMMENT = re.compile(r"^#\s*v?[0-9]")

PINACT_HINT = "Run `pinact run` (brew install pinact) to resolve it."
BRANCH_HINT = (
    "pinact refuses to pin a branch ref: look up the release tag whose commit "
    "the branch head is at, and pin that."
)
DIGEST_HINT = (
    "A container action needs an immutable image digest: "
    "docker://<image>@sha256:<digest>. pinact does not resolve these; use "
    "`docker buildx imagetools inspect <image>:<tag>`."
)


def comments_by_line(text: str) -> dict[int, str]:
    """Map 1-based line number to the YAML comment on it, where there is one.

    The scanner knows where every token ends, so anything after the last token
    on a line is a comment or nothing. That is the whole rule, and it is
    immune to the two cases a hand-rolled quote scanner kept getting wrong: an
    escaped quote inside a double-quoted scalar (`"it\\" # v1"`), and a literal
    apostrophe in a plain scalar (`{n: O'Reilly} # v7.0.1`), which is not a
    quoted scalar at all.
    """
    # Virtual tokens — the ones the scanner synthesises rather than reads —
    # carry a mark at the end of the stream, which on the final line sits
    # *after* the comment and would hide it.
    virtual = (yaml.StreamEndToken, yaml.BlockEndToken)

    ends: dict[int, int] = {}
    try:
        for token in yaml.scan(text):
            if isinstance(token, virtual):
                continue
            line = token.end_mark.line + 1
            ends[line] = max(ends.get(line, 0), token.end_mark.column)
    except yaml.YAMLError:
        return {}

    comments = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        tail = raw[ends.get(number, 0) :].strip()
        if tail.startswith("#"):
            comments[number] = tail
    return comments


def _mapping_get(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def iter_uses(root: yaml.Node | None) -> list[yaml.ScalarNode]:
    """Every action reference in the workflow, as its own scalar node.

    Scoped to where the schema puts one — a job calling a reusable workflow,
    and a step calling an action — so that a `with:` input named `uses` stays
    what it is: data.
    """
    found: list[yaml.ScalarNode] = []
    jobs = _mapping_get(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return found

    for _job_id, job in jobs.value:
        reusable = _mapping_get(job, "uses")
        if isinstance(reusable, yaml.ScalarNode):
            found.append(reusable)

        steps = _mapping_get(job, "steps")
        if isinstance(steps, yaml.SequenceNode):
            for step in steps.value:
                action = _mapping_get(step, "uses")
                if isinstance(action, yaml.ScalarNode):
                    found.append(action)

    return found


def check(path: Path, text: str | None = None) -> list[str]:
    relative = path.relative_to(ROOT) if path.is_absolute() else path
    if text is None:
        # Explicit UTF-8: these workflows contain emoji, and the platform
        # default would raise on a checkout with a non-UTF-8 locale.
        text = path.read_text(encoding="utf-8")

    try:
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        return [f"{relative}: could not be parsed as YAML: {error}"]

    nodes = iter_uses(root)
    comments = comments_by_line(text)
    problems = []

    # An alias resolves to the anchor's node, so an aliased ref would be
    # checked at the anchor's line and against the anchor's comment — the
    # per-occurrence guarantee, quietly lost. Detected by node identity, so an
    # alias somewhere unrelated (an `env` value, say) is none of our business.
    occurrences = Counter(id(node) for node in nodes)
    aliased = {key for key, count in occurrences.items() if count > 1}

    # Two refs sharing a line cannot each be attributed a comment, and one
    # trailing comment naming a single release would vouch for both.
    per_line = Counter(node.start_mark.line + 1 for node in nodes)

    reported_lines: set[int] = set()
    for node in nodes:
        line = node.start_mark.line + 1
        ref = node.value
        where = f"{relative}:{line}"

        if id(node) in aliased:
            if line not in reported_lines:
                reported_lines.add(line)
                problems.append(
                    f"YAML ALIAS  {where}  {ref}\n"
                    "    An aliased ref cannot be verified at its own "
                    "location. Write it literally."
                )
            continue

        if per_line[line] > 1:
            if line not in reported_lines:
                reported_lines.add(line)
                problems.append(
                    f"MULTIPLE REFS ON ONE LINE  {where}\n"
                    "    A trailing comment cannot say which release it names. "
                    "Put each `uses:` on its own line."
                )
            continue

        if ref.startswith("./") or ref.startswith(".\\"):
            continue  # A local action is this repo's own tracked code.

        if "${{" in ref:
            problems.append(
                f"UNVERIFIABLE  {where}  {ref}\n"
                "    The ref is built from an expression, so it cannot be "
                "checked here. Write it literally."
            )
            continue

        if ref.startswith("docker://"):
            if not IMAGE_DIGEST.match(ref):
                problems.append(f"UNPINNED  {where}  {ref}\n    {DIGEST_HINT}")
                continue
        elif not COMMIT_SHA.match(ref):
            hint = BRANCH_HINT if "/" in ref.partition("@")[2] else PINACT_HINT
            problems.append(f"UNPINNED  {where}  {ref}\n    {hint}")
            continue

        comment = comments.get(line)
        if comment is None or not VERSION_COMMENT.search(comment):
            problems.append(
                f"NO VERSION COMMENT  {where}  {ref}\n"
                "    Add the release it corresponds to, e.g. `# v7.0.1`."
            )

    return problems


def main() -> int:
    listed = subprocess.run(
        ["git", "ls-files", ".github/workflows/*.yml", ".github/workflows/*.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()

    problems = []
    for name in sorted(listed):
        problems.extend(check(ROOT / name))

    if not problems:
        return 0

    for problem in problems:
        print(problem, file=sys.stderr)
    print(
        "\nEvery action must be pinned to an immutable ref with the release in "
        "a trailing\ncomment. The expected form is:\n\n"
        "    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 "
        "# v7.0.1\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
