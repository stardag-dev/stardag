#!/bin/bash
# Verify every GitHub Action in .github/workflows/ is pinned to a commit SHA.
#
# A tag is a movable label, not an address: whoever controls an action's
# repository can repoint `v7` at different code at any time, and the next run
# executes it with no commit, diff, or notification here. A commit ID is a
# fingerprint of the code, so moving to new code means editing a workflow file
# — a change that gets reviewed. See tj-actions/changed-files, March 2025.
#
# This exists because pinning is not self-maintaining. The refs were pinned in
# one pass; a single unpinned `uses:` added months later, in a workflow that
# happens to hold `id-token: write`, undoes it silently. The whole property is
# "every one of them", so it has to be asserted rather than remembered.
#
# The version comment is required too, not cosmetic: it is what makes the pin
# readable in review, and what dependabot reads to know which release a SHA
# corresponds to.
#
# To pin or refresh: `pinact run` (brew install pinact). pinact refuses to pin
# a branch ref, so `owner/repo@some/branch` has to be resolved by hand — look
# up the release tag whose commit the branch head is at, and pin that.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

status=0
while IFS= read -r file; do
    # Every `uses:` that names a remote action, as `owner/repo[/path]@ref`.
    # Local actions (`./.github/actions/x`) and container actions
    # (`docker://…`) have no ref to pin and are skipped by the `@` requirement
    # plus the leading-character check below.
    while IFS= read -r line; do
        num="${line%%:*}"
        text="${line#*:}"
        ref="$(printf '%s\n' "$text" | sed -E 's/.*uses:[[:space:]]*//; s/[[:space:]]*(#.*)?$//')"
        case "$ref" in
            ./*|docker://*|'') continue ;;
        esac
        case "$ref" in
            *@[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
            *)
                printf 'UNPINNED  %s:%s  %s\n' "$file" "$num" "$ref"
                status=1
                continue
                ;;
        esac
        # A bare SHA is unreadable in review; require the version comment.
        if ! printf '%s\n' "$text" | grep -qE '#[[:space:]]*v?[0-9]'; then
            printf 'NO VERSION COMMENT  %s:%s  %s\n' "$file" "$num" "$ref"
            status=1
        fi
    done < <(grep -nE '^[[:space:]]*(-[[:space:]]+)?uses:[[:space:]]*[^.]' "$file")
done < <(git ls-files '.github/workflows/*.yml' '.github/workflows/*.yaml' | sort)

if [ "$status" -ne 0 ]; then
    cat >&2 <<'MSG'

An action above is referenced by a movable ref, or is missing its version
comment. Run `pinact run` (brew install pinact) and commit the result; the
expected form is:

    uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
MSG
fi

exit "$status"
