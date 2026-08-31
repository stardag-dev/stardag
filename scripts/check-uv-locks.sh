#!/bin/bash
# Verify every committed uv.lock still matches its pyproject.toml.
#
# Nothing else says so. tox runs plain `uv sync`, which silently re-resolves a
# stale lock, so drift never fails a build — it just means CI tested a
# resolution nobody committed. `uv lock --check` is the explicit signal.
#
# Projects are discovered from the committed lockfiles rather than listed here,
# so a new uv project is covered the day it lands. A hand-maintained list is
# exactly what went stale last time.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$ROOT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "check-uv-locks: uv is not installed (see https://docs.astral.sh/uv/)" >&2
    exit 1
fi

# Deliberately not `set -e`: check every project and report all the stale ones,
# rather than aborting at the first and hiding the rest.
status=0
while IFS= read -r lock; do
    project="$(dirname "$lock")"
    if output="$(uv lock --project "$project" --check 2>&1)"; then
        printf 'ok     %s\n' "$project"
    else
        printf 'STALE  %s\n' "$project"
        # Keep uv's own message. `--check` fails for reasons other than drift
        # too — a malformed manifest, an unresolvable requirement, a uv
        # internal error — and swallowing those makes every one of them read
        # as drift, sending the reader to `uv lock`, which is the wrong fix.
        printf '%s\n' "$output" | sed 's/^/       /'
        status=1
    fi
done < <(git ls-files | grep -E '(^|/)uv\.lock$' | sort)

if [ "$status" -ne 0 ]; then
    cat >&2 <<'MSG'

A uv.lock no longer matches its pyproject.toml. Run `uv lock` in each
directory marked STALE above and commit the result.
MSG
fi

exit "$status"
