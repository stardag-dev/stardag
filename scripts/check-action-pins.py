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

Why this parses YAML rather than grepping for ``uses:`` at the start of a line.
A check that asserts "every one" has to actually see every one, and ``uses`` is
a mapping key, not a line format — ``- {uses: owner/repo@v1}`` and a quoted
``"uses":`` are both valid, and ``jobs.<id>.uses`` (calling a reusable
workflow) is a ref that needs pinning just as much as a step's. Grepping finds
the forms this repo happens to use today, which is a weaker claim than the one
being made.

Comments do not survive ``yaml.safe_load``, so the refs come from the parse and
the version comment is checked against every source line the ref appears on —
every one, because these SHAs repeat, and accepting a ref on the strength of a
commented copy elsewhere would wave through each new uncommented one. A ref the
parse finds but that cannot be located in the source is reported rather than
passed over: an unreadable pin is not a verified one.

To pin or refresh: ``pinact run`` (brew install pinact). pinact refuses to pin
a branch ref, so ``owner/repo@some/branch`` has to be resolved by hand — look
up the release tag whose commit the branch head is at, and pin that.
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

    Searching the raw line for `#` would accept a `#` inside a quoted scalar —
    `- {uses: owner/repo@<sha>, with: {name: '# v1'}}` has no comment at all,
    but reads as one to a plain regex, which is the missing-comment check
    letting itself be bypassed. A comment starts at an unquoted `#` that
    begins the line or follows whitespace.
    """
    quote = None
    for index, char in enumerate(line):
        if quote:
            if char == "\\" and quote == '"':
                continue  # Escapes only exist in double-quoted scalars.
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char == "#" and (index == 0 or line[index - 1].isspace()):
            return line[index:]
    return None


def iter_uses(node):
    """Yield every `uses` value in the document, at any depth.

    Deliberately structural rather than schema-aware: `steps[].uses` and
    `jobs.<id>.uses` both need pinning, and so does whatever GitHub adds next.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from iter_uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from iter_uses(item)


def source_lines(text: str, ref: str) -> list[tuple[int, str]]:
    return [
        (num, line)
        for num, line in enumerate(text.splitlines(), start=1)
        if ref in line and not line.lstrip().startswith("#")
    ]


def _has_version_comment(line: str) -> bool:
    comment = comment_of(line)
    return comment is not None and VERSION_COMMENT.search(comment) is not None


def check(path: Path) -> list[str]:
    text = path.read_text()
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as error:
        return [f"{path.relative_to(ROOT)}: could not be parsed as YAML: {error}"]

    problems = []
    for ref in dict.fromkeys(iter_uses(document)):  # de-dup, keep order
        located = source_lines(text, ref)
        relative = path.relative_to(ROOT)
        where = f"{relative}:{located[0][0]}" if located else str(relative)

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
                    "docker://<image>@sha256:<digest>."
                )
                continue
        elif not COMMIT_SHA.match(ref):
            problems.append(f"UNPINNED  {where}  {ref}")
            continue

        if not located:
            problems.append(
                f"UNLOCATABLE  {where}  {ref}\n"
                "    Pinned, but the ref could not be found on a source line, "
                "so its version comment cannot be read. Write the step in "
                "block style."
            )
        else:
            # Every occurrence, not any: these SHAs repeat — `actions/checkout`
            # alone appears nine times — so accepting the ref because *some*
            # line carries the comment would let each new uncommented copy in
            # on the strength of an older one.
            problems.extend(
                f"NO VERSION COMMENT  {relative}:{num}  {ref}"
                for num, line in located
                if not _has_version_comment(line)
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
        "\nAn action above is referenced by a movable ref, or is missing its\n"
        "version comment. Run `pinact run` (brew install pinact) and commit the\n"
        "result; the expected form is:\n\n"
        "    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 "
        "# v7.0.1\n",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
