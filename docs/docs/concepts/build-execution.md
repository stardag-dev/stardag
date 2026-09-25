# Build & Execution

How Stardag turns a task graph into work — independent of where that work
runs. Everything on this page holds for a laptop, a CI runner, a cluster
or Modal. The Modal integration adds two things on top (detached
execution and a scheduler with no resident process); those have their
[own page](modal-orchestration.md).

## The model

Stardag builds bottom-up, Makefile-style:

1. Start at the requested task.
2. Is it complete? If its output exists, stop.
3. Otherwise make sure every dependency is complete, recursively.
4. Run it, persist its output.

Two properties follow and hold in every execution mode:

- **Completeness is target existence.** A task is done when its output
  exists in storage — not when a scheduler says so. Storage is the ground
  truth, which is what makes resumption, retries and de-duplication safe:
  re-running a build never re-executes work whose outputs exist.
- **Re-execution is idempotent.** Tasks (including those with dynamic
  dependencies) are written so that running them from scratch is safe.
  The engine relies on this whenever an execution crosses a process or
  machine boundary.

## The build functions

`sd.build(task)` / `await sd.build_aio(task)` is how you run a build. It
discovers the graph, then runs a scheduling loop: submit every _ready_ task
(all dependencies complete) to a **task executor**, process results as they
arrive, repeat until the roots are complete.

`sd.build_sequential` / `sd.build_sequential_aio` run one task at a time
with no executor — for tests and debugging.

## Executors: where a task runs

The scheduling loop never runs a task itself. It hands the task to a
`TaskExecutorABC`, and the executor decides where and how:

- **`HybridConcurrentTaskExecutor`** (the default) runs each task in one of
  four local modes, chosen per task: `ASYNC_MAIN_LOOP` (async-native tasks
  on the event loop), `SYNC_THREAD` (the default for sync tasks),
  `SYNC_PROCESS` (CPU-bound work in a process pool), `SYNC_BLOCKING`
  (inline, for debugging).
- **A remote executor** submits the task to other infrastructure.
  `ModalTaskExecutor` is the first-class one; the seam is public, so you
  can implement your own.
- **`RoutedTaskExecutor`** mixes executors — GPU tasks to Modal, the rest
  locally, say.

Two shapes of executor exist, and the difference matters for everything
below:

|                            | attached                             | detached                                                                                            |
| -------------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------- |
| the executor…              | runs the task and returns its result | starts the task and returns a **handle** (a backend reference)                                      |
| if the build process dies… | the task's fate is unknown to anyone | the task keeps running; the handle is recorded in the registry, and a later build re-attaches to it |
| who reports the outcome    | the build process                    | the **worker itself**, from inside the execution                                                    |

Local executors are attached. The Modal executor is detached, and that is
what the [Modal page](modal-orchestration.md) is about.

### Build-local concurrency limits

`ConcurrencyConfig` caps how much a single build submits at once: an
overall limit plus named limits mapped to tasks by a `key_selector`,
enforced with asyncio semaphores around executor submission. They are
scoped to the one build process; limits that hold _across_ builds are a
registry feature, below.

## The registry as a ledger over the entities of a build

Without a registry, a build is a process with a graph in memory. With one,
the registry is the ledger: it records **task**, **task instance**,
**plan**, **deployment**, **settings** and **execution** — the entities a
build is made of — and that record is what lets separate builds, and
builds with no resident process, coordinate.

- **`task`** is the completion and its global state: one row per task id,
  holding status, the live claim (if any) and the current execution
  pointer. It carries no parameters — just the promise and where it
  stands.
- **`task_instance`** is a construction of a task under a **scope** — a
  `(deployment, settings)` pair — storing the full instance body. See
  [Parameters](parameters.md) for `task.id` vs `task.instance_hash`.
- **`plan`** is one build's request under one scope: the set of task
  instances it needs, and the edges between them, as that scope's code
  discovered them.
- **`deployment`** and **`settings`** are the two halves of the scope; see
  [Orchestration on Modal](modal-orchestration.md#deployments-and-code-versions).
- **`execution`** is the ledger of attempts: one row per claim granted,
  recording who held it, when it started and ended, and how.

Three facts about the ledger shape everything else:

- **Task state is global to the environment, not per build.** A task id is
  a deterministic hash of its significant parameters, so the same task in
  two builds is the same `task` row, with one status. That is what makes
  "don't re-run what another build completed" work, and equally what lets
  one build's in-flight task hold another build's downstream tasks back.
- **Statuses are derived from an append-only event log**, denormalised
  onto the task row for fast reads. `COMPLETED` is sticky in the sense
  that nothing revokes it by fiat — the one way out is described in
  [Invalidation](#invalidation-the-registry-follows-the-world) below.
- **Registry writes are best-effort.** A registry hiccup never fails a
  task whose work succeeded; a lost completion heals from target
  existence on the next look.

### Builds are requests; the claim is the only coordination

> A build is a **request for a set of root tasks to be materialised**, not
> an owner of the tasks that materialise them.

Which build runs a task cannot change its result — tasks are
content-addressed — so the only thing that must not happen is two builds
running the same task at once. The **execution claim** prevents exactly
that, and nothing else does.

- The claim is the task's `RUNNING` status plus a finite expiry, taken
  atomically in the start transaction — nothing is live forever. At most
  one claimant wins; the loser learns the winner's executor reference and
  re-attaches, or waits, or — if the claim has **lapsed** — takes it over.
  A lapsed claim is not a distinct state: `RUNNING` with an expiry in the
  past is simply not a claim anymore, so an abandoned execution heals
  without a reaper or an operator, and the takeover is recorded on the
  ledger (`execution.claim_outcome = taken_over`).
- Every start carries a claim TTL. Where the executor knows how long an
  execution may run (Modal's worker `timeout`), the TTL is derived from it
  plus a grace margin, so a live execution's claim cannot be taken while
  the backend would still let it run.
- **A build's plan is closed under dependencies, within its scope.**
  Discovery registers every incomplete dependency of the roots, and the
  registry admits into the plan every incomplete upstream a recorded edge
  in the plan's scope points at — at registration, and again on every read
  of the plan's frontier — so a build is never gated by a task it could
  not run itself.
- **A build acts on everything in its plan**, whichever build last touched
  it — a shared task another build _cancelled_ is actionable again and
  runs; a shared task another build _failed_ is a result, and the build's
  fail mode decides.
- **Authority to revoke is build-scoped.** Cancelling build B releases
  only the claims B's own plans hold, never build C's.
- **Duplicate upstream work across scopes is accepted, not prevented.**
  Two builds in different scopes may discover different upstream sets for
  one shared completion; whichever claims it first runs its own
  instance's body, and the other waits on the task's global status. The
  design does not try to prevent this — see [Shared
  tasks](#shared-tasks-across-builds) below.

With a registry every execution claims; there is no switch to turn it off.
`build(..., claim_config=ClaimConfig(...))` tunes how a claim is waited on
and renewed. Without a registry (`NoOpRegistry`) there is nothing to
arbitrate against and every claim is granted.

Design record: [`docs/design/registry-v2/design.md`](https://github.com/stardag-dev/stardag/blob/main/docs/design/registry-v2/design.md).

### Cancelling work: the worker asks, nothing reaches in

Cancelling a build marks it and releases the claims its tasks hold, and
stops there. **Nothing reaches into a running container to kill it.** The
containers find out by asking.

A worker knows its own execution's identity — minted when its task was
claimed, before the container existed — and at its checkpoints it asks the
registry one question: _is this execution still the one the task is
waiting for?_ Told no, it stops **cleanly**: no output written, no
completion reported.

Two checkpoints are automatic and cost you nothing:

- **The start of each attempt**, before `run()`. This catches a cancel
  that landed while the container was still queued, which on a wide
  fan-out is most of them.
- **Each dynamic-dependency yield**, where the task is about to register
  children and suspend. A build that has stopped does not pay for another
  layer of the DAG.

For a long `run()` body, ask where _you_ know stopping is safe:

```python
import stardag as sd


class TrainModel(sd.TargetTask[sd.DirectoryTarget]):
    def run(self):
        directory = self.target()
        for epoch in range(self.epochs):
            if sd.cancellation_requested():
                raise sd.ExecutionCancelled()
            train_one_epoch(directory)
        directory.mark_done()
```

`cancellation_requested()` is throttled (30s by default,
`STARDAG_CANCELLATION_CHECK_INTERVAL_SECONDS`), so it is cheap to call in a
loop. It answers `False` outside a worker.

Three things are worth knowing about the shape of this.

**It never stops a healthy worker.** A `False` is also what you get from an
unreachable registry, a transport failure, or a registry that does not
implement the question. Stopping needs _positive evidence_ that the
execution is no longer wanted, because stopping wrongly destroys work
while running on wrongly writes a content-addressed output nobody reads.

**Raise rather than return.** An early `return` writes no output, which
looks like a clean stop and is not one: the worker cannot tell it from a
task that finished, so it reports a completion — for a target that does not
exist. `ExecutionCancelled` is the only thing recognised, and the only
thing that records nothing.

**Side-effecting tasks are not covered, and never were.** A task that
writes to somebody else's database or sends an email has already done so
by the time it reaches a checkpoint. Cancellation ends the _execution_; it
cannot undo what the execution did outside its target.

When you need a container gone _now_ rather than at its next checkpoint,
that is a human decision. `stardag builds cancel <build-id>` releases the
build's claims immediately (see [Builds are requests](#builds-are-requests-the-claim-is-the-only-coordination)
above); it does not reach into a running container, so a worker still
mid-execution notices at its own next checkpoint, or runs to completion if
it has none.

### The plan: roots, discovery, closure

_In practice: [Evolve a DAG Safely](../how-to/evolve-dags.md)._

A task id promises the world state its completion establishes. It does
**not** promise the set of upstream tasks it was built from: a downstream
asks for its upstream's output, not for how the upstream got there. So the
registry keeps a task's dependency edges — static `requires()` and yielded
dynamic ones — on the **instance**, under its scope, rather than on the
task id.

A build's **plan** is a request under one scope: a lookup-or-create by
`(build, deployment, settings)`. Its lifecycle:

- **Roots first, unexpanded.** The plan is created holding its root
  instances, admitted before anything is discovered — which is what makes
  a crash mid-registration recoverable: any driver that picks the plan
  back up finds the roots as **discovery jobs** and finishes the static
  phase.
- **Chunks.** Discovery registers the DAG in chunks (bounded batches, each
  self-consistent: an instance lands together with its declared edges).
  The frontier may act on an unsealed plan; only build completion requires
  the plan to be sealed.
- **Seal.** `/seal` verifies every root is expanded or complete, every
  edge's upstream is a member (closure holds), and — for a Modal
  deployment — that the plan's deployment is still the app's current one.
  A plan that passes becomes `sealed_at`; if it is a replacement for an
  existing request, it also **activates** here, and the plan it replaces
  becomes `superseded_at` in the same transaction.
- **Active / superseded.** Exactly one plan per build is active at a time.
  A build's active plan is what the frontier, the claim and completion all
  read; a superseded plan still finishes what is running under it (see
  [rollover](modal-orchestration.md#deployments-and-code-versions)), but
  nothing new starts there.

Because edges belong to the **instance**, which sibling plans in the same
scope share, another plan's yield can add an edge from one of your plan's
members to an instance you do not hold yet. Every read of a plan's
frontier therefore runs a **closure step** first: admit, from the plan's
own members, every upstream instance reachable over recorded edges that
is not yet a member. This is also what lets one scope's discoveries — a
fan-out's expensive pre-yield section, in particular — be shared: two
plans under the same scope trust each other's edges rather than
re-discovering them.

Within a scope, edges only grow and nothing retracts them, so gating can
only over-approximate — never run a task before an upstream its code
reads is complete. A build has one scope (one deployment, one settings)
per plan; re-triggering it with a root that would construct differently
under the same scope is refused: **start a new build.**

Design record:
[`docs/design/registry-v2/design.md`](https://github.com/stardag-dev/stardag/blob/main/docs/design/registry-v2/design.md).

### Shared tasks across builds

Because task state is environment-global, a build routinely finds tasks in
its plan whose status another build produced. The task's status alone
decides what the build does with it, once it is gated open — every upstream
in the plan complete:

| the task is…                       | the build…                                                        |
| ---------------------------------- | ----------------------------------------------------------------- |
| `PENDING`                          | runs it                                                           |
| `RUNNING` under a **live** claim   | waits — the task's completion wakes it                            |
| `RUNNING` under a **lapsed** claim | takes it over                                                     |
| `SUSPENDED`                        | runs it — a resume from scratch, idempotent by contract           |
| `INTERRUPTED`                      | starts it again, within its own budget                            |
| `CANCELLED`                        | runs it, within the attempt budget — a revocation is not a result |
| `SKIPPED`                          | runs it, within the budget — a skip whose reason is gone          |
| `FAILED`                           | leaves it; the build's fail mode owns results                     |

Nothing in that table asks whether another build is alive. A gate can no
longer point outside a build's plan, so a build with nothing to run and
nothing running is genuinely finished or genuinely failed.
`stardag builds frontier <build-id>` shows the frontier directly.

Two builds in different scopes may see different upstream sets for one
shared completion — accepted, not prevented (see [Builds are
requests](#builds-are-requests-the-claim-is-the-only-coordination)
above). Two instances of one completion in one scope, across two plans, do
**not** share dynamic edges, so a suspending task's expensive pre-yield
section can run once per distinct instance rather than once globally —
the accepted cost of a second construction.

### Exclusion: giving up on a member

An operator can give up on a plan member without failing the whole build:
`excluded_at` marks it excluded, which removes it from scheduling and from
what gates the build's completion. Exclusion cascades to the member's
downstream closure within the plan — otherwise a downstream of an excluded
member would be neither runnable nor excluded, a limbo the design does not
allow — and **an excluded root fails the build**, since the request it
represents can no longer be met. A discovery job whose `requires()` raises,
or whose class the tick cannot import, is excluded automatically
(`discovery_failed`) rather than failing the global task: the failure is a
property of this plan's code, not of the promise, and an excluded member
is never retried as a discovery job again.

### Invalidation: the registry follows the world

The only way out of `COMPLETED` is discovery observing that a task's
target no longer exists: the driver checks the target as part of ordinary
discovery, and a negative observation on a task the registry believes
`COMPLETED` withdraws that completion (refused while a live claim holds
the task). There is **no operator route that declares a task incomplete**
by fiat — an operator who wants a re-run acts on the target (deletes it),
then triggers a build, which observes the deletion and invalidates.
`stardag tasks check <task_id>` runs `complete()` locally and reports what
it saw, as a convenience.

This is deliberately narrow: it serves "the target is gone, the promise is
unchanged" (a retention policy, a cleared directory, a corrupt partial
output removed), where re-running reproduces, by the task-id contract, the
same output. **"I want to fix its output" is not a use of invalidation** —
that is a new promise, and needs a new task id (see
[Parameters](parameters.md#the-task-id)).

### Builds stop over executions

Cancelling a build (above) releases claims and reaches no container.
Ending the containers themselves is a separate, human decision over the
build's **executions** — the ledger of attempts, one row per claim
granted — because a claim can move on while a container someone forgot
about keeps running. An execution with no report of having ended yet
(`ended_at IS NULL`) is exactly the list `stardag builds stop` acts on: it
stops each one's container and reports it before the build itself is
cancelled, so nothing is left holding a claim that outlives its own
container — see [Stopping a build's
executions](../configuration/cli.md#stopping-a-builds-executions).
Deleting a build is refused while any of its executions is still
unended, or any of its plans holds a live claim, for the same reason: the
ledger must never be cascaded away under a worker that may still report.

### Concurrency limits across builds

Named limits configured per environment in the registry hold **across all
builds** — processes, machines and scheduling modes. A task occupies a
slot by being `RUNNING` under a live claim with the key recorded, computed
from the instance body at claim time; the slot frees on any transition out
of `RUNNING`, with no leases to renew. Enforcement is atomic with the
claim, so a denied task never occupies a worker, and the refusal itself
records which keys were asked for, so a task that has never held a claim
is still findable as queued on them when a slot frees.

A resident build enforces them the same way a reactive one does: pass
`limit_key_selector` to `sd.build(...)` (or `TickConfig.limit_key_selector`
for a reactive app), mapping a task to the key(s) it competes on. Note
that a resident build killed while holding a slot leaves its task
`RUNNING` until the claim lapses; reactive scheduling on Modal heals this
itself, which is one reason to prefer it for unattended limited runs.

Infrastructure-level limits (Modal's per-function `max_containers`, say)
apply independently underneath.

## Where this leaves off

Everything above is executor-agnostic. The one thing it cannot give you is
a build that survives its own process: an attached executor's task dies
with the build, and a resident scheduling loop has to stay alive for as
long as the longest task. Stardag's answer is a **detached** executor with
**self-reporting workers**, and on top of it a scheduler made of
short-lived ticks with no resident process at all. Modal is where that is
implemented and the recommended way to run Stardag at scale — continue
with [Orchestration on Modal](modal-orchestration.md), or the
[Modal how-to](../how-to/integrate-modal.md) to set it up.
