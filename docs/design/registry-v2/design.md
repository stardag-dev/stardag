# Registry v2: core entities

Why the registry's core relations are being replaced rather than patched
again, what the new entities are, the invariants they carry, and a walk of
every scenario the design was tested against. Written before implementation
(STA-105, project P-STA-1); the "what shipped" section is filled in as the
implementation issues close.

Decisions and their runner-ups are numbered D1–D13 in
[decisions.md](decisions.md); the implementation plan and status are in
[plan.md](plan.md); the point-in-time research the design rests on is under
[research/](research/).

## The problem this answers

v1's `task` row carries two lifetimes: the task's **identity** (`task_data`,
unique per task id) and its **state** (status, claim, executor). Everything
hard about reactive scheduling in the last month traces to that one row and
the two things bolted onto it:

- `task_data` is first-write-wins, so non-identity parameters were frozen at
  first registration (STA-43), and the fix stored them per build instead
  (`build_config`), which then had to be transported into every process of
  the build — the seam that produced most of #346's thirteen review rounds.
- Dependency edges were environment-global and permanent (STA-41/42); the
  fix keyed them by a _structure scope_ string, `<code_id>:<config_hash>`,
  parsed by convention and joined to nothing.
- Plan membership is not a relation at all: it is "has a `TASK_PENDING` or
  `TASK_REFERENCED` event for this build under its current scope", scanned
  from the event log on every frontier read. Five other places define "the
  tasks of this build" as "any event in the build", so cascade-cancel, the
  task list, the graph and the wake relation each see a different set.
- Registration and claim arbitration lock the same row, so identity writes
  take the locks the claim needs (STA-63, STA-48, STA-51), and the
  registration transaction exists twice in a 5,000-line routes file.
- Deployments are recorded but referenced by nothing; a build's deployment
  is the string prefix of its scope key.

v2 replaces the core relations. It is a **fully breaking** release line —
new SDK, CLI, API (`/api/v2`) and UI together, **no data migration**;
existing registries start empty. Compatibility shims, dual-writing and
version-skew handling between v1 and v2 are out of scope. That decision is
what makes a clean model affordable.

## What carries over unchanged

These are settled by the v1 design notes and the September 2026
architecture-health review, and v2 restates rather than reopens them:

- A task id is a promise about **output**: a hash of completion-significant
  parameters, never of code. Completion and the execution claim are
  **global** on it.
- Structure (which upstreams a task has) belongs to the **code and its
  scope**, not to the task id. Within a scope, edges only grow, so gating can
  only over-approximate; under-gating, the only route to wrong output, is
  unreachable. Two builds in different scopes may materialise one completion
  over different upstream sets; the duplicated upstream work is **accepted,
  not prevented**.
- Three rules on what may affect what. **Output** is a function of the
  significant parameters and nothing else: not the code version, not an
  environment variable, not settings — that is the task-id promise, and it is
  the user's to keep (`__version__`). **Structure** may depend on environment
  variables only through the two things that are in the scope, the
  deployment's environment and `settings`; within a scope it is therefore
  deterministic, and an environment variable that reaches `requires()` any
  other way is a contract breach that over-gates, never one that produces
  wrong output. **Execution** may depend on anything.
- A build is a request, not an owner. The claim (RUNNING plus expiry,
  arbitrated under a row lock in the same transaction as the event) is the only
  cross-build coordination. Nothing revokes an execution automatically; the
  server never reaches a backend; cancellation is cooperative.
- COMPLETED is a fact about the world (the target exists). v2 lets the
  registry _follow_ that world when a target is observed missing (below);
  nothing else withdraws a completion, and nobody can by fiat.
- A build follows the live deployment: rollover re-plans it. Retraction of
  edges ("an edge is evidence asserted by an act") stays abandoned.
- The registry is the scheduler state: a status write flags the reactive
  builds it concerns; the watchdog owns time-based wake-ups; the server never
  spawns user code.
- "Don't key on what accompanies a fact." v1 keyed structure on the config
  that accompanied a build; v2 keys it on the task instance itself.

## Two hashes, one flag

A task instance has two identities:

| Identity        | Hashes                                                                            | Answers                                                  |
| --------------- | --------------------------------------------------------------------------------- | -------------------------------------------------------- |
| `task_id`       | `__namespace`, `__name`, `version`, every field with `significant=True` (default) | "Is this output done?" and "Who is running it?" — global |
| `instance_hash` | the same plus every field with `significant=False`, i.e. **all** parameters       | "How exactly was this task constructed?" — per scope     |

The SDK API is `StardagField(significant: bool = True)`. The four logical
levels of significance (completion, dependencies, execution, annotation)
still exist conceptually, but the model needs only "completion-significant
or not": levels 2–4 all go into the instance hash and nowhere else. v1's
`significance=` and `hash_exclude=` are removed; `compat_default=` stays,
on significant fields only (a value equal to it is dropped from `task_id`,
so a field can be added without re-keying every downstream). `build_config`
and its ContextVar transport are deleted. Non-significant fields are
**ordinary fields, passable at init**, stored on the instance body, and
rehydrated from it — the SKDS rule "levels 2/3 never at init" is replaced by
the one-instance-per-completion-per-plan constraint below.

Hashing rules, stated so both hashes are pure functions of the body:

- `task_id` is uuid5 over the canonical hash-mode dump, as in v1, where the
  hash-mode dump now **excludes every `significant=False` field** (the one
  exclusion mode that remains, replacing `hash_exclude` and the significance
  levels) and drops a significant field whose value equals its
  `compat_default`.
- `instance_hash` is uuid5 over the canonical dump of **all** fields,
  defaults included. A nested task appears in that dump as its full body,
  exactly as it must for the dump to deserialize, so the outer hash covers
  the nested task's parameters in full. (It is not represented by its
  `task_id`, which would let two bodies share one instance hash at the
  nested level, and not by a nested hash of its own, which the body does
  not contain.) Nested `StardagBaseModel`s carry `significant` on their own
  fields, which affects `task_id` only.
- The scope is a storage key for the body, not a hash input.
- Rehydration of a body is strict for significant fields (the recomputed
  `task_id` must match) and lenient for non-significant ones: unknown keys
  are dropped with a warning, missing ones take the class default. This is
  what lets an old plan's root bodies be re-read under new code at rollover.

Three rules make the instance hash safe to build on:

1. **The instance hash is the hash of the body, not of a separate view.**
   `instance_hash = hash(canonical_json(body))`, where canonical means sorted
   keys, sorted sets, compact separators, UTF-8. `instance_hash ↔ body` is
   then 1:1 by construction — the hash is of the stored bytes — and the
   server's `instance_body_conflict` reduces to "same hash, different bytes",
   which can only be a client bug.
2. **User control over hashing stays on `task_id` only.** Custom serializers
   under the `"hash"` serialization mode and `compat_default` shape the
   completion identity, which is the promise the user owns. The body has no
   hash mode to customise: what the user controls there is ordinary pydantic
   serialization, and the only thing stardag demands of it is stability.
3. **Stability means a fixed point, and the SDK checks it.**
   `dump(validate(dump(x))) == dump(x)`. At registration the driver runs that
   round trip once per distinct instance and refuses the build with
   `UnstableSerializationError` naming the field that moved. That turns every
   instability below into a trigger-time error instead of an
   `instance_conflict` between two processes later.

The instabilities the check exists for: a `set[str]` iterates in a different
order per process (string hashing is randomised), so sets must be sorted in
the body dump too, not only in hash mode; floats are stable when the value is
(`repr` is the shortest round-trip form), but `-0.0` vs `0.0`, `NaN`, numpy
scalar types and a float computed non-deterministically in `__init__` are
not; naive vs aware datetimes and custom serializers that drop precision fail
the round trip; and because the body includes fields not set at init, a
changed class default under a new deployment is a new instance hash — correct
(a new scope anyway) and worth a sentence in the user docs.

Two words, kept apart everywhere in code, docs and the UI: an **instance** is
a registry row, a construction of a task under a scope; the Python object is
a **task object**. One task object constructed under two deployments is two
instances; one instance rehydrates into any number of task objects. Because
the row key is `(deployment_id, settings_hash, instance_hash)`, `instance_hash`
is **never a public identifier on its own**: routes, the CLI and the UI
address an instance by its row id or by the full scope triple, so the
tempting misreading — "the instance hash identifies the instance" — cannot be
acted on.

Two instances with the same `task_id` and different `instance_hash` are two
ways of asking for one completion. Globally the registry stores any number
of them per scope. **Within one plan there may be only one.**

The target is a function of significant fields only: two instances of one
`task_id` must produce the same `output_uri`, and registration refuses an
instance whose `output_uri` differs from the one recorded on the task (409
`output_uri_conflict`). Without that check, completion would be global while
the output location was not.

## The deterministic scope

`scope = (deployment_id, settings_hash)`: the pair under which code
behaviour — output, structure, execution — is deterministic by contract.

**Deployment.** One row per `stardag modal deploy`, with an id minted by the
CLI (uuid7), baked into the Modal image as the `STARDAG_DEPLOYMENT_ID`
secret (replacing `STARDAG_CODE_ID`), and registered in two steps:
`POST /deployments` **before** the deploy creates the row and the server
assigns it a `generation`, monotonic per environment and app; `POST
/deployments/{id}/activate` after the deploy succeeded marks it live (a
failed activation exits non-zero; a tick whose deployment id the registry
does not know, or has not activated, cannot create a plan). A redeploy of
unchanged code is a new deployment and therefore a new scope; running
reactive builds re-plan, which is cheap and rare in production. The row
holds `kind` (`modal` | `local`), `app_name`, `code_id`, `image_id`
(nullable), `modal_app_id` (nullable), `generation`, `deployed_at`,
`activated_at`. "Current" for an app is the **activated row with the highest
generation**: order is fixed when a deploy starts, not when its record
lands, so a record that arrives late cannot roll a build back to older
code.

**Local builds** have no deploy event, so their deployment is derived: the
SDK does an idempotent lookup-or-create on `(environment, kind='local',
code_id)` at `sd.build()` start, where `code_id` is `STARDAG_CODE_ID` if set,
else the clean git HEAD SHA, else a fresh uuid per process (warned). A clean
tree therefore shares its scope across local builds at the same commit; a
dirty tree never shares; `STARDAG_CODE_ID` is the explicit pin and is on the
user. `kind` keeps local and Modal deployments from ever colliding. A `local`
row has no deploy step to wait for, so lookup-or-create returns it **already
activated** (`activated_at` set in the same insert, `generation` assigned as
for any row); the two-step create/activate is for Modal deployments only.
A `local` row's `app_name` is `"local"` unless the driver names an app, so
its generations are ordered like any app's; a lookup naming another app for
a recorded code id is 409 `local_deployment_conflict`. A `local` deployment
is **never current and never superseded**: it is authoritative for its own
plans, so the seal's current-deployment check, resume's reactivation check
and rollover apply to `kind = modal` only (a local driver at a new commit
plans under a new scope; the old commit's plans stay usable), and
`GET /deployments` marks no local row current. Generation assignment
is serialised per `(environment, kind, app_name)` (a transaction-scoped
advisory lock, since the row may not exist yet), and `deployed_at` is the
registry's clock. `GET /deployments` marks each Modal row current or not
(`?current=true` keeps one per app), which is how a hybrid driver finds the
app's current deployment.

**A driver that is not the deployment.** A hybrid `sd.build()` whose tasks
run on a Modal app, and `reactive_discovery="local"`, plan under **the
app's current deployment** (read from the registry at start); that the
driver's code matches it is on the user, as v1's clean-tree sharing was.
Workers therefore never yield into a scope that is not their own: `/yield`
refuses a worker whose `STARDAG_DEPLOYMENT_ID` differs from the plan's
(409 `deployment_mismatch`), and the worker fails the task with that
message. A pure local build plans under its `local` deployment. When both
`STARDAG_DEPLOYMENT_ID` and `STARDAG_CODE_ID` are set, the former wins; the
latter only feeds the local lookup. The principle for every case in this
paragraph: where the registry cannot guarantee that a driver's code matches
the deployment, it does not try — the simplest mechanism, and the
responsibility on the user, stated in the docs.

**`settings`** is a flat `dict[str, str]` of environment variables applied in
every process of the build (bootstrap, tick, worker, resident driver), chosen
per trigger (`build_trigger(settings=...)`), per `sd.build(settings=...)` or
on the command line (`--settings KEY=VALUE`, repeatable). Its body is stored
in the registry under a content hash: a UUID5, like the task id and the
instance hash, over the canonical JSON (sorted keys, compact separators,
UTF-8), in a fixed namespace of its own
(`uuid5(<default task-id namespace>, "stardag.settings_hash.v1")`, never an
overridden task namespace, since the registry computes it). It is stable
across code versions, unlike the task hashes, and the registry computes it
from the posted body: a client never sends one, so none can store a body
under a key it does not match. The empty settings are stored like any other
body, as `{}` under the well-known hash
`11406eac-39d0-5b1b-9423-cfb4a1454543`, and every settings row is created
lazily by the first plan that needs it. They are for build-wide behaviour a
user chooses not to put in task parameters (a global thread count, a feature
flag), and the intended way to read them is the pydantic-settings pattern:
the user declares a `BaseSettings` subclass and stardag sets the variables it reads. **Never
credentials** — secrets live in Modal secrets, deployment env vars or a
secret manager.

The contract, which the user docs state next to `significant`: settings
**may change structure and execution, never output**. Anything that affects
output is a significant parameter. Completion is global, so a value here
that changed output would let one build reuse another's different result.

Mechanics. The registry validates the body too (a flat `str → str`
mapping; `STARDAG_*` / `MODAL_*` keys are 400 `reserved_settings_key`), and
serves it at `GET /settings/{hash}`. When a key appears in several places, settings win over the
worker selector's per-task env, which wins over the deployment's env. Keys
starting `STARDAG_` or `MODAL_` are refused at the trigger, and the
framework's own identifiers (`STARDAG_PLAN_ID`, `STARDAG_DEPLOYMENT_ID`,
`STARDAG_EXECUTION_ID`, the registry coordinates) are written **last** by the
executor and win over settings, the selector's env and the deployment's env
alike — a selector is user code and may not redirect a worker's reports.
Values are
applied in a scoped `temp_env_vars` around the run in workers and ticks, and
for the duration of the build in a resident driver (two concurrent
`sd.build()` calls in one process with different settings are refused).
**A process applying settings serves one build at a time**: deployed ticks
and workers run one input per container and scale by containers (a declared
`max_concurrent_inputs` above one on them is refused at deploy), and
`settings_applied` refuses a second build entering while another build's
settings are installed, so a misconfiguration fails loudly rather than
running under the wrong build's values. The scoped blocks and a resident
driver hold one process-wide owner token, so a resident `sd.build()` and a
local reactive bootstrap or tick in one process cannot both install
settings (the refusal names the owner). The cost is more containers — a
lingering tick holds one of its own — accepted. The
docs say "read at run time, not import time", because warm containers import
before they know the build. Two names it is deliberately not: `env_overrides`,
which remains the worker selector's per-task env fixed at deploy; and the
SDK's own connection configuration (profile, registry URL), which stays under
`stardag.config` — settings are what _user_ code reads, and the reserved-key
rule keeps the two apart mechanically.

Consequences: a plan is identified by its build and its scope; a resume or
re-trigger under different settings is a new plan in the same build,
not a refusal (v1's 409 `scope_mismatch` goes away). A re-trigger whose root
**instances** differ from the plan's recorded roots under the same scope is
409 `root_instance_conflict` ("start a new build"): a build is one request.

## Entities

Group (b) tables (users, workspaces, environments, members, invites, API
keys, target roots) are untouched. Every table below has `environment_id`
and `created_at`; both are omitted from the listings. Ids are uuid7 unless
stated. One rule for every reference between environment-scoped tables:
**the FK carries `environment_id`** (a composite FK onto a `(environment_id,
id)` unique key on the parent), so a row can never point at another
environment's build, deployment, task, plan, instance, settings or
execution by constraint rather than by query discipline. The registry
requires **PostgreSQL 15 or newer**: the pointer FKs use the column-list
`ON DELETE SET NULL (col)` form, which clears the pointer without clearing
the composite key's `environment_id`. The listings name
the composite FKs that carry something more; the environment column is
implied in all of them.

### `task` — the completion and its global state

Replaces v1 `tasks`. Holds no parameters.

| Column                                                     | Notes                                                                                                                                                                                                                                                                                                                                                    |
| ---------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id` PK                                                    |                                                                                                                                                                                                                                                                                                                                                          |
| `task_id`                                                  | completion hash; `UNIQUE (environment_id, task_id)`                                                                                                                                                                                                                                                                                                      |
| `task_namespace`, `task_name`, `version`, `output_uri`     | identity-level, part of the hash / derived from it                                                                                                                                                                                                                                                                                                       |
| `status`                                                   | `PENDING, RUNNING, COMPLETED, FAILED, CANCELLED, SKIPPED, SUSPENDED, INTERRUPTED` — a native enum or a CHECK; v1's `UNREGISTERED` phantom status is gone                                                                                                                                                                                                 |
| `status_at`, `started_at`, `completed_at`, `error_message` | as v1 `latest_*`                                                                                                                                                                                                                                                                                                                                         |
| `claim_expires_at`                                         | CHECK: NOT NULL whenever `status = RUNNING`. The claim is **live** when `status = RUNNING AND claim_expires_at > now()`; RUNNING with a past expiry is a **lapsed** claim, which the next claiming start takes over. Every execution has a finite expiry (D11); nothing is live forever: the default TTL is 3600 s and a requested one is capped at 24 h |
| `claim_plan_id`, SET NULL                                  | the holder; implies the build, and via `plan_member` the instance body that is running. Composite FK `(claim_plan_id, id)` → `plan_member (plan_id, task_pk)`, its primary key, so a claim can only name a plan that holds this task                                                                                                                     |
| `execution_id`, SET NULL                                   | current execution, minted by the client before the claim (STA-50 rule unchanged); executor details are read from the execution row, not copied. Composite FK `(id, execution_id)` → `execution (task_pk, id)`                                                                                                                                            |
| `preempted_at`                                             | as v1 (`waiting_for_lock` goes with the lock table)                                                                                                                                                                                                                                                                                                      |

Indexes: `(environment_id, status, status_at)`, `(environment_id,
task_name)`, `(claim_plan_id) WHERE status = 'RUNNING'`.

Claim arbitration locks this row with `FOR NO KEY UPDATE` (not `FOR
UPDATE`): every insert into `task_instance`, `plan_member`, `execution` and
`event` takes a `FOR KEY SHARE` on the referenced `task` row through its FK,
and `FOR UPDATE` conflicts with that while `FOR NO KEY UPDATE` does not —
STA-51's finding, applied. Registration locks `task` only through
`transition_task()`, when an item's observation changes a status: `FOR NO
KEY UPDATE`, in `task_id` order (the chunk's order).

Dropped from v1: `task_data`, `is_phantom`, `latest_status_scope_key`,
`latest_commit_hash`, `latest_status_event_id`, `latest_status_build_id`
(replaced by `claim_plan_id`), `latest_executor*`, `latest_waiting_for_lock`.

### `deployment`

| Column                                                           | Notes                                                                                                                      |
| ---------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `id` PK                                                          | client-minted                                                                                                              |
| `kind`                                                           | `modal` \| `local`                                                                                                         |
| `app_name`, `code_id`, `image_id`, `modal_app_id`, `deployed_at` |                                                                                                                            |
| `generation`                                                     | server-assigned at create, monotonic per `(environment_id, kind, app_name)`; decides which activated deployment is current |
| `activated_at`                                                   | set by `/activate` after the deploy succeeded; NULL rows are never current and cannot host a plan                          |

`/activate {modal_app_id?, image_id?}` records what only the finished
deploy knows: a given value fills a NULL column or must equal the recorded
one (409 `deployment_activation_conflict`).

Index `(environment_id, kind, app_name, generation DESC)`; unique
`(environment_id, kind, app_name, generation)`; partial unique
`(environment_id, code_id) WHERE kind = 'local'`.

### `settings`

| Column       | Notes                                                                                                                                                                    |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `hash` UUID  | UUID5 of canonical JSON, computed by the registry; PK with `environment_id`                                                                                              |
| `body` JSONB | flat `dict[str, str]`; the empty settings are stored as `{}` and created by the same lookup-or-create as any other, so a fresh registry has no rows until its first plan |

### `task_instance` — a task as constructed under a scope

| Column                                     | Notes                                                                                                                                                                               |
| ------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id` PK                                    |                                                                                                                                                                                     |
| `deployment_id`, `settings_hash`, NOT NULL | the scope; `settings_hash` references `settings` through the composite FK `(environment_id, settings_hash)`, since `settings` is keyed per environment                              |
| `instance_hash`                            | hash of all parameters, computed by the SDK under that scope                                                                                                                        |
| `task_pk` FK task                          | the completion this instance realises                                                                                                                                               |
| `body` JSONB                               | all parameters, registry-mode dump; nested tasks as full dumps                                                                                                                      |
| `expanded_at`                              | **the closure flag**: set when this instance's `requires()` was evaluated under its scope and every resulting edge recorded (zero edges included). NULL means "not yet looked for". |

`UNIQUE (deployment_id, settings_hash, instance_hash)`; `UNIQUE (id,
task_pk)` (target of the composite FK from `plan_member`); `UNIQUE (id,
deployment_id, settings_hash)` (target of the composite FKs from edges);
index `(deployment_id, settings_hash, task_pk)`.

Invariants per scope: `instance_hash ↔ body` is 1:1 — an insert that hits
the unique key with a **different body** is 409 `instance_body_conflict`,
so this is server-checked, not a client promise; `instance_hash → task_id`
is a function; `instance_hash → upstream instance set` is a function once
the flag is set. Many instances per `task_pk` per scope are allowed (they
are different constructions of one promise).

### `task_instance_dependency`

| Column                                           | Notes                                                                                                                                                                                                                            |
| ------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `downstream_instance_id`, `upstream_instance_id` | PK is the pair                                                                                                                                                                                                                   |
| `deployment_id`, `settings_hash`, NOT NULL       | denormalised scope; composite FKs `(downstream_instance_id, deployment_id, settings_hash)` and `(upstream_instance_id, deployment_id, settings_hash)` → `task_instance` make "both ends share a scope" a constraint, not a check |
| `is_dynamic`                                     | set at first insert, never changed                                                                                                                                                                                               |

Edges belong to no plan and are never deleted (retention of instances and
edges of retired deployments is STA-68's question, unchanged; `deployment`
is `ON DELETE NO ACTION` from every table, so retention has to be explicit;
`NO ACTION` rather than `RESTRICT` because it is checked at the end of the
statement, so deleting an environment — whose cascade removes the deployment
and every row referencing it together — never depends on cascade order).

### `plan` — one request, under one scope

| Column                                     | Notes                                                                                                                                                                                                                                         |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id` PK                                    | client-minted (idempotent create)                                                                                                                                                                                                             |
| `build_id` FK                              |                                                                                                                                                                                                                                               |
| `deployment_id`, `settings_hash`, NOT NULL | the scope; composite FK `(environment_id, settings_hash)` → `settings`, as on `task_instance`                                                                                                                                                 |
| `generation`                               | server-assigned per build at create, monotonic; `/seal` activates a plan only if no higher-generation plan exists for the build, so two replacements racing (same deployment, different settings) cannot leave the build on the older request |
| `activated_at`                             | the plan is the build's active plan from here; the first plan activates on create, a replacement activates on `seal`                                                                                                                          |
| `sealed_at`                                | the static phase is fully stated and verified (roots expanded, closure holds, deployment still current)                                                                                                                                       |
| `superseded_at`                            | set when a replacement plan activated                                                                                                                                                                                                         |

`UNIQUE (build_id, deployment_id, settings_hash)`; `UNIQUE (id,
deployment_id, settings_hash)` (target of the composite FK from
`plan_member`); `UNIQUE (build_id, generation)`; partial unique
`(build_id) WHERE activated_at IS NOT NULL AND superseded_at IS NULL` —
**exactly one active plan per build**, while a replacement can be registered
alongside it. Reactivating an old scope on resume flips the timestamps.

### `plan_member` — membership

| Column                                     | Notes                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plan_id` FK, `task_pk` FK                 | **PK (plan_id, task_pk)** — the one-instance-per-completion-per-plan rule                                                                                                                                                                                                                                                                                                                                                   |
| `instance_id`                              | composite FK `(instance_id, task_pk)` → `task_instance (id, task_pk)`, so a member's instance realises the member's task by construction; `UNIQUE (plan_id, instance_id)` follows                                                                                                                                                                                                                                           |
| `deployment_id`, `settings_hash`, NOT NULL | denormalised scope, with composite FKs `(plan_id, deployment_id, settings_hash)` → `plan` and `(instance_id, deployment_id, settings_hash)` → `task_instance`, so a member's instance is in its plan's scope by constraint, not by check                                                                                                                                                                                    |
| `is_root`                                  | the build's request, as instances of this plan                                                                                                                                                                                                                                                                                                                                                                              |
| `admitted_by`                              | `root` \| `static` \| `dynamic` \| `closure`                                                                                                                                                                                                                                                                                                                                                                                |
| `excluded_at`, `excluded_reason`           | `excluded_reason` ∈ `operator \| discovery_failed \| upstream_excluded`, set together with `excluded_at` (CHECK). "Given up on" (STA-104): not scheduled, does not gate the build's completion; exclusion **cascades to the member's downstream closure within the plan** (like skip-blocked, otherwise a downstream is neither runnable nor excluded) and **an excluded root fails the build** (the request cannot be met) |

No counters: attempts and interruptions are counted from `execution` rows
over the build's plans (index `execution (task_pk, plan_id)`). The frontier
carries them on its `runnable` and `running` items — `attempts` (executions
of the task under any of the build's plans, so a replacement plan does not
reset the budget) and `interruptions` (those released or ended
`interrupted`, or ended `preempted`). The tick applies its interruption
budget to `interruptions` (`TickConfig.max_interruptions`: an INTERRUPTED
member at the cap is failed with the count, not restarted), and its
execution budget to `attempts` when it would take over a lapsed claim
(`TickConfig.max_executions`, default 20: a RUNNING member with a lapsed
claim at the cap is failed with the count, not taken over; a first run, an
INTERRUPTED restart and an operator retry are not gated); it retries
nothing on `attempts`, since a FAILED member is the fail mode's.

Registering the same instance again is a no-op (`ON CONFLICT DO NOTHING …
RETURNING`, STA-48's pattern; the event write is gated on the `RETURNING`,
so a retried chunk appends nothing). Registering a **different** instance
for a completion already in the plan is 409 `instance_conflict` naming the
fields that differ between the two bodies. The SDK's discovery pass sees
both static constructions first and fails synchronously at the trigger with
both paths named; a conflict found at `/yield` or by closure is
**non-retryable** and fails the member (and so the build, per fail mode)
with both paths named; the server is authoritative via the primary key.

### `execution` — the ledger

| Column                                          | Notes                                                                                                                                                                                                                                                                                                                                                                                                                       |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `id` PK                                         | client-minted; this is the `execution_id` of STA-50                                                                                                                                                                                                                                                                                                                                                                         |
| `task_pk`, `plan_id`, `instance_id`             | which promise, under which request, from which body; composite FKs `(instance_id, task_pk)` → `task_instance (id, task_pk)` and `(plan_id, instance_id)` → `plan_member (plan_id, instance_id)` (its unique key), so an execution names one consistent membership                                                                                                                                                           |
| `executor`, `executor_ref`, `executor_metadata` | the one place these live                                                                                                                                                                                                                                                                                                                                                                                                    |
| `started_at`                                    | the claim was granted                                                                                                                                                                                                                                                                                                                                                                                                       |
| `claim_released_at`, `claim_outcome`            | written by the **server** whenever the task leaves RUNNING or the claim changes hands, uniformly in `transition_task()`: `completed, failed, suspended, interrupted, cancelled` (the report that moved the task), `taken_over` (a later claiming start took a lapsed claim), `lapsed` (a lapsed claim closed by something other than a claiming start, e.g. an observed completion), `released` (build terminal transition) |
| `ended_at`, `outcome`                           | written only by the **execution's own report** or an operator end: `completed, failed, suspended, interrupted, preempted, stopped, lost` (`stopped`: the CLI stopped it; `lost`: it cannot be stopped and the operator gives up on it)                                                                                                                                                                                      |

One row per execution, updated only at its two ends. It never decides
anything: liveness is the claim on `task`. `ended_at IS NULL` means "no
report of this execution ending has arrived", which is exactly what
`builds stop` wants to list, and is independent of whether the claim has
since moved (an execution ref is not a claim). At most one terminal report
is applied per execution; anything after `ended_at` is recorded as an event
with `report_applied = false`.

### `build`

Keeps: id, name, description, user, `status` + companions, `root_task_ids`
(the request at completion-id level, stable across rollover), reactive
columns (`reactive_app_name`, `reactive_tick_kwargs`, `scheduler_lease_*`),
`last_active_at`, `executor_metadata`, and `error_message` (why it is FAILED:
the message of the `BUILD_FAILED` that produced the status; NULL for every
other status, so a resumed or completed build does not keep explaining an old
failure). Drops `scope_key`, `build_config`,
`commit_hash`. The active plan is found through `plan`, not stored twice.

The wake-up flags `needs_tick_at` and `tick_requested_at` are on
**`build_wake`**, one row per build (PK `build_id`, composite FK onto
`build`, created with it, `ON DELETE CASCADE`), so that flagging never locks
the build row: a claiming start holds its build `FOR SHARE` while it holds
the task row, and a flagger — inside another task's transition — can
neither wait for that (build → task order; it would deadlock against the
claim's task lock) nor skip it without losing the wake-up.

### `event`

Keeps the append-only log. `task_pk` nullable (build-level events); adds
`plan_id` (replaces `scope_key`; NULL for build-level events, by CHECK) and
`execution_id`; `report_applied` becomes a real column, here only, as does
`batch_id` (nullable; unique on `(execution_id, batch_id)` where set).
`build_id`, `plan_id` and `execution_id` are **nullable pointers**: NULL for
an event that has none (operator `invalidate` and limit-slot `evict` have no
build), and set NULL when what they point at is deleted — `ON DELETE SET
NULL (col)`, the column-list form, so only the pointer is cleared and not
the composite key's `environment_id`, and not CASCADE: v1's cascade deleted
the sources of the global status fold. All three FKs are `DEFERRABLE
INITIALLY DEFERRED`, because a build delete reaches one event row along
three paths (build; build → plan; build → plan → member → execution): once
the first action rewrites the row, Postgres re-checks its other FKs, and an
immediate check fails on an execution already deleted whose own SET NULL
has not run yet. Deferred to commit, every action has run first. Event
types unchanged, except `TASK_WAITING_FOR_LOCK`, removed with the lock
table, plus `TASK_INVALIDATED`, `TASK_EXCLUDED`, `TASK_OBSERVED_COMPLETE`,
`TASK_STRUCTURE_DIVERGED`, `TASK_YIELDED`.

### Peripheral tables, re-pointed

`task_artifact.task_pk` → `task` (artifacts belong to the promise); they
are uploaded through the member that produced them, `POST
/plans/{plan_id}/members/{task_id}/artifacts`, upserted per `(task, type,
name)` as in v1, and read by `GET /tasks/{task_id}/artifacts`.
`task_limit_key.task_pk` → `task`, written **at claim time from the claiming
instance** and replaced on every claim (limit-key selection may read
non-significant fields, so it is per instance; a slot is a limit key plus a
live claim). The tick computes the keys from the instance body and sends
them **with the claiming start** (`limit_keys` on the start request, not on
the registration item). The claim locks the limit rows `FOR UPDATE` in key
order and counts live holders; a full key is 409
`concurrency_limit_reached`, and that refusal also writes the keys it asked
for, so a task that has never held a claim is findable as queued on them
when a slot frees (it holds no slot: its claim is not live). `distributed_lock` is **retired** (D11). `build_tick_summary`
unchanged. The 24-hour creation quota counts `task_instance` rows — the
table a non-significant field can inflate — and a second one counts
`task_artifact` rows (v1's artifact count), both per environment and charged
only for rows actually inserted. Deleting a build is refused
(409) while any of its plans holds a live claim **or any of its executions
has `ended_at IS NULL`**; `stardag builds stop` is how those are ended first
(it writes `ended_at` with outcome `stopped`, also for a container the
backend reports gone, or `lost` for one it cannot stop and gives up on), so
the ledger is never cascaded away under a worker that may still report.

## Registration

Every write route is idempotent from the first release (STA-54): plan ids,
execution ids and deployment ids are minted by the client; inserts use `ON
CONFLICT DO NOTHING … RETURNING`; the only contended rows are `task` (claim
arbitration), locked in `task_id` order, and never in the same transaction as
instance/edge/membership inserts except through the FK `KEY SHARE` locks
STA-51 documented — the registration transaction takes no `FOR UPDATE` on
`task` at all, and a `FOR NO KEY UPDATE` only where an observation changes a
status (in `task_id` order, through `transition_task()`). Every composite FK in this design has its **scope** columns
(`environment_id` and the keys it scopes) NOT NULL, because a PostgreSQL
composite FK does not check a row in which any referencing column is NULL.
The pointer columns on `task` (`claim_plan_id`, `execution_id`) and on
`event` (`build_id`, `plan_id`, `execution_id`) are nullable, and Postgres
skipping the check when they are NULL is the intended "no claim" / "no
such parent". Build and deployment **lifecycle** transitions
(`complete`, `fail`, `cancel`, `exit-early`, `resume`, `/activate`, `/seal`)
are idempotent by state, not by key: a re-delivered transition finds the row
already in the requested state, returns it, and writes no event and no
timestamp. Only a transition that changes the state writes.

**The item.** Every registration route carries the same item shape:

```
{ task_id, task_namespace, task_name, version, output_uri,
  instance_hash, body,
  declared_upstreams: [instance_hash, ...] | null,   -- null = not expanded (pruned)
  observed_complete: true | false,                    -- the driver checked the target
  observed_at }                                       -- when it checked (driver clock)
```

Upstreams are named by `instance_hash`, never by `task_id`, because a scope
may hold several instances of one completion. Limit keys are not part of the
item: they are per claim, and travel on the claiming start.

**Static phase.** The driver (bootstrap, resident engine, or a rollover tick)
walks the DAG from the root instances under its scope. The walk stops at
complete tasks (target exists) and never evaluates their `requires()`. It
tracks instances by `task_id` and raises `InstanceConflictError` at the
trigger if two constructions of one `task_id` differ. It then registers:

```
POST /builds/{id}/plans
  { plan_id, deployment_id, settings, roots: [<items, declared_upstreams = null>] }
  -> lookup-or-create by (build, scope); the roots are admitted first, unexpanded
     (is_root, admitted_by = root); 409 root_instance_conflict if the existing plan's
     root instances differ; the first plan of a build is activated here
POST /plans/{plan_id}/members   (chunk, ≤1000 items, post-order, sorted within the chunk)
  -> one transaction per chunk. One instance twice in a chunk must be the same item
     (400 duplicate_item otherwise; observed_at aside). On a sealed plan only a re-delivery
     (a member it holds, same instance: a no-op except for its observation) or a discovery
     job's result (the first expansion of a member, and what it reaches in the chunk) may
     land; anything else is 409 plan_sealed. For each item:
     task            insert-if-absent by task_id; an existing row must agree on namespace, name,
                     version and output_uri (409 task_identity_conflict otherwise — all four are
                     identity-level, and first-write-wins would leave the row naming the wrong class)
     task_instance   insert-if-absent by (scope, instance_hash) (409 instance_body_conflict)
     edges           insert-if-absent for declared_upstreams; each upstream must exist as an
                     instance in this scope (400) and is admitted to the plan if it is not
                     yet a member (admitted_by = closure; 409 instance_conflict if the plan
                     holds another instance of that task). An already-expanded instance whose
                     declared set differs from its recorded static edges gets the new edges
                     appended and a TASK_STRUCTURE_DIVERGED event — only when new static
                     edges actually land, so a re-sent or subset declaration records nothing:
                     edges only grow, and a within-scope divergence is a contract breach that
                     over-gates (v1's rule)
     expanded_at     set when declared_upstreams is a list (possibly empty); untouched when null
     plan_member     insert-if-absent by (plan_id, task_pk); 409 instance_conflict otherwise
     status          observed_complete = true and no live claim -> COMPLETED (TASK_OBSERVED_COMPLETE);
                     if the task was RUNNING with a lapsed claim, that claim is closed on the ledger
                     first (claim_outcome = lapsed) — whatever moves a task off RUNNING closes the
                     current execution's claim, always, in transition_task();
                     observed_complete = false and status COMPLETED -> PENDING (TASK_INVALIDATED;
                     task.completed_at is cleared, as the completion is withdrawn)
                     -- in this transaction, so no downstream in a later chunk can run against
                     -- a status the driver has already seen to be false; and only if the
                     -- task's status_at (for an invalidation: task.completed_at, the timestamp of
                     -- the completion being withdrawn) precedes the item's
                     -- observed_at, so a delayed duplicate cannot undo a completion that
                     -- happened after the driver looked. observed_at is the driver's clock:
                     -- a value ahead of server time by more than a few seconds is refused
                     -- (400 clock_skew), because forward skew is the only direction that
                     -- could pass this check wrongly; backward skew only makes an
                     -- observation look older, which skips it. The residual worst case is
                     -- a spurious re-run that re-produces the same output, never wrong
                     -- output (see decisions.md, Copilot round 2)
     event           TASK_PENDING (new task) or TASK_REFERENCED, gated on the member insert
POST /plans/{plan_id}/seal
  -> first runs the closure step (below), in the seal's transaction: an edge another plan added
     from a shared instance to an instance this plan does not hold is admitted rather than
     failing a correct seal; a conflict the closure finds fails the build and refuses the seal
     (409 instance_conflict, the build failure committed)
  -> then verifies: every root member is expanded or COMPLETED (a root whose target already existed
     is admitted unexpanded and stays so until an invalidation makes it a discovery job);
     every edge from a member has its upstream as a member (closure holds); plan.deployment_id
     is still the app's current deployment, for a Modal deployment — a local one is
     authoritative for its own plans and never superseded
     (rollover only moves forward — checked here, not only at the start); no plan with a
     higher generation exists for this build (409 plan_superseded otherwise: the latest
     request wins, and its roots are already discovery jobs any tick can finish);
     then sealed_at = now(), and if this plan is a replacement, activated_at = now() and the
     previous active plan gets superseded_at, in the same transaction
```

Roots first, unexpanded, makes a crash at any point recoverable: any tick
finds the roots as discovery jobs (below) and finishes the static phase. A
tick that finishes someone else's unsealed plan treats it as a resume: before
sealing it re-observes the targets of the plan's unexpanded members and sends
the observations, so a member recorded COMPLETED by the crashed driver whose
target has since vanished is invalidated rather than sealed over.
Each chunk is self-consistent (an instance lands with its edges; its
upstreams exist and are members), so a partially registered plan is closed
under dependencies for everything it contains; the frontier may act on an
unsealed plan, and build completion requires `sealed_at`. Every route is a
no-op on re-delivery: ids are client-minted, inserts are `DO NOTHING …
RETURNING`, events are written only when the insert happened.

**Closure is kept as a mechanism**, not just a flag. Edges and `expanded_at`
belong to the instance, which scope-mate plans share; membership belongs to
the plan. So another plan's yield (or a later expansion of an instance this
plan admitted unexpanded) can add an edge from one of _my_ members to an
instance that is not _my_ member, and the frontier must not gate on a
member it does not hold. Every frontier read therefore starts with a
closure step: admit, in one transaction, every upstream instance reachable
over edges from the plan's members that is not yet a member, whatever its
status (`admitted_by = closure`; a COMPLETED upstream is admitted too, so
membership and edges always agree and the seal check above holds); a
conflict found there fails the build
naming both members. It is a recursive query over `task_instance_dependency`
∪ `plan_member`, and it is what v1 ran only at stall.

**Dynamic phase.** A worker whose generator yields sends **one request per
yield batch**:

```
POST /plans/{plan_id}/members/{task_id}/yield
  { execution_id, deployment_id, batch_id,
    items: [<items, post-order, the yielded children and their static closure>],
    yielded: [instance_hash, ...],
    suspend: true | false }
```

In one transaction: `deployment_id` must equal the plan's (409
`deployment_mismatch`); the parent's `task` row is locked first (the same
lock a transition takes), and a batch already applied for this
`execution_id` and `batch_id` — a `TASK_YIELDED` event whose typed
`batch_id` column carries a unique index on `(execution_id, batch_id)`, so
two concurrent retries cannot both miss the lookup — is replayed with its
stored result rather than re-checked — a `suspend: true` yield releases the
claim,
so the plain execution check would otherwise refuse the worker's own retry
after a lost response; otherwise `execution_id` must be the task's current
execution (else recorded, `report_applied = false`, 409); the items land
exactly as a
static chunk; parent→child edges to every `yielded` instance are inserted
with `is_dynamic = true`; with `suspend: true` the parent's `TASK_SUSPENDED`
is applied and its claim released. Very large yields may be split into
several requests, each self-consistent; the parent stays RUNNING and claimed
until the last one carries `suspend: true`. The reactive worker sends
`suspend: true` (its container exits); the resident engine sends `suspend:
false` and keeps the claim while its in-process generator waits (D11). Both
engines use this one route in this one order — v1 had them in different
orders. A failure to register is **not** swallowed: the worker reports
`TASK_FAILED` with the error rather than suspending a parent with no
children; an `instance_conflict` here is non-retryable, and the server
applies it: the items roll back to a savepoint, the parent gets `TASK_FAILED`
with the conflict named, and the batch is refused 409 `instance_conflict`.
Every `yielded` hash must be one of the batch's items (400
`unknown_yielded_instance`); the yielded instances are admitted
`admitted_by = dynamic`, their closure `static`. A refused batch is recorded
without a typed `batch_id`, so it can never be replayed as applied.

**Invalidation: the registry follows the world.** The only path out of
COMPLETED is discovery's `observed_complete: false` on a task whose registry
status is COMPLETED, applied in the chunk transaction above with the
`observed_at` guard, refused while a live claim exists, and recorded as
`TASK_INVALIDATED{reason: target_missing, observed_at, plan_id}`. There is
**no operator route** that declares a task incomplete: an operator who needs
a re-run acts on the target (deletes it), then triggers a build, which
observes and invalidates. `stardag tasks check <task_id>` is a convenience
that runs `complete()` locally and reports the observation, since the SDK has
target access.

The narrow case this serves is "the target is gone, the promise is
unchanged": a retention policy or bucket lifecycle deleted it, a developer
cleared a directory, a corrupt partial output was removed. Re-running then
produces, by the task-id contract, the same output, so the record stays
truthful: downstream tasks were produced from an output identical to the one
that exists again, and the ledger shows the completion by execution E1, the
invalidation, and the re-completion by E2 in order. **"I want to fix its
output" is not a use of invalidation**, and the docs say so: changing what a
task produces is a change of promise, so it needs a new `task_id` — bump
`__version__` or add a significant parameter. That cascades on its own,
because a downstream task that takes the upstream as a parameter hashes its
id, and it leaves the old outputs and their history intact. A cascading
"uncomplete" in the registry could do neither: the registry cannot touch
targets, other builds rely on the global completion, and "complete" can be a
compound world state.

## The runnable rule

v1: _a plan member whose registered upstreams are all COMPLETED is
runnable._ The summary comment's finding stands after verification with a
correction: v1 has **no** window for a yielded child today because the
worker registers children with their closure before the edges, but that
guarantee is an ordering discipline in SDK code, it is different in the two
engines, and it has three neighbours that are real today: complete-at-
discovery tasks are registered PENDING with zero edges until a later
"mark complete" call lands (a concurrent tick can run them); a swallowed
registration failure leaves a SUSPENDED parent with no children edges; and
a COMPLETED task cannot be reset at all, so "completed becoming incomplete"
fails the other way (it can never be rebuilt).

v2 makes the guarantee a stored fact. For a member `m` of the active plan
with instance `i` and task `t`:

```
ACTIONABLE       := {PENDING, SUSPENDED, INTERRUPTED, CANCELLED, SKIPPED}
                    ∪ {RUNNING with claim_expires_at <= now()}   -- a lapsed claim is taken over
                                                                 -- (FAILED: fail_mode decides)
discovery_job(m) := t.status <> COMPLETED AND m.excluded_at IS NULL
                    AND i.expanded_at IS NULL
runnable(m)      := t.status ∈ ACTIONABLE AND m.excluded_at IS NULL
                    AND i.expanded_at IS NOT NULL
                    AND NOT EXISTS edge(u -> i) WITH u.task.status <> COMPLETED
running(m)       := t.status = RUNNING AND claim live            -- whoever holds it
```

evaluated after the closure step above, over the active plan. A discovery
job is executed by the tick (or the resident driver): it rehydrates
`i.body` under the plan's scope, evaluates `requires()`, and registers the
result through `POST /plans/{plan_id}/members` (same route, same
idempotency), which sets the flag. A discovery job that fails — the class
cannot be imported in the tick (`UnknownTaskClassError`), or `requires()`
raises — **excludes the member** (`excluded_reason = discovery_failed`, with
the error) rather than failing the global task: the failure is a property of
this plan's code, not of the promise. Exclusion cascades to the root and
fails the build per fail mode, and an excluded member is never selected as a
discovery job again, so nothing is retried every tick. Upstream completion is read globally
on `task`, as in v1; edges are read on the instance, i.e. per scope, as in
v1. SUSPENDED stays "run it from scratch once its dynamic children are
COMPLETED" (verified v1 behaviour, kept). A claiming start names its plan
and its execution id; it is refused with 409 `build_not_running` if the
plan's build is not RUNNING (the frontier of such a build lists no runnable
members and no discovery jobs), and with 409 `plan_superseded` if the plan is
not active — after first checking whether the same execution already holds
the claim, so a retried granted start is a no-op, not a loss — with 409
`member_excluded` for an excluded member, with 409 `task_not_actionable` for
a status outside ACTIONABLE (FAILED in particular: the fail mode decides, and
only `retry` moves it back to PENDING), and it **re-checks the runnable
predicate inside its own transaction** (the instance expanded, all upstream
tasks COMPLETED, read under the task row lock), refusing 409
`upstream_incomplete` otherwise — with `reason: not_expanded` for an
unexpanded member, whose upstreams are not yet known. The frontier is a hint; the claim is the
decision. This closes the window where a tick reads a downstream as runnable,
an observation then invalidates its upstream, and the tick claims the
downstream against a target that no longer exists (S39).

Blocked-by-failure propagation (`skip-blocked`) is the same recursive walk
as v1, over instance edges within the plan; exclusion propagates the same
way.

**Build status** stays a stored column driven by build events, as in v1
(a repeat of the current state is a no-op), except that a terminal build
status is sticky; later lifecycle reports are recorded, not applied: a
`complete`, `fail`, `cancel` or `exit-early` against a COMPLETED, FAILED or
CANCELLED build is 409 `build_terminal`, recorded with `report_applied =
false`, and `resume` is the way out. The
server does not flip it inside task transactions (that would lock every
build holding the task on each completion, the inversion `_flag_builds`
avoids with `SKIP LOCKED`). What changes is that completion is **verified**:
the frontier response carries `plan_complete` (= sealed, and every
non-excluded member COMPLETED) as a diagnostic, and `/complete` runs the
closure step, then **recomputes the same predicate in its own transaction** and is refused with 409
`plan_incomplete` unless it holds or `force` is set (the operator override
that v1's unchecked `/complete` was; `force` overrides outstanding members,
**never a missing seal and never an excluded root** — an unsealed plan is a
request not yet fully stated, an excluded root is a request that cannot be
met, and the way out of either is `fail` or `cancel`). To be atomic with a
concurrent observation, `/complete` reads the active plan's member task rows
`FOR SHARE` in `task_id` order inside its transaction, which serialises with
the `FOR NO KEY UPDATE` an invalidation takes (the refusal names its
`reason`: `not_sealed`, `root_excluded` or `members_incomplete`). `complete`,
`fail` and `cancel` release the claims held by **any** of the build's plans,
superseded ones included, in one server-side place — v1 released on two of
three — through `transition_task()`: `claim_outcome = released`, and the task
goes **CANCELLED** whichever terminal transition it was, because the build
stopped wanting it, which is not a result ("revocation is not a result":
CANCELLED is ACTIONABLE, so any other build holding the task runs it). The
execution's `ended_at` is untouched — the container may run on — and its
report is late from then on. `exit-early` releases nothing: a resident
build's in-flight tasks keep reporting, and if the process is gone their
claims lapse like any other worker's. A claiming start takes the build row
`FOR SHARE` before its task row, which is what makes terminal transitions and
deletes (`FOR NO KEY UPDATE` on the build) a synchronisation point for claims:
a start in flight is waited for, and a start arriving after one reads the new
status and is refused with `build_not_running`. A plan's roots are the build's request:
`POST /builds` records `root_task_ids` (required, non-empty) and `create_plan`
refuses roots whose task ids are not exactly those (400 `root_mismatch`).

A build is **resumable** when a plan can be created or reactivated under the
caller's scope. An existing sealed plan is reused **without re-evaluating
`requires()`** (structure is trusted within a scope), but the driver still
walks the members' targets and sends `observed_complete` for each, and
requests the retries its fail mode allows (v1's `retry_failed`), so a
FAILED member is reset and a vanished output is invalidated on resume. An
unsealed plan is completed by re-sending; none means discovery runs.
`POST /builds/{id}/resume {deployment_id, settings}` makes the build RUNNING
(`BUILD_RESUMED`) and looks the plan up by the caller's scope: the active one
is reused; one that was active before and has since been superseded is
reactivated (the active plan superseded, in one transaction, after the same
deployment-currency check a seal makes, Modal deployments only, and under
the seal's latest-request rule: while a later request for the build is
still registering — a higher-generation plan never activated — the
reactivation is refused 409 `plan_superseded`); an unsealed replacement is
left to its seal; none is returned as none, and the driver creates it.

## Rollover

A tick starts, reads its own `STARDAG_DEPLOYMENT_ID`, and compares it with
the active plan's `deployment_id` (an id comparison, not a code-id prefix).
If they differ:

1. It checks that the registry's current deployment for the app is its own;
   if not, it exits `superseded` (rollover only moves forward, and only on
   the registry's record).
2. It rehydrates the active plan's root instances from their bodies under
   its own code, recomputes their `task_id`s and compares them with
   `build.root_task_ids`; a difference fails the build with "re-trigger it
   as a new build".
3. It runs the static phase under `(own deployment, plan.settings_hash)`
   — lookup-or-create; a plan for that scope may already exist and be
   sealed, in which case nothing is discovered — and seals. `/seal`
   re-checks that the plan's deployment is still the app's current one and
   refuses otherwise (under the app's advisory lock, shared to its commit;
   create and `/activate` take it exclusively, so a seal and an activation
   serialise), so two ticks under two new deployments cannot leave
   the build on the older code; the winner's seal supersedes the old plan in
   the same transaction.

An old tick reads the frontier, sees the active plan's deployment is not its
own, and exits `superseded`. An execution started under the old plan runs to
completion; completion is global, the new plan sees COMPLETED and moves on.
If it **yields**, its `/yield` names the old plan id and the old deployment:
the deployment matches the old plan, so the server accepts the instances and
edges (they are true facts about the old scope, useful to any scope-mate)
and the membership into the superseded plan (inert), and applies the
suspend. The new plan's member for that task has its own instance with no
dynamic edges in the new scope, so SUSPENDED is runnable there: the next
tick claims it cleanly (a suspended task holds no claim) and restarts it
under the new code. The old children keep running as accepted duplicate
work. No detection step, no double execution.

If the new deployment's record never reaches the registry (the deploy
succeeded, the `POST /deployments` failed and the operator ignored the
non-zero exit), every tick of the new code exits `superseded` and the app's
reactive builds stall; `stardag modal deployments` shows the gap and the
record can be re-sent (same client-minted id, idempotent).

An **orphaned execution** is an `execution` row with `ended_at IS NULL`
whose `plan_id` is not the active plan of its build. `stardag builds stop
--not-in-current-plan` lists and, on confirmation, cancels them through the
existing stop path; nothing does it automatically (STA-67's cancellation,
STA-80's filter).

Two deferrable refinements to `stardag modal deploy`: spawn one tick per
running reactive build of the app right after recording the deployment, so
the rollover happens now; and report how many RUNNING members have a
generator `run` and will restart under the new code.

### Claim × plan invariants (STA-74, restated on v2)

- The claim is on `task`; it names a `plan_id` and an `execution_id`. A
  plan being superseded changes nothing about a claim it holds.
- A claiming start names its plan; it is granted only if the plan is the
  build's active plan (409 `plan_superseded` otherwise). A superseded tick
  therefore cannot start new work.
- Every report (complete, fail, suspend, interrupt, preempt, skip, yield)
  names its `execution_id`; it changes the task's status when that is
  `task.execution_id`, **whether or not the claim has lapsed**: a lapsed
  claim still names its execution until a claiming start takes it over, so
  a worker finishing seconds after expiry keeps its completion (the target
  exists). A **late report** — one naming an execution whose
  `claim_released_at` is already set because the claim was taken over,
  closed by an observation (`lapsed`) or released by a build transition —
  writes that execution's ledger end (`ended_at`, `outcome`) and is recorded
  as an event with `report_applied = false`; it never touches `task`. Anything else
  (unknown execution, second terminal report) is recorded and refused. The
  holder's **non-claiming start** (its self-report, carrying executor details)
  follows the same rule: applied while it names the current execution, lapsed
  or not; late only after a takeover. Every timestamp a transition writes
  (`status_at`, `completed_at`, `started_at`, the claim's expiry, the ledger's
  ends) is stamped **after the task row lock is granted**, so the `observed_at`
  guard compares against the real completion time, not the time its caller
  began waiting. One
  `transition_task()` implements this for every event type — v1 guarded four
  of eight routes, and the lock-release route committed a completion before
  its ownership check.
- A report on the current execution, and the holder's self-report start,
  come through the plan the claim was granted through: under any other plan
  (`task.claim_plan_id` differs from the route's `plan_id`) they are 409
  `not_claim_holder` and leave no trace — no ledger end, no event — so the
  execution's one terminal report is not spent by a mis-routed call.
- Completion from any plan is completion for all; the new plan never waits
  on the old plan's _plan_, only on the task's global status.

## Wake-ups, limits, locks

Unchanged in mechanism, re-keyed: "builds holding a task" is `plan_member`
of active plans (not "any event in the build"); flagging on every transition
(still `SKIP LOCKED`) and on limit-slot release, `wake-candidates`, the
scheduler lease (its own columns on `build`, no longer on the lock table)
and the watchdog carry over. Concretely: every status change made by
`transition_task()` sets `needs_tick_at` on the other RUNNING reactive
builds whose active plan has a non-excluded member for the task (their
`build_wake` rows `FOR NO KEY UPDATE SKIP LOCKED`, ordered by build id; the
build row is read, never locked, so a claim in flight on the build does not
hide it — only a concurrent flagger, `notify` or `wake-candidates` holding
the same wake row does, and that writer is setting the flag); a move out of RUNNING also
flags those whose active plan has an actionable member with a
`task_limit_key` on one of the task's keys. `POST /builds/{id}/notify` flags
the caller's RUNNING build and reads the lease after the flag's commit;
`POST /builds/wake-candidates` hands out at most 20 flagged builds with no
live lease, not handed out within 120 s, oldest flag first. Concurrency slots are `task_limit_key` rows
joined to a live claim. The `distributed_lock` table and `/locks` routes are
retired: the claim is the only mutual exclusion (D11); the one thing kept
from them is renewal, as `POST …/tasks/{task_id}/claim/renew {execution_id}`
for in-process executions, whose driver is alive to call it; the renewal is
granted only if that execution holds the live claim, under the same row lock
a claiming start takes, so a delayed resident process cannot extend a claim
another execution has taken over. It also locks the task's limit rows (key
order, as a claim does) and re-reads the clock before it extends the
expiry, so a claim sharing a key never counts the holder lapsed and takes
its slot while the renewal is in flight.

## Scenario checklist

Every scenario names the column or constraint that decides it, and the
test tier that pins it (`unit`: SDK only; `api-pg`: the FastAPI suite on
Postgres; `resident`: the in-process engines with the fake registry; `live`:
the registry-live tier). See `plan.md`, Methodology.

| #         | Scenario                                                                                                     | Outcome                                                                                                                                                                                                                                                                                                   | Decided by                                                            | Test tier     |
| --------- | ------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------- | ------------- |
| S1        | Two builds, overlapping DAGs, different scopes, diverging upstreams                                          | Both plans admit the shared completion with their own instances; whichever claims it first runs _its_ instance body; the other waits on the global status. Upstream sets may differ; duplicate upstream work accepted.                                                                                    | `task.claim_*` global; edges per instance                             | live          |
| S2        | One `task_id`, two instances, one plan                                                                       | 409 `instance_conflict` at the server; `InstanceConflictError` at the trigger first, naming the differing fields                                                                                                                                                                                          | `plan_member` PK `(plan_id, task_pk)`                                 | api-pg + unit |
| S3        | Running build, new deployment for the app                                                                    | New plan under the new deployment; old tick exits `superseded`; old executions finish; yields under the old plan restart under the new code                                                                                                                                                               | `plan.superseded_at`, `STARDAG_DEPLOYMENT_ID` vs `plan.deployment_id` | live          |
| S4 (§4.1) | Yield committed before the children's closure                                                                | Impossible: `/yield` lands children with closure in one transaction; a child never exists in a plan with `expanded_at IS NULL` unless pruned-complete, and then it is not runnable (it is a discovery job if its status is not COMPLETED)                                                                 | `task_instance.expanded_at`, single `/yield` transaction              | api-pg        |
| S5 (§4.2) | A COMPLETED task's target is deleted                                                                         | Discovery finds the target missing → the chunk item carries `observed_complete: false` → COMPLETED → PENDING in the same transaction as the instance and its edges, before any downstream chunk lands; runnable once its upstreams are complete. Without discovery seeing it, it stays COMPLETED (sticky) | `TASK_INVALIDATED` in the chunk transaction, the flag                 | live          |
| S6        | Redeploy with no code change                                                                                 | New deployment row, new scope, re-plan of running reactive builds; instances re-registered (cheap, bodies identical); no behaviour change                                                                                                                                                                 | `deployment.id` per deploy (D6)                                       | live          |
| S7        | Old-deployment execution yields after the switch                                                             | Accepted into the superseded plan (inert); parent SUSPENDED globally; new plan's instance has no dynamic edges → runnable → restarted under new code; old children run on as duplicates                                                                                                                   | `/yield` accepts superseded `plan_id`; SUSPENDED ∈ ACTIONABLE         | live          |
| S8        | Two builds, different `settings`, sharing a completion                                                       | Two scopes, two instances, one `task` row; claim decides who runs; the other reuses the result                                                                                                                                                                                                            | `settings_hash` in the scope                                          | live          |
| S9        | Local placeholder deployment vs a real one                                                                   | Cannot collide: `kind` is in the lookup; a local `code_id` equal to a Modal `code_id` is a different deployment                                                                                                                                                                                           | `deployment.kind`                                                     | unit          |
| S10       | Annotation-only difference for one completion in one plan                                                    | Same as S2: rejected with the field named ("`label='nightly'` vs `label='backfill'`") — rare, meaningless, fixed in seconds                                                                                                                                                                               | `plan_member` PK                                                      | api-pg        |
| S11       | Concurrent workers yielding into one plan                                                                    | Each `/yield` is one transaction; membership inserts are `DO NOTHING`; a conflicting instance is 409; the frontier is defined over committed batches                                                                                                                                                      | `/yield` atomicity, `plan_member` PK                                  | api-pg        |
| S12       | Complete-at-discovery task registered                                                                        | The item carries `declared_upstreams = null, observed_complete = true` → the instance lands unexpanded and the task is set COMPLETED in the same transaction (no live claim); nothing can run it in between                                                                                               | the flag + `TASK_OBSERVED_COMPLETE` in the chunk transaction          | api-pg        |
| S13       | Registration of a yield fails mid-way                                                                        | No partial children (one transaction); the worker reports FAILED with the error instead of suspending; retry budget applies                                                                                                                                                                               | `/yield` transaction; no swallow                                      | unit + api-pg |
| S14       | Resume under a different `settings`                                                                          | New plan in the same build, registered alongside the active one, activated at seal; discovery runs; completed members are reused via global status                                                                                                                                                        | `plan` unique on `(build, scope)`; `activated_at`/`superseded_at`     | live          |
| S15       | Resume under the same scope after a crash mid-registration                                                   | Plan exists unsealed with its roots as discovery jobs; any tick or a re-send finishes it; seal verifies roots expanded and closure                                                                                                                                                                        | roots-first, idempotent inserts, `/seal` checks                       | api-pg        |
| S16       | Two builds register the same brand-new task concurrently (STA-48)                                            | Both `DO NOTHING … RETURNING`; one creates, both reference; no 500, no `FOR UPDATE` on a missing row; chunk rows sorted by `(task_id, instance_hash)` so two chunks cannot deadlock on unique-index waits                                                                                                 | insert pattern, sort order                                            | api-pg        |
| S17       | Deleted build                                                                                                | Refused (409) while a plan of it holds a live claim or an execution of it is unended (`builds stop` ends them first); otherwise plans, members and executions cascade, events keep their rows with `build_id` and `plan_id` NULL                                                                          | delete guard, FK actions                                              | api-pg        |
| S18       | Operator gives up on a member (STA-104)                                                                      | `excluded_at` set; not scheduled, not gating completion; exclusion cascades to its downstream closure in the plan; an excluded root fails the build; the global status untouched                                                                                                                          | `plan_member.excluded_at`                                             | api-pg        |
| S19       | A stale worker's `/complete` for another build's running task                                                | Applied only if its `execution_id` is current; otherwise recorded, `report_applied=false`, 409                                                                                                                                                                                                            | `transition_task()` authority                                         | api-pg        |
| S20       | Root identity changed by new code at rollover                                                                | Build FAILED "re-trigger it as a new build"                                                                                                                                                                                                                                                               | `build.root_task_ids` comparison                                      | live          |
| S21       | Worker dies without reporting (OOM)                                                                          | Claim lapses at `claim_expires_at`; RUNNING-with-lapsed-claim is ACTIONABLE; the next claiming start takes it over and writes `claim_outcome = taken_over` on the old execution, whose `ended_at` stays NULL                                                                                              | ACTIONABLE definition                                                 | live          |
| S22       | Shared instance across two plans; A yields C; A is cancelled or excludes C                                   | B's frontier closure step admits C (and its closure) into B before evaluating gates; B runs C itself; no stall                                                                                                                                                                                            | closure at every frontier read                                        | live          |
| S23       | B admitted an instance unexpanded (pruned-complete); it is invalidated; A expands it first                   | B's closure step admits the new upstreams; B's discovery job for it is skipped (flag now set); no gate B does not hold                                                                                                                                                                                    | closure + shared `expanded_at`                                        | api-pg        |
| S24       | Two instances of one completion in one scope, different plans, dynamic yield (v1's `shared_structure_scope`) | The two instances do **not** share dynamic edges: B's instance is SUSPENDED-runnable while A's children run, so the pre-yield section re-runs once. Accepted cost of the second identity; only identical instances share yields                                                                           | edges on instances                                                    | live          |
| S25       | Resident `sd.build()` with thread/process pools                                                              | Every execution claims (a TTL the driver renews); the resident yield keeps the claim (`suspend: false`); settings applied for the build's duration in-process; a dead process stops renewing and its claims lapse like any other worker's                                                                 | D11                                                                   | resident      |
| S26       | Local driver with Modal workers (hybrid)                                                                     | Plans under the app's current deployment; workers' `/yield` matches; a laptop whose code differs is the user's YOLO, as before                                                                                                                                                                            | D13, `deployment_mismatch`                                            | live          |
| S27       | `settings` sets a `STARDAG_*` key or one the worker selector sets                                            | `STARDAG_*`/`MODAL_*` refused at the trigger; selector keys are overridden by `settings` (documented precedence)                                                                                                                                                                                          | trigger validation                                                    | unit          |
| S28       | Yielded child already COMPLETED but its target is missing                                                    | The child item carries `observed_complete: false` → invalidated in the yield transaction → runnable under the parent's plan                                                                                                                                                                               | chunk-transaction invalidation                                        | api-pg        |
| S29       | Yielded child RUNNING in another build                                                                       | Admitted with its edges; not runnable while the claim is live; the parent waits on the global status                                                                                                                                                                                                      | global claim                                                          | api-pg        |
| S30       | An `observed_complete: false` chunk racing a claiming start                                                  | Both lock the task row; the loser sees the other's state: the observation is skipped against a live claim (recorded, not applied), a claiming start against COMPLETED is 409 `task_already_completed`                                                                                                     | row lock, `observed_at` guard                                         | api-pg        |
| S31       | Delayed duplicate observation after the task re-completed                                                    | `completed_at` is after the item's `observed_at` → not applied, nothing changes                                                                                                                                                                                                                           | `observed_at` guard                                                   | api-pg        |
| S32       | `/seal` while a timed-out chunk retry is in flight                                                           | Seal verifies closure and roots; if the chunk had not landed, the seal is 409 `plan_incomplete_registration`; the retry lands, seal is re-sent                                                                                                                                                            | `/seal` verification                                                  | api-pg        |
| S33       | Two ticks under deployments D2 and D3 both roll over                                                         | Both register plans; `/seal` re-checks "current deployment is mine": D2's seal is refused, D3's supersedes the old plan; D2's tick exits `superseded`                                                                                                                                                     | `/seal` deployment check                                              | live          |
| S34       | Discovery job for a class the tick cannot import                                                             | The member is excluded (`discovery_failed`, with the error); exclusion cascades to the root and fails the build per fail mode; an excluded member is never a discovery job again                                                                                                                          | discovery-job failure rule                                            | api-pg        |
| S35       | Duplicate delayed `/fail` after an operator `retry`                                                          | The execution already has `ended_at`; the duplicate is recorded, `report_applied = false`, not applied                                                                                                                                                                                                    | one terminal report per execution                                     | api-pg        |
| S36       | Retried claiming start whose first delivery was granted, seal landed in between                              | The same execution already holds the claim → granted (no-op), not `plan_superseded`                                                                                                                                                                                                                       | ordering of the two checks                                            | api-pg        |
| S37       | Deploy recorded late or never                                                                                | New-code ticks exit `superseded` until the record exists; `stardag modal deployments` shows it; re-sending the record is idempotent                                                                                                                                                                       | client-minted deployment id                                           | live          |
| S38       | Re-trigger of a build with a root whose non-significant field changed                                        | 409 `root_instance_conflict` under the same scope — a build is one request; start a new build                                                                                                                                                                                                             | roots recorded on the plan                                            | api-pg        |
| S39       | A tick reads downstream D as runnable, then an observation invalidates D's upstream T                        | The claiming start for D re-checks its upstreams under the task row lock and is refused 409 `upstream_incomplete`; the frontier was a hint, the claim is the decision                                                                                                                                     | claim-time re-check                                                   | api-pg        |

## What this costs

- A redeploy of unchanged code re-plans running reactive builds (D6).
- Instances are stored per scope: N deployments × the DAG size in
  `task_instance` rows and bodies. Retention of retired scopes is STA-68.
- Two hashes computed per task construction.
- Every process of a build must know its `plan_id` and apply `settings`;
  both travel as environment variables, which every process boundary already
  carries, and nothing else is transported.
- A registry cannot be upgraded in place: v2 is a new line and existing
  registries start empty.
- Two instances of one completion in one scope do not share dynamic edges
  (S24): the pre-yield part of a suspending task can run once per distinct
  instance.
- Every frontier read runs a closure query before evaluating gates.
- In-process executions hold claims with a TTL that the resident driver
  renews (as v1 renewed the distributed lock), so a dead resident process
  lapses like any other worker; the renewal is one request per interval per
  running task.

## Superseded

`scope-keyed-dependency-structure.md` is superseded by this note; its
"abandoned paths" section remains the record of the two designs before it.
`execution-claims-and-liveness.md` stands, re-read with the claim on `task`
naming a `plan_id`. `executions-as-records.md` stands except its banner: the
table it declined to build is built here, as a ledger.
