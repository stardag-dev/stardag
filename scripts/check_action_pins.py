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

Three things this gets right that a line-oriented check cannot, each of which
was a real bypass before it was closed:

* **It reads the document, not the lines.** ``uses`` is a mapping key, so
  ``- {uses: owner/repo@v1}`` and a quoted ``"uses":`` are both valid and both
  invisible to a regex over line starts.
* **It walks the workflow schema**, not every mapping key named ``uses``.
  ``with:`` and ``env:`` hold user-defined keys, so an input that happens to be
  called ``uses`` is workflow *data* and must not be reported as an unpinned
  action.
* **It locates each occurrence by its own line mark**, so a ref appearing twice
  is checked twice. Searching the source for the ref text meant one commented
  copy vouched for every other — and these SHAs repeat.

See ``scripts/test_check_action_pins.py``; every rule above is a test, because
this is the kind of code that fails silently and in the safe-looking direction.

To pin or refresh: ``pinact run`` (brew install pinact). pinact refuses to pin
a branch ref, so ``owner/repo@some/branch`` has to be resolved by hand — look
up the release tag whose commit the branch head is at, and pin that. It does
not touch ``docker://`` refs either; those take an image digest.
"""

from __future__ import annotations

import re
import subprocess
import sys
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


def comment_of(line: str) -> str | None:
    """Return the line's YAML comment, or None.

    Searching the raw line for ``#`` would accept a ``#`` inside a quoted
    scalar — ``- {uses: owner/repo@<sha>, with: {name: '# v1'}}`` has no
    comment at all, but reads as one to a plain regex. A comment starts at an
    unquoted ``#`` that begins the line or follows whitespace.
    """
    quote = None
    escaped = False
    for index, char in enumerate(line):
        if escaped:
            # The previous character was a backslash inside a double-quoted
            # scalar, so this one is escaped and cannot close the scalar.
            # Merely skipping the backslash would let `"it\" # v1"` end at the
            # escaped quote and read the rest as a comment.
            escaped = False
        elif quote:
            if char == "\\" and quote == '"':
                escaped = True  # Escapes only exist in double-quoted scalars.
            elif char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[index:]
    return None


def has_version_comment(line: str) -> bool:
    comment = comment_of(line)
    return comment is not None and VERSION_COMMENT.search(comment) is not None


def _mapping_get(node: yaml.Node | None, key: str) -> yaml.Node | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return value_node
    return None


def iter_uses(root: yaml.Node | None) -> list[tuple[int, str]]:
    """Every action reference in the workflow, as (1-based line, ref).

    Scoped to where the schema actually puts one — a job calling a reusable
    workflow, and a step calling an action — so that a `with:` input named
    `uses` stays what it is: data.
    """
    found: list[tuple[int, str]] = []
    jobs = _mapping_get(root, "jobs")
    if not isinstance(jobs, yaml.MappingNode):
        return found

    for _job_id, job in jobs.value:
        reusable = _mapping_get(job, "uses")
        if isinstance(reusable, yaml.ScalarNode):
            found.append((reusable.start_mark.line + 1, reusable.value))

        steps = _mapping_get(job, "steps")
        if isinstance(steps, yaml.SequenceNode):
            for step in steps.value:
                action = _mapping_get(step, "uses")
                if isinstance(action, yaml.ScalarNode):
                    found.append((action.start_mark.line + 1, action.value))

    return found


def _aliases(text: str) -> list[int]:
    """1-based lines carrying a YAML alias (`*name`)."""
    try:
        return [
            token.start_mark.line + 1
            for token in yaml.scan(text)
            if isinstance(token, yaml.AliasToken)
        ]
    except yaml.YAMLError:
        return []


def check(path: Path, text: str | None = None) -> list[str]:
    relative = path.relative_to(ROOT) if path.is_absolute() else path
    if text is None:
        text = path.read_text()

    # An alias resolves to the anchor's node, so its occurrence would be
    # checked at the anchor's line and against the anchor's comment — the
    # per-occurrence guarantee, quietly lost. Refuse rather than pass
    # something this check cannot actually verify.
    if alias_lines := _aliases(text):
        return [
            f"YAML ALIAS  {relative}:{line}\n"
            "    This check cannot verify an aliased ref at its own location. "
            "Write the ref literally."
            for line in alias_lines
        ]

    try:
        root = yaml.compose(text)
    except yaml.YAMLError as error:
        return [f"{relative}: could not be parsed as YAML: {error}"]

    lines = text.splitlines()
    problems = []
    for number, ref in iter_uses(root):
        where = f"{relative}:{number}"

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
                problems.append(
                    f"UNPINNED  {where}  {ref}\n"
                    "    A container action needs an immutable image digest: "
                    "docker://<image>@sha256:<digest>. pinact does not resolve "
                    "these; use `docker buildx imagetools inspect <image>:<tag>`."
                )
                continue
        elif not COMMIT_SHA.match(ref):
            problems.append(
                f"UNPINNED  {where}  {ref}\n"
                "    Run `pinact run` (brew install pinact) to resolve it."
            )
            continue

        if not has_version_comment(lines[number - 1]):
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
