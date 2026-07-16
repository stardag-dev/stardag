#!/usr/bin/env bash
# Print the server version for builds of app/server.Dockerfile.
#
# Convention (see DEV_README.md "Releasing the Server"): release builds get
# a clean X.Y.Z from the server-vX.Y.Z tag (the tag workflow passes it
# directly); any other build derives the version from git describe so the
# deployment truthfully reports its deviation from the nearest release:
#
#   exactly at a server-vX.Y.Z tag  ->  X.Y.Z            (clean, same as CI)
#   N commits past the nearest tag  ->  X.Y.Z+N.g<sha>   (semver build metadata)
#   no server-v* tag reachable      ->  0.0.0+g<sha>     (before the first release)
#   not a git checkout              ->  dev
#
# Usage:
#   docker build -f app/server.Dockerfile \
#     --build-arg STARDAG_SERVER_VERSION="$(scripts/server-version.sh)" .
set -euo pipefail

describe="$(git describe --tags --match 'server-v*' --always 2>/dev/null || true)"

if [[ "$describe" == server-v* ]]; then
  version="${describe#server-v}"
  # git describe forms: X.Y.Z (exactly at tag) or X.Y.Z-N-g<sha> (past it)
  if [[ "$version" =~ ^([0-9]+\.[0-9]+\.[0-9]+)-([0-9]+)-g([0-9a-f]+)$ ]]; then
    echo "${BASH_REMATCH[1]}+${BASH_REMATCH[2]}.g${BASH_REMATCH[3]}"
  else
    echo "$version"
  fi
elif [[ -n "$describe" ]]; then
  # --always fallback: a bare commit sha - no server-v* tag reachable yet
  echo "0.0.0+g${describe}"
else
  echo "dev"
fi
