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

## Build Commands

`stardag build` starts (or resumes) a build; `stardag builds` inspects and
ends them; `stardag executions`, `stardag plans`, `stardag deployments`
and `stardag tasks` inspect and act on the entities underneath one.

### Starting a build: `stardag build`

=== "Active venv"

    ```sh
    stardag build <ref> [<ref> ...] [--param KEY=VALUE ...]
        [--settings KEY=VALUE ...] [--app module:attr] [--reactive]
        [--resume <build-id>] [--description TEXT] [--dry-run] [--json]
    ```

=== "uv run ..."

    ```sh
    uv run stardag build <ref> [<ref> ...] [--param KEY=VALUE ...]
        [--settings KEY=VALUE ...] [--app module:attr] [--reactive]
        [--resume <build-id>] [--description TEXT] [--dry-run] [--json]
    ```

Each `<ref>` is `module:attr` or `path/file.py:attr`, naming — in that
module — a task object, a list of task objects, a zero-argument callable
returning either, or a task class (constructed from `--param KEY=VALUE`,
each value parsed as JSON when it parses, else taken as a string).

- Without `--app`: runs `sd.build` in this process, against the configured
  registry (or none), planning under this process's local deployment.
- With `--app module:attr` (naming a `StardagApp`, as for
  `stardag modal deploy`): calls `build_trigger` on it instead — mints (or
  resumes) the build here and spawns the deployed function that drives it.
  `--reactive` needs `--app`: it schedules with short-lived ticks rather
  than the resident `build` function.
- `--settings KEY=VALUE` (repeatable) sets a build-wide environment
  variable for every process of the build — see
  [Settings](../how-to/integrate-modal.md#settings-per-build-configuration-without-touching-the-task-id).
  `STARDAG_*` / `MODAL_*` keys are refused. `--resume <build-id>` with no
  `--settings` reuses the build's stored settings.
- `--dry-run` discovers the DAG locally and prints what a build would
  plan — which tasks, whether each is already complete, how many upstream
  edges each has — without creating a build or writing to the registry.
  With `--resume` and no `--settings` it first reads (only reads) the
  resumed build's stored settings, so the local walk matches what the
  resumed build would actually see.

### Inspecting and ending builds: `stardag builds`

=== "Active venv"

    ```sh
    stardag builds list [--status S] [--app NAME | --reactive-app NAME]
        [--limit N] [--cursor C] [--json]
    stardag builds show <build-id> [--json]
    stardag builds frontier <build-id> [--json]
    stardag builds ticks <build-id> [--limit N] [--json]
    stardag builds stop <build-id> [--not-in-current-plan]
        [--executor NAME] [--worker NAME] [--namespace NS] [--older-than 30m]
        [--task-id ID ...] [--no-cancel] [--mark-lost] [--dry-run]
        [--yes] [--json]
    stardag builds cancel <build-id> [--yes] [--json]
    stardag builds complete <build-id> [--force] [--json]
    stardag builds fail <build-id> [--message TEXT] [--yes] [--json]
    ```

=== "uv run ..."

    ```sh
    uv run stardag builds list [--status S] [--app NAME | --reactive-app NAME]
        [--limit N] [--cursor C] [--json]
    uv run stardag builds show <build-id> [--json]
    uv run stardag builds frontier <build-id> [--json]
    uv run stardag builds ticks <build-id> [--limit N] [--json]
    uv run stardag builds stop <build-id> [--not-in-current-plan]
        [--executor NAME] [--worker NAME] [--namespace NS] [--older-than 30m]
        [--task-id ID ...] [--no-cancel] [--mark-lost] [--dry-run]
        [--yes] [--json]
    uv run stardag builds cancel <build-id> [--yes] [--json]
    uv run stardag builds complete <build-id> [--force] [--json]
    uv run stardag builds fail <build-id> [--message TEXT] [--yes] [--json]
    ```

All of these accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
profile / environment other than the active one. `stardag build` above is
the exception: it takes only `-p/--stardag-profile` (no `--stardag-env`
option).

- `builds list` — builds, most recently active first, a page at a time.
  The table shows each build's `Last active` time, which is the order.
  `--status` filters by status; `--app` (alias `--reactive-app`) keeps the
  builds a given app schedules reactively. The footer says how many of the
  `total` matches are shown and prints the cursor of the next page: pass
  it back as `--cursor` (`--json` carries `total` and `next_cursor`).
- `builds show` — one build: status, the failure reason of a `FAILED`
  build, last activity, roots, reactive metadata, and its active plan's
  deployment, settings and outstanding counts.
- `builds frontier` — the build's active plan as a scheduler tick sees it,
  after the registry's closure step: members to expand (discovery jobs),
  members to claim (runnable), members under a live claim (running) — see
  [Reading the frontier](#reading-the-frontier).
- `builds ticks` — the scheduler's own account of its recent ticks, crashed
  ones included. Reactive builds are driven by many short-lived ticks, each in
  its own container; this is where their reasoning is kept.
- `builds stop` — end the build's containers, not just its claims (see
  [Stopping a build's executions](#stopping-a-builds-executions) below).
- `builds cancel` — release the claims held by every one of the build's
  plans and stop there. **Nothing is stopped**: the released tasks go to
  `CANCELLED` (actionable, so any other build holding them runs them), and
  a worker still running exits at its next cooperative checkpoint, or runs
  to completion if its `run()` has none — its report is recorded as late.
- `builds complete` — mark a build `COMPLETED`; refused (`plan_incomplete`)
  unless the active plan is sealed and every non-excluded member is
  `COMPLETED`, unless `--force` (which never overrides a missing seal or
  an excluded root). Releases any claim the build still holds.
- `builds fail` — mark a build `FAILED`, recording `--message`. Releases
  the build's claims, like `cancel`; stops nothing.

### JSON output

Every command from `stardag build` through `stardag tasks` and
`stardag concurrency-limits` takes `--json`. It writes exactly one JSON
document to stdout (after acting, for a write); every hint, warning and
prompt goes to stderr, so piping is safe:

```sh
stardag builds frontier <build-id> --json | jq '.runnable | length'
```

The document is the SDK's model of the API payload — the same field names
and nesting as the REST response, minus any field this SDK version does
not model — plus, on the commands that combine reads (`builds show`,
`builds frontier`, `plans show`, `tasks show`), the extra keys they
compute. A write that would ask for confirmation refuses to prompt in
`--json` mode: pass `--yes` with it. The commands outside the registry
groups (`auth`, `config`, `environment`, `modal deploy`, `self-host`) do
not take `--json`; `stardag environment target-roots set --json` is an
input flag (the roots as a JSON string), not this output mode.

### Reading the frontier

`builds frontier` is the command for a build that is not progressing. It
shows the active plan's `deployment`, `settings` hash, whether it is
`sealed`, whether the registry considers it `plan_complete`, and three
partitions of its members:

- **Discovery jobs** — members whose instance has never been expanded
  (`requires()` not yet evaluated under this scope). A tick rehydrates
  each one and registers what it finds.
- **Runnable** — members whose upstreams are all `COMPLETED` and whose
  status is actionable (`PENDING`, `SUSPENDED`, `INTERRUPTED`,
  `CANCELLED`, `SKIPPED`, or `RUNNING` with a lapsed claim).
- **Running** — members currently `RUNNING` under a live claim, whoever
  holds it. Task state is global to the environment, so this can be a
  claim another build took out on a shared task; this build's next tick
  waits for it to move rather than treating it as a problem (see [Build &
  Execution](../concepts/build-execution.md#shared-tasks-across-builds)).

Runnable and running members carry their `Attempts` and `Interruptions`
in this build, counted by the registry from the execution ledger over all
of the build's plans. A member whose interruptions reach
`TickConfig.max_interruptions` (default 20) is failed by the next tick
rather than restarted, and a member whose claim lapsed with its attempts at
`TickConfig.max_executions` (default 20) is failed rather than taken over.

Above the lists, the summary also shows:

- **Needs tick** — the build's wake-up flag (`GET /builds/{id}/notify`,
  read without clearing it): something changed that a tick has not acted
  on yet. A reactive build that says `yes` here for long, with nothing
  running, has no tick coming; the watchdog sweep is what picks it up.
- **Members** — the plan's non-excluded members by their task's status,
  with the excluded ones counted apart (`GET /plans/{id}`).
- **Roots** — how many of the plan's roots are `COMPLETED`, out of all of
  them.

An empty frontier with the build still `running` and no discovery jobs
either means the plan is not yet sealed, or every member is settled —
`plan_complete` distinguishes the two.

### Stopping a build's executions

`builds cancel` releases claims and reaches no container — a worker still
running notices at its own next checkpoint, or runs to completion.
`stardag builds stop <build-id>` is the command for ending the containers
themselves: it reads `GET /builds/{id}/executions` (the ledger's rows
with no end reported — exact regardless of what has happened to their
claims), cancels each one's Modal call, reports every one it stopped, and
only then cancels the build.

```sh
stardag builds stop <build-id> --dry-run   # list what would be stopped; write nothing
stardag builds stop <build-id>             # stop, report, then cancel the build
```

Filters compose and narrow the selection: `--executor modal`, `--worker
NAME`, `--namespace NS` (tasks whose namespace starts with `NS`; the
ledger rows name the task, so each listed task's namespace is read from
`GET /tasks/{id}`), `--older-than 2h` (executions started at least that
long ago — see [Durations](#durations)), `--task-id <id>` (repeatable).
Anything a filter excludes keeps running once the build is cancelled — it
simply no longer holds a claim.

- `--no-cancel` stops and reports the selected executions but leaves the
  build running on its active plan.
- `--not-in-current-plan` selects only **orphans** — executions started
  under a plan that is no longer the build's active one (after a rollover
  or a re-trigger under new settings) — and **implies `--no-cancel`**: the
  build keeps running on its current plan; only the stray executions of
  its old one are stopped.
- `--mark-lost` additionally ends any selected execution that has no call
  id to cancel, with outcome `lost`, after its own confirmation and
  warning. If it still holds the task's claim, the claim is released as
  `cancelled` and the task set CANCELLED. No report from a marked-lost
  execution is ever applied afterwards — if it is in fact still running,
  its end is recorded but discarded, and its result is not applied.
- Only Modal executions can be cancelled from here; one running in a
  driver's own process, or whose spawn has not yet reported a call id, is
  listed with the reason and left alone (re-run to catch the latter once
  its container is up).

`stardag executions list --build <build-id>` shows the same unended-
executions list on its own, without acting on it.

### Durations

`--older-than` takes one number and one optional unit — `s`, `m`, `h`, `d`
or `w`; a bare number is seconds: `90s`, `90m`, `24h`, `3d`, `2w`. Case is
ignored. Compound forms (`1h30m`), fractions (`1.5h`), months and years are
not accepted, and neither is zero (a zero threshold would match
everything, including work that just started).

On `builds stop` the filter is applied by the CLI to the listed
executions' `started_at`: an execution whose start time is unknown never
matches.

## Plan, Deployment, Execution and Task Commands

=== "Active venv"

    ```sh
    stardag plans show <plan-id> [--json]
    stardag plans list --build <build-id> [--json]
    stardag deployments list [--app NAME] [--kind modal|local]
        [--current] [--limit N] [--json]
    stardag deployments show <deployment-id> [--json]
    stardag executions list (--build <build-id> [--not-in-current-plan] |
        --task <task-id>) [--include-ended] [--json]
    stardag tasks list [--status S] [--limit N] [--cursor C] [--json]
    stardag tasks show <task-id> [--include-ended] [--events N] [--json]
    stardag tasks check <task-id> --module MODULE [--json]
    stardag tasks retry <task-id> [--build <build-id>] [--yes] [--json]
    stardag tasks cancel <task-id> [--build <build-id>] [--yes] [--json]
    stardag tasks exclude <plan-id> <task-id> --reason TEXT [--yes] [--json]
    ```

=== "uv run ..."

    ```sh
    uv run stardag plans show <plan-id> [--json]
    uv run stardag plans list --build <build-id> [--json]
    uv run stardag deployments list [--app NAME] [--kind modal|local]
        [--current] [--limit N] [--json]
    uv run stardag deployments show <deployment-id> [--json]
    uv run stardag executions list (--build <build-id> [--not-in-current-plan] |
        --task <task-id>) [--include-ended] [--json]
    uv run stardag tasks list [--status S] [--limit N] [--cursor C] [--json]
    uv run stardag tasks show <task-id> [--include-ended] [--events N] [--json]
    uv run stardag tasks check <task-id> --module MODULE [--json]
    uv run stardag tasks retry <task-id> [--build <build-id>] [--yes] [--json]
    uv run stardag tasks cancel <task-id> [--build <build-id>] [--yes] [--json]
    uv run stardag tasks exclude <plan-id> <task-id> --reason TEXT [--yes] [--json]
    ```

- `plans show` — any plan, active or superseded (`GET /plans/{id}`): its
  build and generation, lifecycle (created, activated, sealed,
  superseded), scope (the deployment, resolved, and the settings), member
  counts by status with the excluded ones apart, and its roots; for the
  build's active plan, also whether it is complete and what is
  outstanding.
- `plans list --build <build-id>` — every plan of a build, newest
  generation first, with lifecycle and counts (`GET /builds/{id}/plans`):
  the history of its rollovers and re-triggers.
- `deployments list` — every recorded deployment, newest first
  (`--app`, `--kind`, `--current` filter; `--current` keeps one row per
  app). A `modal` row with no `Activated` timestamp is a deploy whose
  record was created but whose activation never landed — re-run
  `stardag modal deploy`. Shares its listing with `stardag modal
deployments`.
- `deployments show <deployment-id>` — one deployment: kind, app,
  generation, code id, image and Modal app ids, deployed and activated
  times, and whether it is its app's current one.
- `executions list` — the execution ledger: a build's executions
  (`--build`) or one task's across builds, newest first (`--task`). By
  default only those with no end reported — what `builds stop` would act
  on; `--include-ended` lists every execution granted, with its outcome.
  `--not-in-current-plan` (with `--build`) keeps the orphans.
- `tasks list` — the environment's tasks, most recent status change
  first, a page at a time (`--status` filters; pass the printed cursor
  back as `--cursor`). `--status running` answers "who holds what": the
  `Claim (build)` column names the build holding each claim. v1's
  `--older-than`, `--name` and `--namespace` filters are not offered: the
  registry's task list filters by status only.
- `tasks show` — one task's global status and the claim's holder (plan and
  build, live or lapsed), every instance the registry holds of it (one per
  scope it was constructed under, newest first), its executions across
  builds (`--include-ended` for all of them, not only the unended), its
  last events (`--events N`, default 10, from the first 500 the registry
  serves), and its artifacts. Every `TASK_STRUCTURE_DIVERGED` event — an
  expanded instance that declared new static edges within its scope, which
  the registry appends and records rather than refuses — is called out
  above the event table, with its detail.
- `tasks check <task-id> --module <import path>` — rehydrate the task's
  newest instance in this process (importing `--module`, repeatable, to
  resolve its class) and run `complete()` locally, printing the
  observation next to the registry's recorded status. Writes nothing: the
  registry only follows the world when a build's own discovery observes a
  target (see
  [Invalidation](../concepts/build-execution.md#invalidation-the-registry-follows-the-world)).
  The command accepts `--report`, but only to refuse it: it exits 1 with
  a message saying to trigger a build instead, because there is no route
  for a bare observation outside a build's discovery.
- `tasks retry` — reset a failed (or otherwise ended) task to `PENDING`
  under the build's active plan, so its next tick or driver runs it
  again. Refused on `COMPLETED` or under a live claim.
- `tasks cancel` — release the claim the build holds on one task
  (`CANCELLED`, actionable elsewhere). Nothing is stopped; `builds stop
--task-id` stops the execution itself.
- For both `retry` and `cancel`, `--build` defaults to the build holding
  the task's claim (`claim_build_id` from `GET /tasks/{id}`) and remains
  an override; a task that holds no claim (a `FAILED` one, typically)
  needs `--build`. Both ask for confirmation, naming the task and the
  build, unless `--yes`.
- `tasks exclude <plan-id> <task-id>` — give up on one task within one
  plan: excludes it and its downstream closure (short of `COMPLETED`
  members) from that plan's scheduling and completion check, recording
  `--reason`. The task's global status is untouched, so other builds
  holding it are unaffected; an excluded root fails the build.

## Concurrency Limit Commands

Named concurrency limits cap how many tasks tagged with a given key may run
concurrently across all builds in an environment. The SDK tags tasks with keys;
the cap lives server-side in the registry and is enforced atomically when a task
starts. Manage them with `stardag concurrency-limits` or the
`GET/PUT/DELETE /api/v2/concurrency-limits` routes directly (see
[Platform: API](../platform/api.md)) — the registry UI has no
concurrency-limits admin page in v2.

=== "Active venv"

    ```sh
    stardag concurrency-limits list [--holders] [--json]
    stardag concurrency-limits set <key> <max_concurrent> [--json]
    stardag concurrency-limits delete <key> [--yes] [--json]
    stardag concurrency-limits holders <key> [--limit N] [--json]
    ```

=== "uv run ..."

    ```sh
    uv run stardag concurrency-limits list [--holders] [--json]
    uv run stardag concurrency-limits set <key> <max_concurrent> [--json]
    uv run stardag concurrency-limits delete <key> [--yes] [--json]
    uv run stardag concurrency-limits holders <key> [--limit N] [--json]
    ```

All commands accept `-p/--stardag-profile` and `-e/--stardag-env` to target a
profile / environment other than the active one.

- `list` — show each key, its `max_concurrent` and how many slots are
  currently `in_use` (`--holders` adds a table of each key's current
  holders, from the same call — no extra request per key).
- `set` — create or update a limit (upsert; `max_concurrent` must be ≥ 0;
  `0` blocks the key entirely).
- `delete` — remove a limit so the key becomes unlimited.
- `holders` — list the tasks currently holding slots of a key (a live
  claim), oldest-running first, with task id/name, build and execution.
  A key with no configured limit is not listed here — configure one first.

There is no `evict`. A v2 slot is released by ending the execution that
holds it: for a holder whose worker is gone, `stardag builds stop
--mark-lost` is the recovery path, not a concurrency-limits command.

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
