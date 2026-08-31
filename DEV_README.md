# Development Guide

## Project Structure

```
lib/
├── stardag/           # Core SDK library
└── stardag-examples/  # Example DAGs and demos

app/
├── stardag-api/       # FastAPI backend for task tracking
└── stardag-ui/        # React frontend for monitoring
```

## Quick Start

### Install all packages

```bash
./scripts/install.sh
```

Or manually:

```bash
# Install each Python package (creates separate .venv per package)
cd lib/stardag && uv sync --all-extras && cd ../..
cd lib/stardag-examples && uv sync --all-extras && cd ../..
cd app/stardag-api && uv sync --all-extras && cd ../..

# Install frontend
cd app/stardag-ui && npm install && cd ../..

# Install root workspace (for dev)
uv sync --all-extras
```

### Run all tests

```bash
./scripts/test.sh
```

Or via tox:

```bash
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311,stardag-ui
```

## Running the Full Stack

```bash
docker compose up -d
```

This starts:

- PostgreSQL database on port 5432
- API service on port 8000
- Web UI on port 3000

Then run a DAG with API registry:

```bash
export STARDAG_API_REGISTRY_URL=http://localhost:8000
python -m stardag_examples.api_registry_demo
```

View tasks at http://localhost:3000

## Development Commands

### Testing

```bash
# Test specific package
tox -e stardag-py311
tox -e stardag-examples-py311
tox -e stardag-api-py311
tox -e stardag-ui

# Run all Python tests
tox -e stardag-py311,stardag-examples-py311,stardag-api-py311
```

#### Live Modal tests

Modal integration tests come in two tiers. The unit tier (default) uses fakes
and needs no credentials. Modules marked `modal_live` hit a real Modal
workspace (deploy test apps, create volumes, run containers):

```bash
cd lib/stardag

# Unit tier only
uv run pytest tests/test_integration/test_modal -m "not modal_live"

# Live tier — requires Modal credentials; use a personal/dev profile!
STARDAG_MODAL_TEST_PROFILE=<your-dev-profile> \
  uv run pytest tests/test_integration/test_modal -m modal_live
```

Or through tox, which is what CI runs and which defaults to require mode:

```bash
STARDAG_MODAL_TEST_PROFILE=<your-dev-profile> tox -e stardag-modal-live
```

Gating (see `stardag.testing.modal.live_modal_guard`):

- `STARDAG_MODAL_LIVE_TESTS`: `auto` (default: run if authenticated, else
  skip), `1` (require: fail instead of skip), `0` (always skip). The
  `stardag-modal-live` tox env sets `1`, because invoking it _is_ the request
  to run the tier — `auto` would let a missing credential skip everything and
  still exit 0.
- `STARDAG_MODAL_TEST_PROFILE`: if set, live tests are skipped unless the
  active Modal profile matches. Convenient locally, where credentials come
  from a `~/.modal.toml` profile and the name therefore means something.
- `STARDAG_MODAL_TEST_WORKSPACE`: if set, the workspace the credentials
  **actually belong to** must match — resolved from the token itself. Prefer
  this wherever credentials come from the environment rather than a profile,
  CI most obviously. `MODAL_PROFILE` selects a section of `~/.modal.toml`, but
  `MODAL_TOKEN_ID`/`MODAL_TOKEN_SECRET` take precedence over that file and are
  not bound to the profile name, so a profile name asserts nothing there.

Set one of the two whenever you run the live tier, so it can never reach a
shared or production-adjacent workspace by accident.

The ordinary test envs exclude the tier twice over — `-m "not modal_live"`
plus `STARDAG_MODAL_LIVE_TESTS=0`. Both are needed: marker deselection happens
_after_ module import, so without the environment variable the guard still
makes a Modal API call per module before deciding to skip.

##### In CI

`.github/workflows/modal-live.yml` runs the tier against the `andhus` Modal
workspace, asserted via `STARDAG_MODAL_TEST_WORKSPACE`. It is **not** part of
the normal CI run and is
not a required check — it needs credentials, which GitHub does not give to
pull requests from forks.

| When                                                                                                              | Modal environment    |
| ----------------------------------------------------------------------------------------------------------------- | -------------------- |
| A pull request from a branch on this repo, that either touches the tier's paths or carries the `modal-live` label | `ci-pr-<number>`     |
| Manual `workflow_dispatch`                                                                                        | `ci-manual-<run-id>` |
| The weekly schedule                                                                                               | `ci-main`            |

**You do not normally need to do anything.** A pull request touching any of
these runs the tier automatically:

- `lib/stardag/src/stardag/integration/modal/`
- `lib/stardag/src/stardag/build/`
- `lib/stardag/src/stardag/testing/modal/`
- `lib/stardag/tests/test_integration/test_modal/`
- `lib/stardag/pyproject.toml` (the dependency list baked into the worker image)
- `tox.ini`, `.github/workflows/modal-live.yml` (the harness itself)

**The `modal-live` label is the manual override**, for a change that touches
none of those and still warrants a live run — a dependency bump, a `selfhost`
change, a hunch. The `Decide what to run` job logs which rule applied and
why, so a surprising skip is one click to explain.

The schedule is weekly rather than nightly, and it is not there to catch
regressions in merged code — the automatic trigger does that. It is there for
the one signal no commit produces: drift underneath us. `test_live_semantics.py`
pins Modal _platform_ behaviour, and rebuilt images re-resolve dependencies;
both change with time rather than with commits.

Each run gets its own Modal environment, and deleting that environment removes
the apps, volumes and dicts inside it — so the tier can leave its fixed-name
objects (`stardag-testing`, `stardag-testing-app`, ...) exactly where they are
without concurrent runs colliding. A concurrency group serialises runs sharing
an environment name, which is what makes those fixed names safe.

**What the tier does and does not cover.** It exercises Modal _execution_:
real containers, detached spawn and re-attach, retries, cancellation, timeout
semantics. It does _not_ exercise registry interaction — the registries in
these tests are `NoOpRegistry` subclasses, so claim arbitration and reactive
wake-ups are simulated in-process rather than checked against the real API.
Registry behaviour is covered separately by `app/stardag-api`'s own suite
against Postgres, and the crossing between the two by the registry-live tier
below.

#### Registry-live tests

The tier above runs real Modal workers against fake registries. The API's own
suite runs a real registry with no Modal. **Neither covers the crossing** — a
real worker reporting to a real registry over the network — and that crossing
is not a detail of reactive scheduling, it _is_ reactive scheduling: the
worker writes status, the registry flags wake candidates, and the worker
spawns the next tick when no scheduler is live.

`integration-tests/tests_registry_live/` covers it, by deploying a registry
for the run.

**The registry runs its own Postgres inside its own Modal container.** There
is no database account to create, nothing to provision and nothing to clean
up: `modal environment delete` takes the API, the database, the worker app,
the target-root volume and the API-key secret in one call. Migrations run from
scratch on every container start, which is a check the deployed path never
performs.

##### Running it against your own Modal account

You need Modal credentials and nothing else. Everything lands in a Modal
environment named after your checkout, so several worktrees can each have
their own stack at once:

```bash
export MODAL_PROFILE=<your-dev-profile>

# Bring a stack up: ~30s against a warm image cache, ~90s cold.
uv run --project integration-tests python \
  -m stardag_integration_tests.registry_live.provision up

# Run the scenarios (concurrently).
tox -e registry-modal-live

# ...iterate on a scenario against the same stack, as often as you like...

# Throw the whole thing away.
uv run --project integration-tests python \
  -m stardag_integration_tests.registry_live.provision down
```

`provision` names the environment `dev-<checkout-directory>` — so a worktree
at `stardag-worktrees/sta-24` gets `dev-sta-24`. Pass `--modal-env` to
override. It refuses any name outside the `dev-` and `ci-` prefixes: deleting
a Modal environment is irrevocable and takes everything inside it, and the
workspace may well also hold deployments you care about.

**Keeping the stack between runs is the point.** Provisioning is the slow
part; re-running one scenario against a live stack takes seconds. Tear it down
when you are done with the branch, not after every run.

Set `STARDAG_MODAL_TEST_WORKSPACE` if you want provisioning to assert which
Modal account it is about to build in — resolved from the token, not from a
profile name.

##### Concurrency, and turning it off

The scenarios run concurrently (`-n 4`). They are almost entirely sleep —
each waits on Modal containers it does not own — so running them together
costs little more than running the longest, and the tier's runtime stops
being the sum of its parts as scenarios are added. They share one registry
container, which serves them concurrently, and each salts its own task ids.

**When something fails, run them one at a time.** A shared registry and
interleaved logs make a low-level failure much harder to read:

```bash
tox -e registry-modal-live -- -n0 tests_registry_live -k cross_build_wake
```

Note the path is repeated: `--` replaces tox's default posargs wholesale, so
passing only flags would drop it. In CI the same switch is the `serial` input
on `workflow_dispatch`.

##### Gating

`STARDAG_REGISTRY_LIVE_TESTS` is on or off, with no `auto` in between —
unlike the Modal tier, which can cheaply ask "are there credentials?" and skip
politely. Here there is nothing to detect: the registry does not exist until
this tier deploys one, so "detect and decide" would mean building the stack in
order to find out whether to build it. The tox env sets it, and the tier lives
outside the project's default `testpaths`, so neither a bare `pytest` nor a
bare `tox` can reach it.

When it is on, the guard asserts — at module import, so a misconfiguration is
a collection error rather than a scenario that quietly ran against nothing:

- the resolved registry is a real `APIRegistry`, not a `NoOp` (with no
  registry configured the SDK falls back to one, and the scenarios would pass
  having checked nothing);
- it is **this session's deployment**, by URL. The type check alone is not
  enough: a production registry is a perfectly real `APIRegistry`, and these
  scenarios trigger builds, race claims and cancel things;
- it answers an authenticated request, made through the registry's own
  client, so the credentials are proven rather than assumed.

The SDK is configured from `STARDAG_API_URL` / `STARDAG_WORKSPACE_ID` /
`STARDAG_ENVIRONMENT_ID` / `STARDAG_API_KEY` rather than a profile, and that
matters more than it looks: profile resolution reads `~/.stardag/config.toml`
_and_ walks the working directory's parents looking for one, so a checkout
under your home directory finds your real config several levels up.

##### In CI

The same workflow runs both tiers, decided separately, into the same
per-run Modal environment. A change under `app/stardag-api/`,
`lib/stardag/src/stardag/{build,registry,integration/modal,selfhost}/` or
`integration-tests/` triggers this one. Teardown is its own job that waits for
both tiers — sharing an environment means whichever finished first would
otherwise delete the other's stack out from under it.

### Linting & Formatting

```bash
tox -e pre-commit
```

### Type Checking (pyright)

```bash
# Type check specific package
tox -e stardag-pyright
tox -e stardag-examples-pyright
tox -e stardag-api-pyright
```

Note: pyright currently has pre-existing errors and is excluded from CI.

### Full CI Check

```bash
tox
```

## Frontend Development

```bash
cd app/stardag-ui
npm run dev      # Start dev server (port 5173)
npm test         # Run tests
npm run build    # Production build
```

The dev server proxies `/api` to `http://localhost:8000`.

## Authentication for Local Development

When developing locally against the docker compose stack, you need to authenticate the SDK with the API service.

### Setup

1. Start the full stack (includes Keycloak identity provider):

```bash
docker compose up -d
```

2. Access the web UI at http://localhost:3000 and create an account or log in.

3. Install the CLI:

```bash
cd lib/stardag
uv sync --extra cli
```

### Authentication Methods

**Method 1: Browser Login (recommended for interactive development)**

```bash
uv run stardag auth login
```

This opens your browser to Keycloak (http://localhost:8080). After login, tokens are stored in `~/.stardag/credentials.json`.

Check your auth status:

```bash
uv run stardag auth status
```

**Method 2: API Key (for scripts/automation)**

1. Log in to the web UI at http://localhost:3000
2. Go to Organization Settings > API Keys
3. Create a new API key for your workspace
4. Set the environment variable:

```bash
export STARDAG_API_KEY=sk_your_key_here
```

### Sanity Check

After authentication, verify the setup works:

```bash
# Check auth status
uv run stardag auth status

# Run the demo script to test API registry integration
cd lib/stardag-examples
export STARDAG_API_URL=http://localhost:8000
uv run python -m stardag_examples.api_registry_demo
```

You should see tasks appearing in the web UI at http://localhost:3000.

### Logout

```bash
uv run stardag auth logout
```

## Releasing the Server

The server (Registry API + web UI) is released as one image with its own
semver, independent of the SDK. API and UI share a single joint version.

Before tagging, make sure `CHANGELOG.md` has an entry covering the
release's Registry API / UI / Deployment changes (move them out of
`[Unreleased]`) — the GitHub release links to it.

### When to cut one

**After each significant change to the Registry API or the UI**, not in
batches. The image is the only route those changes have to a self-hosted
deployment: the hosted service builds from a commit, so it always runs the
newest API, but a self-hoster runs whatever the last `server-v*` tag built.
An unreleased API change therefore reaches nobody outside the hosted
deployment.

Letting releases lag has a specific failure mode, and it is silent. Version
skew degrades gracefully by design — an older registry answers nothing to an
endpoint it does not have, and the SDK falls back — so a self-hoster on a
stale image sees no error. They see a feature they upgraded the SDK for
quietly not working. `server-v0.1.2` sat for 18 days that way, through six
SDK releases (v0.19.0 → v0.22.0) — including the cross-build wake-up
endpoints v0.22.0's headline feature is built on.

"Significant" means anything a user could notice: new or changed endpoints, a
schema migration, a UI change, a dependency swap on the auth or security
path. A pure refactor with no external surface can wait for the next one.
Bump the minor when the HTTP surface grows or there is a migration, the patch
for fixes and dependency floors.

Bump `DEFAULT_SERVER_VERSION` in the same PR — the pin is what a fresh
`stardag self-host up` gets.

### Dropping support for older SDKs

The hosted service always runs the latest API, so the compatibility case
that actually happens is an **old SDK against a new API**. The server
accepts every SDK version by default; the floor lives in
`STARDAG_API_SDK_MINIMUM_VERSION` (see
`app/stardag-api/src/stardag_api/sdk_compat.py`) and is published as
`minimum_sdk_version` on `GET /api/v1/version`.

Raising that floor is a product decision, not an implementation detail: it
breaks working deployments on purpose. **An API change that raises
`minimum_sdk_version` must say so in all three places a user could look:**

1. `CHANGELOG.md` — under the release's Registry API section, with the new
   minimum and what stopped working below it.
2. `RELEASE_NOTES.md` — under the SDK release that clears the bar, as a
   migration note. This is the file users are pointed at when they upgrade.
3. **The error the server returns** — which is automatic, provided you set
   the value rather than special-casing anything: the 426 body names the
   client's version, the required version and the upgrade command.

A newer SDK against an older self-hosted API is not a supported
configuration and nothing tries to keep it working — self-hosters upgrade
the server and the SDK together.
The image definition is `app/server.Dockerfile` (build context = repo root):

```bash
docker build -f app/server.Dockerfile -t stardag-server .
```

> **Python versions are decoupled for the prebuilt path.** `stardag
self-host up` deploys the prebuilt image by _reference_: it points the
> Modal `web`/`migrate` functions at module-level entry points in
> `lib/stardag/src/stardag/selfhost/_modal_entry.py` (`serialized=False`),
> which Modal imports inside the image. Nothing is cloudpickled, so the
> CLI's interpreter is independent of the Dockerfile's base Python — bump
> the Dockerfile freely. (Only `--from-source` still serializes function
> bodies with the client interpreter, and there the image's Python is
> matched to it automatically.)

To release, push a `server-vX.Y.Z` tag on `main`:

```bash
git tag server-vX.Y.Z
git push origin server-vX.Y.Z
```

CI (`.github/workflows/publish-server-image.yml`) then:

1. Builds the image and pushes it to
   `ghcr.io/stardag-dev/stardag-server:X.Y.Z` and `:latest`, with
   `STARDAG_SERVER_VERSION=X.Y.Z` baked in (surfaced at
   `GET /api/v1/version`).
2. Creates a GitHub Release for the tag with the web UI (extracted from the
   pushed image, so it is byte-identical to what the image serves) attached
   as `stardag-ui-dist-X.Y.Z.tar.gz` (for deployments that serve the UI
   separately, e.g. from S3/CDN).

### First release only: make the GHCR package public

The first push creates the `stardag-server` GHCR package with **private**
visibility. `stardag self-host` pulls the image anonymously
(`modal.Image.from_registry` without credentials), so the prebuilt-image
path fails for everyone until the package is made public. One-time step
after the first release workflow completes:

1. Go to the package settings:
   <https://github.com/orgs/stardag-dev/packages/container/stardag-server/settings>
2. Under "Danger Zone" → "Change package visibility", set it to **Public**.
3. While there, connect the package to the repository (adds the README and
   links it from the repo's Packages sidebar).

Verify with an anonymous pull: `docker logout ghcr.io && docker pull
ghcr.io/stardag-dev/stardag-server:X.Y.Z`.

`stardag self-host` deploys the prebuilt image by default; each SDK release
pins the server version it was tested against
(`DEFAULT_SERVER_VERSION` in `lib/stardag/src/stardag/selfhost/_modal_app.py`
— bump it when a new server version becomes the tested pairing).

### Version convention for non-release builds

Release builds get a clean `X.Y.Z` from the tag (CI passes it as the
`STARDAG_SERVER_VERSION` build arg). Any _other_ build of
`app/server.Dockerfile` (e.g. a deployment pipeline building from an
arbitrary commit) should derive the version with `scripts/server-version.sh`,
which normalizes `git describe --tags --match "server-v*"` to semver
build-metadata form — so deployments truthfully report their deviation from
the nearest release:

| State                          | Version          |
| ------------------------------ | ---------------- |
| Exactly at `server-vX.Y.Z`     | `X.Y.Z`          |
| N commits past the nearest tag | `X.Y.Z+N.g<sha>` |
| No `server-v*` tag reachable   | `0.0.0+g<sha>`   |

```bash
docker build -f app/server.Dockerfile \
  --build-arg STARDAG_SERVER_VERSION="$(scripts/server-version.sh)" .
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on submitting changes.
