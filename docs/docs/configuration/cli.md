# CLI Reference

Complete reference for Stardag CLI commands.

## Installation

=== "uv"

    ```sh
    uv add stardag
    ```

=== "pip"

    ```sh
    pip install stardag
    ```

## Global Commands

### Version

=== "Active venv"

    ```sh
    stardag version
    ```

=== "uv run ..."

    ```sh
    uv run stardag version
    ```

Show SDK version.

## Authentication Commands

### Login

=== "Active venv"

    ```sh
    stardag auth login [--registry NAME]
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth login [--registry NAME]
    ```

Opens browser for OAuth authentication.

**Options:**

- `--registry NAME` - Target registry (default: from active profile)

### Status

=== "Active venv"

    ```sh
    stardag auth status
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth status
    ```

Show current authentication status.

### Refresh

=== "Active venv"

    ```sh
    stardag auth refresh
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth refresh
    ```

Refresh access token for current profile.

### Logout

=== "Active venv"

    ```sh
    stardag auth logout
    ```

=== "uv run ..."

    ```sh
    uv run stardag auth logout
    ```

Clear stored credentials.

## Configuration Commands

### Show Configuration

=== "Active venv"

    ```sh
    stardag config show
    ```

=== "uv run ..."

    ```sh
    uv run stardag config show
    ```

Display current configuration and context.

## Registry Management

### List Registries

=== "Active venv"

    ```sh
    stardag config registry list
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry list
    ```

### Add Registry

=== "Active venv"

    ```sh
    stardag config registry add NAME --url URL
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry add NAME --url URL
    ```

**Example:**

=== "Active venv"

    ```sh
    stardag config registry add central --url https://api.stardag.com
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry add central --url https://api.stardag.com
    ```

### Remove Registry

=== "Active venv"

    ```sh
    stardag config registry remove NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config registry remove NAME
    ```

## Profile Management

### List Profiles

=== "Active venv"

    ```sh
    stardag config profile list
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile list
    ```

### Add Profile

=== "Active venv"

    ```sh
    stardag config profile add NAME \
        --registry REGISTRY \
        --user USER \
        --workspace WORKSPACE \
        --environment ENVIRONMENT \
        [--default]
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile add NAME \
        --registry REGISTRY \
        --user USER \
        --workspace WORKSPACE \
        --environment ENVIRONMENT \
        [--default]
    ```

**Options:**

- `-r, --registry` - Registry name
- `-u, --user` - User email
- `-w, --workspace` - Workspace slug (team/company)
- `-e, --environment` - Environment slug (project/stage)
- `-d, --default` - Set as default profile

**Example:**

=== "Active venv"

    ```sh
    stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile add prod \
        -r central \
        -u me@company.com \
        -w my-company \
        -e production \
        --default
    ```

### Use Profile

=== "Active venv"

    ```sh
    stardag config profile use NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile use NAME
    ```

Set the default profile (also refreshes access token).

### Remove Profile

=== "Active venv"

    ```sh
    stardag config profile remove NAME
    ```

=== "uv run ..."

    ```sh
    uv run stardag config profile remove NAME
    ```

## Workspace & Environment Commands

### List Workspaces

=== "Active venv"

    ```sh
    stardag config list workspaces
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list workspaces
    ```

List workspaces you have access to.

### List Environments

=== "Active venv"

    ```sh
    stardag config list environments
    ```

=== "uv run ..."

    ```sh
    uv run stardag config list environments
    ```

List environments in the active workspace.

## Target Root Commands

Target roots are managed under `stardag environment target-roots`. Changes are automatically synced to the local cache.

=== "Active venv"

    ```sh
    stardag environment target-roots list
    stardag environment target-roots add <name> <uri>
    stardag environment target-roots remove <name>
    stardag environment target-roots set <name=uri ...>
    ```

=== "uv run ..."

    ```sh
    uv run stardag environment target-roots list
    uv run stardag environment target-roots add <name> <uri>
    uv run stardag environment target-roots remove <name>
    uv run stardag environment target-roots set <name=uri ...>
    ```

See `stardag environment target-roots --help` for full options (e.g. `--env` to target a specific environment).

## Build & Task Commands

`stardag builds` and `stardag tasks` answer "what does the scheduler actually
think the state is?" — listing builds, showing a build's scheduling frontier
(including upstreams held by _other_ builds), and cleaning up builds that were
abandoned by a process that died.

=== "Active venv"

    ```sh
    stardag builds list [--status running] [--reactive-app NAME] [--older-than 24h]
    stardag builds show <build-id>
    stardag builds frontier <build-id>
    stardag builds ticks <build-id> [--limit N]
    stardag builds cancel <build-id> [--cascade] [--yes]
    stardag builds cleanup [--older-than 24h] [--build-id ID ...] [--apply] [--yes]

    stardag tasks list [--status running] [--older-than 1h]
    stardag tasks cancel <build-id> <task-id> [--yes]
    stardag tasks retry <build-id> <task-id> [--yes]
    ```

=== "uv run ..."

    ```sh
    uv run stardag builds list [--status running] [--older-than 24h]
    uv run stardag builds show <build-id>
    uv run stardag builds frontier <build-id>
    uv run stardag builds ticks <build-id> [--limit N]
    uv run stardag builds cancel <build-id> [--cascade] [--yes]
    uv run stardag builds cleanup [--older-than 24h] [--apply] [--yes]

    uv run stardag tasks list [--status running] [--older-than 1h]
    uv run stardag tasks cancel <build-id> <task-id> [--yes]
    uv run stardag tasks retry <build-id> <task-id> [--yes]
    ```

All commands accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
profile / environment other than the active one.

- `builds list` — builds most recently active first, with each build's _last
  activity_ and how long it has been idle.
- `builds show` — one build: status, roots, reactive metadata, liveness.
- `builds frontier` — what a reactive scheduler tick sees: which tasks it would
  act on, which executions it would probe, and which upstreams are holding the
  build back (see [Reading the frontier](#reading-the-frontier)).
- `builds ticks` — the scheduler's own account of its recent ticks. Reactive
  builds are driven by many short-lived ticks, each in its own container; this
  is where their reasoning is kept.
- `builds cancel` — cancel one build. `--cascade` also cancels its
  RUNNING/SUSPENDED tasks, releasing their execution claims.
- `builds cleanup` — find and cancel abandoned builds (see
  [Cleaning up abandoned builds](#cleaning-up-abandoned-builds)).
- `tasks list` — tasks by their environment-global status. `--status running`
  is the claim-holder question.
- `tasks cancel` / `tasks retry` — release a claim, or reset a
  failed/cancelled/skipped/suspended task to `PENDING`.

### Durations

`--older-than` takes one number and one optional unit — `s`, `m`, `h`, `d` or
`w`; a bare number is seconds. `24h`, `90m`, `3d`, `2w`. Compound forms
(`1h30m`) and fractions (`1.5h`) are not accepted, and the minimum for a build
staleness threshold is 60 seconds.

### JSON output

The read-only commands — `builds list`, `builds show`, `builds frontier`,
`builds ticks`, `builds cleanup` (whose default dry run writes nothing) and
`tasks list` — take `--json`. In that mode **stdout carries exactly one JSON
document and nothing else**; every hint, warning and prompt goes to stderr, so
piping is safe:

```sh
stardag builds list --status running --json | jq -r '.builds[] | .id'
```

The document is the SDK's model of the API payload: the same field names and
nesting as the REST response, minus any field this SDK version does not model.
`builds cleanup --json --apply` requires `--yes`, since it cannot prompt
without contaminating the output.

### Reading the frontier

`builds frontier` is the command for a build that is not progressing. Besides
the actionable/running partitions, it renders the build's **external
blockers**: tasks of this build held back by an upstream whose current status
_another_ build produced.

That case is easy to hit and hard to see any other way. Task rows and their
dependency edges are per **environment**, not per build, so an upstream that
some other build left `RUNNING` gates this build's tasks while contributing
nothing to the counts this build can see. The command names the blocking task
(namespace and name, not just an id), its status, how long it has been in it,
and the build that owns it — plus which of the two remedies applies:

- **Not in this build's task set.** This build will never schedule it; it can
  only wait for the owner. If the owner is gone, release the claim with
  `stardag tasks cancel <owning-build-id> <task-id>`.
- **In this build's task set, but another build produced its status.** It
  resolves when that build finishes it; retrying from here would not release
  the claim.

One important caveat the output states explicitly: the registry computes the
blocker list **only for a build with nothing actionable and nothing running**.
An empty list therefore means "not externally blocked _or_ not stalled" — for
a build that is merely progressing, `builds frontier` says the list was not
evaluated rather than claiming there are no blockers.

### Cleaning up abandoned builds

A build's status is derived from its build-level events, so a build whose
orchestrator died without emitting a terminal event stays `RUNNING` forever —
interrupted local runs, crashed CI jobs, failed triggers. Each one keeps
holding whatever execution claims and concurrency-limit slots its tasks had at
the moment it vanished, and a claim held by a dead build denies that task to
every future build in the environment.

The end-to-end workflow:

```sh
# 1. Find them: running builds with no activity for a day.
stardag builds list --status running --older-than 24h

# 2. Inspect one before acting on the batch.
stardag builds show <build-id>

# 3. See what it is holding — and, if it is stalled, what is holding it.
stardag builds frontier <build-id>

# 4. Or ask the question claim-first: who holds a claim, and since when?
stardag tasks list --status running --older-than 24h

# 5. Dry run (the default): exactly what would be cancelled, and why
#    anything named was skipped. Writes nothing.
stardag builds cleanup --older-than 24h

# 6. Apply.
stardag builds cleanup --older-than 24h --apply
```

Notes on step 5/6:

- **The selection is the server's, both times.** The dry run and the real run
  send the same filter to the same endpoint, so what you review is what you
  get.
- **Idleness is measured on activity**, not on the column the list is ordered
  by — task events deliberately do not touch that column, so filtering on it
  would call a build that has been running tasks for three days "idle".
- **Cascade is on by default** for `cleanup` (and off by default for a single
  `builds cancel`): releasing leaked claims is the point of a cleanup pass.
- **Reactive builds are excluded** unless you pass `--include-reactive` or
  `--reactive-app NAME`. A reactive build is quiet between ticks by design, and
  already has a watchdog for the case where it wedges.
- Only `RUNNING` builds are ever eligible, which makes the operation
  idempotent — safe to re-run, and safe to put on a timer with `--yes`.
- If the output says the result was truncated, more builds matched than
  `--limit` allowed; run it again.

`stardag builds cleanup` cannot stop anything that is still executing: like
every other status write it rewrites the registry's view, and a worker whose
task is cancelled keeps running until it notices (a completion that lands
afterwards wins). Clean up builds you believe are dead.

## Concurrency Limit Commands

Named concurrency limits cap how many tasks tagged with a given key may run
concurrently across all builds in an environment. The SDK tags tasks with keys;
the cap lives server-side in the registry and is enforced atomically when a task
starts. Manage them with `stardag concurrency-limits` (or in the registry UI:
workspace admin → Concurrency Limits).

=== "Active venv"

    ```sh
    stardag concurrency-limits list [--holders]
    stardag concurrency-limits set <key> <max_concurrent>
    stardag concurrency-limits delete <key> [--yes]
    stardag concurrency-limits holders <key> [--limit N]
    stardag concurrency-limits evict <key> <task_id> [--yes]
    ```

=== "uv run ..."

    ```sh
    uv run stardag concurrency-limits list [--holders]
    uv run stardag concurrency-limits set <key> <max_concurrent>
    uv run stardag concurrency-limits delete <key> [--yes]
    uv run stardag concurrency-limits holders <key> [--limit N]
    uv run stardag concurrency-limits evict <key> <task_id> [--yes]
    ```

All commands accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
profile / environment other than the active one.

- `list` — show each key and its `max_concurrent` (`--holders` adds the current
  holder count, one extra call per key).
- `set` — create or update a limit (upsert; `max_concurrent` must be ≥ 1).
- `delete` — remove a limit so the key becomes unlimited.
- `holders` — list the RUNNING tasks currently holding slots of a key
  (oldest-running first), with task id/name, running-since and executor.
- `evict` — record `TASK_FAILED` for a RUNNING holder to free leaked slots.
  Only evict holders whose process you know is dead: the server cannot verify
  liveness, so evicting a live worker leaves the cap oversubscribed until it
  finishes.

See `stardag concurrency-limits --help` for full options.

## Environment Variables

All CLI behavior can be overridden with environment variables:

| Variable                     | Description                   |
| ---------------------------- | ----------------------------- |
| `STARDAG_PROFILE`            | Active profile name           |
| `STARDAG_API_URL`            | Registry API URL              |
| `STARDAG_API_KEY`            | API key (bypasses OAuth)      |
| `STARDAG_WORKSPACE_ID`       | Workspace UUID                |
| `STARDAG_ENVIRONMENT_ID`     | Environment UUID              |
| `STARDAG_TARGET_ROOTS`       | JSON string of target roots   |
| `STARDAG_TARGET_ROOTS__NAME` | Specific target root override |

## Common Workflows

### Initial Setup

=== "Active venv"

    ```sh
    # Add registry
    stardag config registry add local --url http://localhost:8000

    # Login
    stardag auth login --registry local

    # Create profile
    stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development \
        --default

    # Verify
    stardag config show
    ```

=== "uv run ..."

    ```sh
    # Add registry
    uv run stardag config registry add local --url http://localhost:8000

    # Login
    uv run stardag auth login --registry local

    # Create profile
    uv run stardag config profile add dev \
        -r local \
        -u me@example.com \
        -w my-workspace \
        -e development \
        --default

    # Verify
    uv run stardag config show
    ```

### Switch Environments

=== "Active venv"

    ```sh
    # Create multiple profiles
    stardag config profile add dev -r local -u me@example.com -w my-workspace -e dev
    stardag config profile add prod -r central -u me@company.com -w my-company -e prod

    # Switch between them
    stardag config profile use dev
    stardag config profile use prod

    # Or use environment variable
    export STARDAG_PROFILE=prod
    ```

=== "uv run ..."

    ```sh
    # Create multiple profiles
    uv run stardag config profile add dev -r local -u me@example.com -w my-workspace -e dev
    uv run stardag config profile add prod -r central -u me@company.com -w my-company -e prod

    # Switch between them
    uv run stardag config profile use dev
    uv run stardag config profile use prod

    # Or use environment variable
    export STARDAG_PROFILE=prod
    ```

### CI/CD Setup

```sh
# No interactive login needed - use API key
export STARDAG_API_URL=https://api.stardag.com
export STARDAG_API_KEY=sk_...
export STARDAG_WORKSPACE_ID=...
export STARDAG_ENVIRONMENT_ID=...

# Run builds
python my_pipeline.py
```
