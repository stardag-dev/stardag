# Build & Execution

Understanding how Stardag executes DAGs.

## Execution Model

Stardag uses "Makefile-style" bottom-up execution:

1. Start at the requested task
2. Check if it's complete (output exists)
3. If not, recursively ensure dependencies are complete
4. Execute incomplete tasks in dependency order
5. Persist outputs

Example:

```
Requested: Task C (depends on B, which depends on A)

Step 1: Is C complete? No
Step 2: Is B complete? No
Step 3: Is A complete? No
Step 4: Execute A, save output
Step 5: Execute B, save output
Step 6: Execute C, save output

```

Two properties follow from this model and hold across every execution mode
described below:

- **Completeness is target existence.** A task is done when its output
  exists in storage — not when some scheduler says so. Storage is the
  ground truth, which is what makes resumption, retries and de-duplication
  safe: re-running a build never re-executes work whose outputs exist.
- **Idempotent re-execution.** Tasks (including those with dynamic
  dependencies) are written so that re-running them from scratch is safe.
  The engine relies on this whenever execution crosses a process or
  machine boundary.

## The Build Functions

The primary way to execute tasks is `sd.build` or `await sd.build_aio`.

Mostly for testing and debugging, there is also `sd.build_sequential` and
`sd.build_sequential_aio`.

## Local Concurrency

`sd.build` runs an asyncio scheduling loop: it discovers the DAG, then
repeatedly submits every _ready_ task (all dependencies complete) to a
**task executor** and processes results as they arrive. The default
`HybridConcurrentTaskExecutor` runs each task in one of four modes,
selected per task (async-native tasks on the event loop; sync tasks in a
thread pool by default, optionally a process pool):

- `ASYNC_MAIN_LOOP` — `await task.run_aio()` on the main event loop
- `SYNC_THREAD` — `task.run()` in a thread pool (default for sync tasks)
- `SYNC_PROCESS` — `task.run()` in a process pool (CPU-bound work)
- `SYNC_BLOCKING` — inline (debugging)

Build-local concurrency limits — an overall cap and named limits mapped to
tasks via a `key_selector` — are configured with `ConcurrencyConfig` and
enforced by gating executor submission. These are scoped to the one build
process; see [Concurrency Limits](#concurrency-limits) below for limits
that hold across builds.

## Transfer of Execution (Remote Executors)

The scheduling loop is decoupled from _where_ tasks run through the
`TaskExecutorABC` seam. A remote executor — `ModalTaskExecutor` is the
first-class implementation — submits each task to remote infrastructure
instead of a local pool, and a `RoutedTaskExecutor` can mix executors
(e.g. GPU tasks to Modal, the rest locally). See
[Integrate with Modal](../how-to/integrate-modal.md) for the packaged
setup.

### Detached execution

Remote executions are **detached** by default: the worker invocation
survives the process that spawned it. The execution's backend reference
(e.g. a Modal function call id) is recorded in the registry together with
the task's started event, which buys three things:

- **Re-attach instead of re-execute.** A resumed build (or a concurrent
  build wanting the same task) finds the task RUNNING with a live ref and
  attaches to that execution rather than starting a duplicate. An
  orchestrator restart no longer restarts your long-running tasks.
- **Real cancellation.** Fail-fast and user cancellation cancel the
  tracked remote executions — workers of a failed or cancelled build
  don't keep running. (A _crashed_ orchestrator's workers deliberately
  keep running — that's the re-attach premise above.)
- **Liveness probing.** Schedulers can ask the backend whether a recorded
  execution is still running, finished, or gone — no lease/heartbeat
  machinery needed.

### Worker-side lifecycle reporting

Workers report their own lifecycle to the registry — started (with their
own execution ref), completed (plus artifacts), suspended (dynamic
dependencies), failed — whenever the build id is forwarded to them and the
container has registry credentials. Reporting is best-effort by design: a
registry hiccup never fails a task whose actual work succeeded, and a lost
completion event self-heals from target existence. The consequence: a
task's registry state stays accurate **independent of any orchestrator's
lifetime**.

## Reactive Scheduling (No Resident Orchestrator)

With detached executions and self-reporting workers, the build process
itself is optional. In _reactive scheduling_ the build is driven by
short-lived, idempotent scheduler **ticks** instead of a resident process:

```
tick(build_id):
  acquire the build's scheduler lease (single-flight; held → exit)
  loop:
    clear the build's wake-up flag
    frontier = registry state: tasks whose upstreams are all complete
    act (bounded-concurrent, up to a per-tick spawn cap):
         spawn pending/suspended tasks detached (recording refs)
         probe running refs — leave live ones; self-heal completions
         (target existence is ground truth); record failures, and
         respawn the retryable ones within the attempt budget
    handle terminal states (all roots complete / failure — with blocked
    tasks marked skipped / cancelled; wait rather than fail when the
    only thing missing is a task another build is executing)
    linger briefly on the wake-up flag; exit when quiet
  on the way out: re-read the flag once before releasing the lease
    (set → keep the lease and re-act) and once after (set → spawn a
    successor tick)
```

Ticks are triggered by the build's bootstrap (below), by workers finishing
tasks, and by an optional periodic watchdog that also picks up
externally-cancelled builds and silently-lost workers. While a DAG churns,
one lingering tick behaves like a tight scheduling loop; when only
long-running tasks remain in flight, **nothing runs but your tasks**.

### Wake-ups: one tick, not one per completion

A worker's wake-up is two steps — set the build's flag, then make sure
somebody looks at it. The registry answers the flag-set by saying whether a
scheduler currently holds the build's lease, and a worker that hears "yes"
**skips the spawn**: the tick already running will see the flag on its next
poll. On a build of short tasks that is the difference between one working
tick and one container start per completion, each of which would arrive
after the resident scheduler had already done the work.

Skipping is only safe because the scheduler cannot exit past a flag it has
not seen — which is what the two re-reads on the way out are for. A wake-up
that lands while a tick is shutting down either finds the lease already
released, and spawns its own tick, or finds it held, in which case that
tick has not yet done its post-release re-read and will hand off to a
successor. Both may happen; one wins the lease and the other no-ops.

Nothing here depends on the registry supporting it. An older registry does
not report the lease state, so every wake-up spawns a tick exactly as
before — more containers, same correctness.

### Bootstrap: the one thing that happens before the first tick

A reactive build starts with a **bootstrap** — a single invocation that
walks the DAG, registers every task and its edges, persists the task
objects, and only then arms the build and spawns the first tick:

```
trigger (cheap, no target I/O):
  mint or resume the build in the registry
  register the root tasks
  spawn bootstrap(build_id, roots-by-value, tick_kwargs)

bootstrap:
  discover: walk requires(), one completion check per task,
            stopping at already-complete subtrees
  register the discovered DAG (chunked, post-order)
  persist the task objects (skipping pickles the tick can rebuild)
  write the reactive marker  <-- LAST, and the ordering is load-bearing
  spawn the first tick
```

**Why the marker is written last.** `reactive_app_name` is what marks a
build as reactively scheduled, and a tick no-ops on any build without it.
Registration is chunked and post-order, so the roots land _last_ — which
means that mid-registration the build presents as "nothing actionable,
roots not complete", exactly the shape terminal detection fails a build
on. Writing the marker only after discovery _and_ persistence have
finished is what guarantees no tick ever observes a partially-registered
DAG.

**Where it runs, and why that matters.** Discovery is target-root I/O: one
existence check per task. On a Modal deployment the bootstrap therefore
runs _inside Modal_, next to the target root, where a `modalvol://` volume
is a mounted filesystem rather than a rate-limited API — the same walk
from a laptop spends most of its wall time in backoff. Triggering is then
just "mint the build, register the roots, spawn", which needs no target
access at all. The task-store writes move with it for the same reason.
Anything that fails before the first tick records a terminal
`BUILD_FAILED`, on whichever side of the spawn it happened, so a build is
never left `RUNNING` with nothing driving it.

The registry is the scheduler state (the frontier is computed from
recorded task statuses and dependency edges, and it also carries the
reactive marker/owner/tick config). Task _objects_ are rehydrated from a
per-build task store persisted by the bootstrap, with a pickle-free
fallback that reconstructs them from the registry's stored task data
(`stardag.task_from_registry_data`). The store holds task _objects_ only —
the orchestration metadata lives in the registry, so re-triggering works
even when the target root is immutable/append-only. The registry never
pushes or executes anything — only user-deployed code (which has the
DAG-defining code) spawns work.

Each reactive build is **owned by the app that triggered it** (the owning
app name is stored in the registry with the build's reactive metadata):
each app's watchdog sweeps only its own builds, and a tick that reaches
any other deployed app in the environment — typically a wake-up from a
worker still running under a previous owner — forwards the wake-up to the
owner's tick instead of driving the build with the wrong code. Ownership
moves only by an explicit re-trigger from the new app, which also updates
the tick config.

### Wide layers: what one tick commits to

Acting on a frontier is bounded-concurrent. Putting a single task on a
worker costs a task-store read, an execution-claim acquisition, an executor
spawn and a start recording the execution ref — so a layer thousands of
tasks wide is thousands of independent round-trips, and doing them one
after another in one short-lived container is a scaling wall with nothing
behind it. A tick runs `TickConfig.max_concurrent_actions` of them at a
time (default 50, the same bound the resident engine has always used for
its own discovery). Bounded, not unbounded: firing a whole layer at the
registry at once would only move the failure.

A tick also caps how much work **one pass** commits to, via
`TickConfig.max_spawns_per_tick`. Left unset, the cap is derived rather
than guessed: it is a duration budget — a fraction of how long the tick's
own container may live, spread over the in-flight bound — because the cap
exists to stop a tick starting more work than it can survive to finish.

Which duration gets read is a ladder, most specific first:

1. `max_spawns_per_tick`, if you set it. The override always wins.
2. `TickConfig.tick_timeout_seconds` — **this container's** wall-clock
   limit. The Modal integration fills this in automatically from the
   `timeout` its `tick` function was deployed with, so in practice this is
   the rung you are on.
3. The executor's `execution_timeout_seconds`, as a fallback proxy. It
   measures how long the spawned _executions_ may run, which is a different
   quantity — a 24-hour worker under a 5-minute tick would derive a cap the
   tick cannot possibly work through — so it is only used when nothing
   knows the tick's own limit, and the tick's log line says so.
4. A conservative default, when no wall clock is known anywhere.

Every tick logs its cap and which rung produced it once per tick, so a
truncating tick is diagnosable without guessing which number it read.

Hitting the cap is not a stall and is never silent: the tick logs the
truncation, and because the pass _acted_, it immediately re-evaluates on a
fresh frontier and takes the next batch — no wake-up, no linger, no
watchdog. The only case where it waits instead is the one where waiting is
correct: every task it attempted was denied a concurrency-limit slot, so
what the build needs is a slot to free up, not another round of denials.

Discovery — the DAG walk at trigger time, and the same walk every worker
runs when it registers dynamically yielded dependencies — is bounded-
concurrent on the same default. Its ordering guarantees are unaffected:
dependencies are still registered before the tasks that depend on them (the
bulk endpoint resolves `dependency_task_ids` against rows that must already
exist), and the walk's result is identical to the serial one for any DAG.
Only the completion checks overlap.

### Task retries: the failures no backend can retry for you

An execution backend's function-level retries (Modal's `retries=`, say)
cover exceptions raised _inside_ the container. That leaves a class of
failures they structurally cannot reach: a spawn that failed before any
container existed, an execution the backend killed (OOM, timeout), a spot
instance preempted mid-run, a worker that died after writing part of its
output. Under `FAIL_FAST`, one of those used to end the whole build.

A tick therefore keeps its own budget: `TickConfig.max_attempts` (default
**2**), a budget **per task per build round** on how many executions the
scheduler will start. A failure the tick records is reset to pending and
picked up on the next pass while the budget allows.

The split is deliberate and narrow:

| Failure                                                             | Retried?  |
| ------------------------------------------------------------------- | --------- |
| Spawn raised — no container ever ran                                | yes       |
| Backend reports the execution failed (OOM, kill)                    | yes       |
| Execution claim lapsed with nothing left to probe (worker vanished) | yes       |
| Task object missing and not rehydratable from the registry          | **no**    |
| Exception inside your task                                          | n/a       |
| Container reclaimed, or the function timeout hit                    | see below |

The `see below` row is not a failure at all: the platform ended the
attempt and the task did nothing wrong. Those are recorded as
[interruptions](#interrupted-the-platform-took-the-execution) and
budgeted separately, so that a task designed to be killed and resumed
does not spend a budget meant for genuine failures.

The `n/a` row is the important one: a task that raises reports its own
`TASK_FAILED` and leaves the frontier, so a tick never sees it. Genuinely
deterministic failures are therefore out of reach of this budget by
construction, not by a judgement call — which is why defaulting it above 1
cannot turn "fails fast on a bug" into "runs the bug three times". The
`no` row is the one deterministic failure a tick _does_ see, and it is
excluded explicitly: the task store and the imported task classes do not
change between two passes, so a retry re-reads the same absence.

Because a tick is short-lived and remembers nothing, the count comes from
the registry (`attempt_count` on the frontier, the per-build task listing
and task events).

#### Rounds: what resets the budget, and what does not

A **round** runs from the build's most recent `BUILD_RESUMED` event, or
from the build's beginning if it has never been resumed, and the count is
scoped to it. So the two things that look alike from the UI are not:

- **Re-triggering the build resets it.** `build_trigger(..., build_id=<this
build>, reactive=True)` records `BUILD_RESUMED` _before_ its discovery
  resets the failed tasks to pending, so the round boundary lands ahead of
  them and every task starts the new round at zero. This is the recovery
  path for a build that ran out of attempts, and both exhaustion messages
  point at it. Add `tick_kwargs={"max_attempts": N}` to the same re-trigger
  if the task needs more attempts per round.
- **A bare retry does not.** Clicking Retry in the UI, running `stardag
tasks retry`, or POSTing the retry route flips the task back to pending
  without recording `BUILD_RESUMED`, so the count it is measured against is
  unchanged. On a task that is already at budget the retry _succeeds_ and
  the scheduler then refuses to start it — which would look like nothing
  happening at all, so the tick says so explicitly and fails the task again
  with the re-trigger spelled out.

**Resuming a suspended task is never budget-gated.** A dynamic-dependency
yield records a fresh start, so a task that yields repeatedly accumulates
attempts while being perfectly healthy; gating resumption would cap dynamic
dependencies rather than retries. It does mean such a task reaches its
_retry_ budget sooner within a round — raise `max_attempts` for DAGs that
suspend a lot.

Set `max_attempts=1` for the previous behaviour (record the failure, never
respawn). Against a registry that does not report `attempt_count` no budget
can bound a retry loop, so retries are disabled and the tick logs that.

### INTERRUPTED: the platform took the execution

Some executions end without the task being wrong: the backend reclaims the
container, or the execution hits the function timeout. Recording those as
failures is wrong twice over — under `FAIL_FAST` it kills a build for
something the task did not do, and it spends a retry budget meant for
genuine failures on a task whose whole design may be to be killed and
resumed.

So they get their own status. `INTERRUPTED` is shaped exactly like
`SUSPENDED`: non-terminal, non-running, holds no execution claim, listed
as actionable by the frontier, and reset by a re-trigger. The difference
between them is only the reason the attempt ended.

**The worker reports it, in the grace window before it dies.** That is the
point. A preempted or timed-out worker that reports nothing leaves its
claim held and its concurrency-limit slots occupied until something
notices the execution is gone — and the only thing that can notice is a
scheduler tick, woken by a watchdog that is off by default. Reporting the
interruption releases the claim immediately and wakes the scheduler
directly, so recovery no longer depends on an opt-in safety net.

**Only a task that asks gets resumed.** The status is written for one
reason: the task caught the interruption, persisted its progress, and
raised `ResumableInterruption`. An interruption it did not catch never
becomes INTERRUPTED at all — the execution simply dies and a later
scheduler pass records an ordinary retryable failure, which is the right
answer for both things an uncaught interruption can mean (the task hung, or
its timeout is too small).

That is why there is no policy or per-task configuration here. The
distinction between "expected timeout" and "something went wrong" is made
by the task, in the only place that knows it.

Resumption is bounded by `TickConfig.max_interruptions` (default 20),
separate from `max_attempts` — a task designed to be killed and resumed
would otherwise exhaust a budget meant for genuine failures.

**A backend running its own retries is not raced.** Some backends restart
a timed-out input themselves, under the same execution ref. Before
respawning an interrupted task whose ref still probes as live, a tick
leaves it alone — otherwise the task would run twice.

For the Modal specifics — which exception arrives when, how much grace
there is, and the one-keyword footgun in writing the `except` block — see
[Preemption and timeouts](../how-to/integrate-modal.md#preemption-and-timeouts).

### Cross-build blocking

Task state is **per environment**, not per build: a task id has one status,
and its dependency edges are shared. Two builds over overlapping DAGs
therefore see each other — which is what makes "don't re-run what another
build already completed" work, and equally means an upstream some _other_
build is executing holds this build's downstream tasks back.

A build can consequently have nothing runnable and nothing running of its
own and still be perfectly healthy. Terminal detection distinguishes the
two cases from the frontier's list of blocking upstreams this build does
not own:

- **A RUNNING blocker whose execution claim is still live** — the build
  waits, exactly as it waits for a busy concurrency-limit slot. The
  blocker's completion wakes this scheduler, and the watchdog covers a lost
  wake-up.
- **A RUNNING blocker whose execution claim has lapsed** — the build fails.
  Not "presumed abandoned": the claim's expiry has passed, so the registry
  no longer honours it, has stopped counting it against concurrency limits,
  and will hand the task to the next claimant.
- **A blocker another build has yet to schedule** (pending or suspended,
  under a build that is itself still live) — the build waits too. That
  build is going to run it; failing here would just trade one spurious
  failure for another.
- **A non-RUNNING blocker no live build is going to run** — its owning
  build has gone terminal, no build owns its status at all, or that status
  could not be resolved. Nothing is going to move it, so the build fails
  immediately, naming the blocking task, its status, how long it has been
  in it, the build that owns it, and why that owner will not move it.

The two questions are answered from different evidence, on purpose. A
RUNNING task holds an **execution claim**, and every start records that
claim with an expiry (see [Claim expiry](#claim-expiry)) — so "is anyone
still executing this?" is something a build in another scheduler can simply
read, without probing an executor it has no access to. Every other status
holds no claim and therefore carries no expiry, so the only available
evidence is whether the build that owns the blocker's status is itself
still live; that lookup happens only when a build actually looks stalled,
and once per owning build, so a healthy build never pays for it.

A RUNNING blocker whose claim carries **no** expiry — an older registry, or
a start recorded before expiry existed — is waited on indefinitely, and the
tick logs that the wait cannot be shown to end. That is deliberate: a
missing expiry means "never lapses", not "dead", and failing on it would
reintroduce exactly the spurious failures this path exists to remove.
Cancel the blocking task to break such a wait.

Note what proving a blocker dead does **not** buy you: if the blocker is not
in your build's task set, your build can never run it however dead it is, so
the build still fails. What changes is the certainty and the message — it
names the claim that lapsed instead of quoting a timeout.

#### Claim expiry

Every start a reactive tick records carries a claim TTL derived from the
**executor's own timeout** (for Modal, the worker function's `timeout` from
its `FunctionSettings`) plus a grace margin, so the claim outlives the
execution it guards by a small margin and no more. Where no timeout is
known the registry's own default applies.

The derivation matters. Granting an expiry on every start is what makes an
abandoned claim heal at all, but it also means a task that outlives its TTL
could have its claim taken while it is still alive — a duplicate execution.
Tying the TTL to the limit the backend itself enforces is what keeps that
from being a real risk: the backend will have killed the execution before
its claim lapses.

**Seeing it.** `stardag builds frontier <build-id>` renders the blocking
upstreams: which task of your build is held up, by which task (namespace and
name), in what status, for how long, and which build owns it — plus which of
the two remedies below applies. `stardag tasks list --status running` asks the
same question claim-first: which tasks in this environment are holding an
execution claim, and since when. See the
[CLI reference](../configuration/cli.md#reading-the-frontier).

**Recovering a build blocked on a task from another build.** Retry the
blocking task (`stardag tasks retry <owning-build-id> <task-id>`, or `POST
/api/v1/builds/{build_id}/tasks/{task_id}/retry`, using the owning build's id)
to reset it to PENDING, then re-trigger your build — a re-trigger resets
blocking tasks in any retryable status for you.
Retry covers **suspended** as well as failed/cancelled/skipped: a task left
SUSPENDED (its execution registered dynamic dependencies, yielded and
returned, and then its build was abandoned) used to have no supported way
back, and needed an undocumented cancel-then-retry dance. A task stuck
RUNNING is the one exception — it holds a live execution claim, so
**cancel** it (`stardag tasks cancel <owning-build-id> <task-id>`) to release
the claim; retry deliberately does not, since that would risk a second
concurrent execution.

If the owning build is abandoned altogether — its orchestrator died without
emitting a terminal event, so it is `RUNNING` forever and holding every claim
its tasks had — `stardag builds cleanup` is the bulk recovery; see
[Cleaning up abandoned builds](../configuration/cli.md#cleaning-up-abandoned-builds).

Reactive scheduling is experimental and currently Modal-first — see
[Integrate with Modal](../how-to/integrate-modal.md#reactive-scheduling-no-resident-build-function-experimental)
for usage, requirements and limitations.

## Exactly-Once Execution (Execution Claims)

Within one build, the engine guarantees each task executes at most once.
Across builds, restarts and retries, the **execution claim** extends that
guarantee — **by default** wherever a registry is configured and the
execution is probeable (detached remote executions, in both resident and
reactive scheduling):

- The claim is an atomic check inside the task's start transaction (the
  registry denies a start racing an already-RUNNING task and echoes the
  running execution's ref), so it costs no extra roundtrips and at most
  one concurrent claimant can win.
- A losing claimant resolves with the machinery described above: it
  **re-attaches** to the winner's live execution, self-heals a completion
  the winner already produced (target existence is ground truth, with
  eventual-consistency retries), records a provably dead winner and
  re-claims, or — when the winner exposes no probeable ref — waits for
  external completion with backoff.
- Control it with `build(..., claim=...)`: `None` (default) claims
  probeable executions; `True` forces claiming (losers without a ref
  wait); `False` disables. Reactive scheduler ticks always claim.
- Custom arbitration backends implement
  `RegistryABC.task_start_claim_aio` — keeping claim, status and
  completion consistent in one backend. There is no default
  implementation: without a registry (`NoOpRegistry`) there is no shared
  state to arbitrate against and every claim is granted; any other
  backend must arbitrate for real.
- The claim is taken **before** the build-local concurrency-limiter slot
  (the registry-backed limiter counts RUNNING tasks, so claiming inside
  the slot would deny itself). Consequence: a claimed task can appear
  RUNNING (without an executor ref yet) while still queued behind a
  local limit.

### Global Concurrency Lock (deprecated)

The optional lease-based **global concurrency lock** (`GlobalLockConfig`)
predates execution claims and is deprecated in their favor. It remains
available for the one case claims don't cover yet: executions **without
probeable liveness** (e.g. local executors shared across machines), where
its TTL lease is what recovers from a crashed holder. When enabled, the
engine now renews held locks in the background so long-running tasks no
longer outlive the lease.

## Concurrency Limits

Two mechanisms, by scope:

- **Build-local** (`ConcurrencyConfig`): asyncio-semaphore limits inside
  one build process — an overall cap plus named limits via a
  `key_selector`. Uniform across local and remote executors. Resident
  builds can instead enforce the registry-backed limits below by passing
  `concurrency_limiter=RegistryConcurrencyLimiter(key_selector=...)`.
- **Registry-backed named limits**: caps configured
  per environment in the registry
  (`PUT /api/v1/concurrency-limits/{key}`), enforced atomically when a
  task starts — **across all builds** in the environment. A task occupies
  a slot simply by being RUNNING with the key recorded; the slot frees on
  any terminal status (no leases). In reactive scheduling denied tasks
  stay pending and proceed when a slot frees, and status liveness — hence
  slot honesty — is maintained by worker reporting and tick self-healing.
  In resident builds the `RegistryConcurrencyLimiter` blocks-and-retries
  the submission, but **no automatic healer exists for resident mode**: a
  resident build killed after acquiring leaves its task RUNNING, holding
  the slot until the task or build is explicitly failed/cancelled via the
  API/UI. Prefer reactive scheduling for unattended limited runs. Both
  modes share the same slots — limits hold across processes, machines
  and scheduling modes.

Infrastructure-level limits (e.g. Modal's per-function
`concurrency_limit`) apply independently underneath either mechanism.
