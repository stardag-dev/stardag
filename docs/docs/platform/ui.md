# Web UI

The Stardag Web UI is a dashboard over the registry: builds and their plans,
tasks and the instances that realise them, executions, deployments and
concurrency limits, plus workspace management.

## Accessing the UI

| Environment                  | URL                                        |
| ---------------------------- | ------------------------------------------ |
| SaaS                         | [app.stardag.com](https://app.stardag.com) |
| Local dev (`docker compose`) | [localhost:3000](http://localhost:3000)    |
| Self-hosted                  | Your configured domain                     |

Sign in (OAuth, with GitHub or another configured identity provider), then
pick a workspace and environment in the header. Every page below is scoped
to the selected environment, and its URL can be shared or bookmarked:
`/<workspace>/<environment>/builds/<id>`, `/tasks/<task id>`,
`/deployments`, `/limits`.

## A few words first

- A **task** is a completion, identified by its task id: it has one global
  status per environment, and while it runs, one **claim** held by one
  build.
- An **instance** is one construction of a task under a scope — a
  deployment and a settings hash. Its body holds the parameters.
- A **plan** is one request of a build under one scope. A build has exactly
  one active plan; a rollover or a re-trigger under new settings adds a new
  one and supersedes the old.
- An **execution** is one attempt at a task, under one plan: which executor
  and call ran it, when, and how it ended.

## Builds

The builds list shows the environment's builds, **most recently active
first** (the _Last active_ column: a build's last lifecycle change —
created, resumed or finished; task activity does not move it). Filter by
status, by reactive app, or by _Idle for_ to find running builds with no
lifecycle change for a while; the list pages through the server's cursor.

### The build view

One build, over its active plan:

- **The plan graph**: the plan's members over their instance edges
  (dynamic edges dashed). Wide fan-outs — more members of one type, at one
  level, with one status than _Group after_ (default 5) — are drawn as one
  batch node with a count; click it to expand. The graph can be opened
  fullscreen (Esc to leave), switched between left-to-right and
  top-to-bottom, and rearranged by dragging.
- **The task table**: every member with its status and plan membership
  (root, static, dynamic, closure; excluded ones marked — hover for what
  each means), filterable by name and status.
- **Why it failed**: a failed build shows the reason recorded with its
  failure above the graph; once the plan's roots have completed anyway it
  collapses to a dated note.
- **Refresh**: click to refresh, double-click to refresh every 5 seconds
  while the build runs.

The toolbar opens three dialogs:

- **Build info**: the build's identity, status, timestamps, executor
  metadata, the active plan's deployment and settings, and the error of a
  failed build.
- **Plans and scheduling**: every plan of the build, newest first — its
  deployment and settings, when it was activated, sealed and superseded,
  member counts — and what the scheduler sees of the active one: the
  members by status, what is running, runnable or awaiting discovery (with
  attempt and interruption counts), closure conflicts, and the reactive
  scheduler's recent ticks. A reactive build with nothing to do says
  whether a wake-up is queued for it, or that it needs intervention.
- **Build controls**: what the build is still running, and how to stop it.
  The list is the build's executions with no end reported, under any of its
  plans, orphans (executions under a superseded plan) marked, each with its
  task, status, worker, call and age. Narrow it with the filters or by
  ticking rows, and copy the exact `stardag builds stop` command for what
  is on screen; the dialog says what that command will do (it cancels the
  build, unless it stops orphans only) and what it leaves running. The UI
  itself stops nothing: stopping a container needs your executor
  credentials, which is the CLI's job. Below the list, the build's recorded
  outcome can be overridden (completed, failed, cancelled) — which changes
  the record only.

Selecting a task opens its detail pane next to the table.

## Tasks

A task page (`/tasks/<task id>`, or the link icon in a build's detail pane)
shows one completion:

- **Status and claim**: the global status, and while it runs, whether the
  claim is live or lapsed, which execution holds it and **which build** —
  a link to that build. A claim can be released from here, addressed to
  the build that holds it, by any workspace member; the release is
  recorded on the task's event log, and it stops nothing — the worker
  finds out at its next checkpoint. A failed, cancelled or interrupted
  task can be reset to pending.
- **Executions**: every execution of the task across builds, newest first,
  ended ones included — executor, worker, how it ended or what became of
  its claim, its build, and for Modal the call id with a link to the Modal
  dashboard and the full set of Modal identifiers.
- **Instances**: each construction of the task, under its deployment and
  settings, with its parameters; parameters that differ between instances
  are named.
- **Artifacts** the task produced, and its **output URI**.
- **See full event log**: the task's append-only history across builds,
  including reports the registry recorded but refused and structure
  divergences between scopes. Each event's build is a link.

Tasks are reached from a build's plan, a link, or by pasting a task id on
the Tasks page; search over task parameters is not available yet.

## Deployments

The deployments page lists every recorded deployment grouped by app: each
generation, when it was deployed and activated, and which one is current.

## Concurrency limits

The Concurrency page lists the environment's named limits, how many slots
live claims occupy, and which tasks hold them (each linked to its task and
build). Limits can be created, changed and deleted there.

## Workspace management

- Members, roles (owner, admin, member), invites
- Environments, their target roots, and environment-scoped API keys

To create an API key: Workspace Settings, API Keys, _Create API Key_; copy
it and store it securely — it is shown once.

## See Also

- [Platform Overview](index.md)
- [API Service](api.md)
- [Self-Hosting](self-hosting.md)
