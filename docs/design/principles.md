# Principles

What stardag promises, what it asks of the code it runs, and the limits it
accepts in return. The principles below hold for the v2 line (the registry
entities of [registry-v2/design.md](registry-v2/design.md)); where a design
note goes deeper, it is linked.

Each principle is one sentence, the reasoning behind it, and the limit it
implies. A limit is not a bug waiting for a fix: it is the price of the
principle, stated so that nobody pays it by surprise. The last section lists
the costs stardag accepts and the things it deliberately does not do.

This note replaces the eight principles of the September 2026 architecture
review, which were written against v1's structure scopes and per-build
config; the ideas survived, the mechanisms did not.

## A task id is a promise about output

**A task id hashes the task's name, namespace, version and significant
parameters, and it promises the output — nothing about the code that
produced it or the upstreams it was built from.**

Completion and the execution claim are both keyed on the task id, globally
per environment, so every build and every scope that asks for the same id
asks for the same output. The id never hashes code: keeping "same id, same
output" true when the code changes is the author's job, done by bumping
`__version__` or adding a significant parameter. Custom `"hash"`-mode
serializers and `compat_default` exist so the author can shape this
identity, and they apply to it only.

_The limit:_ stardag cannot detect a broken promise. If a code change alters
what a task writes without changing its id, every build treats the old
output as the new one. Changing what a task produces is a change of promise
and needs a new id.

## A target makes completion a fact about the world

**A task is complete when its target exists; the registry follows that
fact, and only that fact.**

Discovery checks targets and stops at complete tasks, so a DAG is never
walked past work that is done. The registry records what discovery
observed. The only path out of COMPLETED is discovery observing the target
missing, recorded as `TASK_INVALIDATED`; there is no operator route that
declares a task incomplete. An operator who wants a re-run acts on the
target (deletes it) and triggers a build, which observes and invalidates.
`stardag tasks check` runs `complete()` locally and prints the observation
next to the registry's status, but reports nothing.

_The limit:_ the registry can be behind the world, never ahead of it, and it
cannot be told what the world does not show. There is no "uncomplete",
cascading or otherwise: the registry has no access to targets, other builds
rely on the global completion, and "complete" can be a compound world state.

## A build is a request, not an owner

**A build asks for its root tasks to be complete; which build completes a
task, or any of its upstreams, is irrelevant.**

A build holds tasks through a plan (below) but owns none of them. Two builds
asking for overlapping DAGs share the overlap: a task one of them completes
is complete for the other. The only thing that must never happen between
builds is two concurrent executions of one task.

_The limit:_ a build cannot reserve a task, and cannot keep another build
from completing it first under a different construction. A build that needs
its own copy of a result needs a different task id.

## The claim is the only coordination, and revocation is build-scoped

**An execution claim — RUNNING plus an expiry, taken atomically on the task
row — is the only cross-build coordination, and a build can release only
the claims it holds.**

A claiming start names its plan and its execution id; it is granted when
the task is actionable, its upstreams are complete (re-checked under the
lock) and any concurrency-limit slot is free. Every report names its
execution and is applied only while that execution is the task's current
one. Every execution claims, in-process ones included, with a finite
expiry; there is no lock table and no heartbeat beyond the renewal a live
resident driver sends. A build that stops wanting a task releases its claim
(the task becomes CANCELLED, which is actionable for every other build
holding it); it cannot touch a claim another build holds
(`not_claim_holder`). Nothing revokes an execution automatically: the
server never reaches an execution backend, and cancellation is cooperative.
A worker checks at its checkpoints whether it is still wanted.

_The limit:_ a released claim does not stop a container. A worker that
never reaches a checkpoint runs to the end of its `run()`, and a hard stop
is a human action with the backend's own credentials, `stardag builds
stop`.

## Structure belongs to code under a scope

**Which upstreams a task has is a fact about the code under a scope, not
about the task id; within a scope edges only grow, so gating can only
over-approximate.**

A task _instance_ is a construction of a task under a scope `(deployment,
settings)`, stored with its full parameter body. Its dependency edges belong
to the instance: they are recorded when its `requires()` is evaluated (and
when a running task yields dynamic dependencies) and never retracted. A
plan gates a member on every recorded edge of its instance, so a stale edge
can only make a task wait for work it did not strictly need. Under-gating —
running a task before an upstream it reads — is the only route to wrong
output, and it is unreachable within a scope. Sharing edges between
scope-mates is licensed by the snapshot contract: mutable inputs are
snapshotted and referenced by a parameter.

_The limit:_ a within-scope divergence of a task's declared upstreams can
only come from user code breaking the environment-variable or snapshot
contract. The registry appends the new edges and records
`TASK_STRUCTURE_DIVERGED`; it does not refuse, and it does not detect a
breach that adds no edge.

## Two hashes and one flag

**A task has two identities — the task id over its significant parameters,
and the instance hash over all of them — and one flag,
`StardagField(significant=...)`, decides which parameters are which.**

A `significant=False` field is an ordinary parameter: passed at init, stored
on the instance body, rehydrated from it, and part of the instance hash but
not the task id. The instance hash is the hash of the canonical body itself
(sorted keys, sorted sets, compact separators), so hash and body are one to
one by construction and have no hash mode to customise. What stardag demands
of the body is round-trip stability, `dump(validate(dump(x))) == dump(x)`,
checked once per distinct instance at registration
(`UnstableSerializationError`). Within one plan there is one instance per
task id: two different constructions of one completion in one request is
`InstanceConflictError` at the trigger, or a non-retryable conflict found
later. The instance hash is never a public identifier on its own;
instances are addressed by row id or by the full scope triple.

_The limit:_ a changed class default is a new instance hash, because the
body includes fields not set at init. Under a new deployment that is a new
scope anyway; within one scope it happens only when code changed under a
pinned `STARDAG_CODE_ID`, which is the user's pin to keep honest.

## Settings may change structure and execution, never output

**Settings are environment variables chosen per build; they may change which
upstreams a task has and how it runs, never what it writes — and a process
applying them serves one build at a time.**

`sd.build(settings=...)`, `build_trigger(settings=...)` and
`stardag build --settings KEY=VALUE` take a flat string-to-string mapping,
applied as environment variables in every process of the build and part of
the scope, so structure is deterministic within a scope. They are meant to
be read through a pydantic-settings class at run time, not import time.
Keys starting `STARDAG_` or `MODAL_` are refused; stardag's own identifiers
are written last and cannot be overridden. Environment variables are
process-global, so deployed ticks and workers run one input per container
(a `max_concurrent_inputs` above one on them is refused at deploy), and a
second build entering a process while another build's settings are
installed raises `SettingsError`.

_The limit:_ nothing checks that a setting stays out of output. Completion
is global, so a setting that changed output would let one build reuse
another's different result; anything that affects output belongs in a
significant parameter. Settings are not for credentials. The one-build-per-
process rule costs containers: a lingering tick holds one of its own.

## A build follows the live deployment

**A running build follows the app's current deployment, and a local
deployment is never current.**

Every `stardag modal deploy` is a new deployment with a client-minted id
(`STARDAG_DEPLOYMENT_ID`, baked into the image), recorded before the deploy
and activated after it; the current deployment of an app is the activated
one with the highest generation, so a late record cannot roll a build back.
The first tick on new code re-plans the build under the new scope, and a
tick on old code exits `superseded`. A local build plans under a `local`
deployment derived from its code id (`STARDAG_CODE_ID`, else a clean git
SHA, else a fresh id); a local deployment is authoritative for its own plans
and never superseded. A driver whose tasks run on a Modal app plans under
that app's current deployment; that its own code matches is on the user.

_The limit:_ a redeploy of unchanged code is still a new scope, so running
builds re-plan and a suspended task's pre-yield work can run again. Where
the registry cannot guarantee that a driver's code matches a deployment, it
takes the simplest mechanism and states the responsibility rather than
trying.

## The registry is the scheduler state

**Everything a reactive build needs to be woken is in the registry: a
status write flags the builds it concerns, and time-based recovery happens
only through the watchdog.**

A task transition flags every other running reactive build whose active
plan holds the task, and a move out of RUNNING also flags builds queued on
its concurrency keys; the scheduler hands each flagged build out at most
once per window. The server never spawns user code. A claim lapsing is the
one event no write announces, and the watchdog's periodic sweep is the only
thing that acts on time; a tick acts on flags, never on elapsed time.

_The limit:_ recovery from a dead worker is bounded by the claim's expiry
plus the watchdog's period, not by how quickly the death could have been
noticed.

## Every write is idempotent

**Every write route can be re-delivered without effect, and client-minted
ids are what make that possible.**

Build, plan, execution, deployment and yield-batch ids are minted by the
client before the request that creates them, so a retry after a lost
response names the same thing. Registration inserts only what is absent and
writes events only for rows it inserted; lifecycle transitions are
idempotent by state; a yield already applied for its `(execution_id,
batch_id)` is replayed with its stored result; the creation quotas charge
only for rows a request inserted. Target existence is the ground truth the
system converges from, so a registry write may fail and be retried without
losing correctness.

_The limit:_ an id is a promise the client keeps. A client that mints a
fresh id for a retry makes a second attempt, not a retry, and the registry
cannot tell the difference.

## Non-goals and accepted costs

- **Duplicate upstream work across scopes.** Two builds in different scopes
  (different deployments or settings) may complete one task over different
  upstream sets. Completion is shared; the upstream work each scope needed
  is not deduplicated.
- **Distinct instances of one completion do not share yields.** Dynamic
  edges belong to the instance that yielded them, so two instances of one
  task id in one scope each run their pre-yield part.
- **A redeploy re-plans running builds**, even for unchanged code, and a
  task that had yielded can re-run its pre-yield work under the new
  deployment. Rollover is cheap and rare in production; deduplicating
  deployments by code id was considered and not taken.
- **No operator "uncomplete".** The registry follows the target (see
  above); a re-run means deleting the target, and a changed output means a
  new task id.
- **No automatic cancellation of orphaned executions.** An execution left
  running under a superseded plan is listed by `stardag builds stop
--not-in-current-plan` and ended there, on confirmation, by a human.
  An execution that cannot be stopped can be ended in the ledger as `lost`
  (`--mark-lost`), which records that the container may still be running.
- **Postgres only**, 15 or newer. The API suite runs on Postgres, and the
  schema depends on it (column-list `ON DELETE SET NULL`, advisory locks,
  row-lock modes).
- **No v1 compatibility.** v2 is a new line: a v1 SDK cannot talk to a v2
  registry or the reverse, and an existing registry starts empty. There are
  no version gates, shims or dual writes.
- **The `observed_at` guard compares two clocks.** An observation of a
  missing target is ignored when it predates the task's completion; the
  server refuses an `observed_at` ahead of its own clock by more than a few
  seconds. The residual worst case under clock skew is a spurious re-run,
  which re-produces the same output under the task-id contract, never wrong
  output.
- **One build per process for ticks and workers.** Settings are process
  environment variables, so a deployed tick, worker or bootstrap serves one
  build at a time and scales by containers, not by concurrent inputs.
