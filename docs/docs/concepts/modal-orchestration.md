# Orchestration on Modal

How Stardag runs on Modal: what a deployed app contains, the two ways a
build can be driven, and — for the reactive mode — how a build with no
process of its own learns that something changed. For setup and recipes
see [Integrate with Modal](../how-to/integrate-modal.md); this page is the
model behind it, and assumes [Build & Execution](build-execution.md).

## What a deployed app is

`StardagApp.finalize()` registers a handful of Modal functions:

| function                                          | runs                                                                     |
| ------------------------------------------------- | ------------------------------------------------------------------------ |
| `worker_<name>` (one per `worker_settings` entry) | one task per input, self-reporting its lifecycle                         |
| `build`                                           | a **resident** build: the ordinary scheduling loop, in a Modal container |
| `bootstrap`, `tick`, `tick_watchdog`              | the **reactive** scheduler (below)                                       |

All of them make registry calls, so all of them carry the registry secret.
A task's worker and resources are chosen by the app's `worker_selector`,
its concurrency-limit keys by its `limit_key_selector`; both are
deployed-app configuration, identical for every build of the app.

## Detached execution and self-reporting workers

The Modal executor is **detached**: it spawns the worker and records the
Modal function-call id in the registry with the task's `TASK_STARTED`
event, rather than holding a blocking call open. The worker then reports
its own lifecycle — started (with its own call id), completed (plus
artifacts), suspended (dynamic dependencies), failed, interrupted — from
inside the container.

Three things follow, and every mode below rests on them:

- **Re-attach instead of re-execute.** A resumed build, or another build
  wanting the same task, finds it `RUNNING` with a live reference and
  attaches. Restarting an orchestrator does not restart your long tasks.
- **Real cancellation.** Fail-fast and a cancel from the UI cancel the
  tracked calls; workers of a dead build do not run to completion.
- **Liveness is the backend's answer.** A scheduler asks Modal whether a
  recorded call is running, finished or gone — no heartbeats.

A task's registry state is therefore accurate **independent of any
orchestrator's lifetime**, which is what makes the orchestrator optional.

## Two ways to drive a build

**Resident** — `build_trigger(tasks)` (or `build_spawn`): the `build`
function runs `sd.build` in one container for the whole build. Simple, and
the fastest for a DAG of many short tasks: the scheduling loop is a tight
in-memory loop. Its cost is that the container lives as long as the
longest task, and a three-day build with two long tasks pays for a
container that mostly waits.

**Reactive** — `build_trigger(tasks, reactive=True)`: no resident process.
The build is driven by short-lived, idempotent scheduler **ticks**, spawned
when something changes. While a DAG churns, one tick lingers and behaves
like a resident loop; when only long tasks remain in flight, **nothing runs
but your tasks**. Reactive scheduling is also what makes cross-build
coordination, retries of infrastructure failures and checkpoint/resume of
preempted tasks work unattended.

Both modes share the registry, the claims and the limits, so they mix: a
resident build on a laptop with Modal workers (a _hybrid_ run) and a
reactive build in the cloud coordinate through the same rows.

## Reactive scheduling

### A build's life

```
trigger (cheap, no target I/O):
  mint or resume the build; register the roots; spawn bootstrap

bootstrap (one container, once per trigger):
  discover the DAG next to the target root; register it, closed over
  dependencies; persist task objects; arm the build; spawn the first tick

tick (short-lived, single-flighted per build):
  acquire the build's scheduler lease (held → exit)
  loop:
    clear the build's wake-up flag; read the frontier
    act: spawn ready tasks detached (claim first), probe running ones,
         heal completions, record failures and retry within budget
    terminal? → complete / fail the build (cancelling live executions)
    acted? → re-read immediately; else linger on the wake-up flag
  on the way out: re-read the flag before and after releasing the lease
```

The bootstrap writes the reactive marker **last**, after the whole DAG is
registered, so no tick ever sees a half-registered build. Discovery runs
inside Modal because it is target I/O — a mounted volume there, a
rate-limited API from a laptop — so triggering needs registry credentials
only.

A tick's fan-out is bounded (`max_concurrent_actions`, default 50) and the
work one pass commits to is capped by a duration budget derived from the
tick function's own `timeout`, so a container never starts more than it
can live to finish. Truncation re-reads immediately; it is never a stall.

### Retries and interruptions

A tick retries the failures no backend can retry for you — a spawn that
never produced a container, an execution Modal killed or lost — up to
`TickConfig.max_attempts` (default 2) per task per **round** (a round
starts at each re-trigger). An exception _inside_ your task is reported by
the worker as `FAILED` and never reaches this budget; that is what Modal's
own `retries=` is for.

A task the platform took away — preempted, or past its `timeout` — that
caught the interruption, checkpointed and raised `ResumableInterruption`
is recorded `INTERRUPTED` and resumed, up to `max_interruptions` (default
20). An interruption the task did not catch is an ordinary failure: it had
no plan for one. Recipe and knobs: [Preemption and
timeouts](../how-to/integrate-modal.md#preemption-and-timeouts).

### Wake-ups: how a build with no process learns something changed

A reactive build progresses only while a tick runs for it, and a tick runs
only because something spawned one. The registry sees every write that
can change a build's frontier but has no executor and **never spawns** —
so a wake-up is two halves, done by two parties:

1. **The registry flags.** Every change to a task's status — by any
   worker, any tick, a resident build, an operator in the UI or CLI, the
   reaper — flags every _other_ live reactive build holding that task. A
   transition out of `RUNNING` also flags the builds queued on the
   concurrency-limit keys the task held. Cancelling a build flags the
   build itself. The flag is `needs_tick_at` on the build; setting it is
   part of the write's own transaction.
2. **The scheduler spawns.** A finishing worker sets its own build's flag
   and spawns a tick unless the registry says a scheduler already holds
   the build's lease (that tick will see the flag on its next poll).
   Every tick, at the end of each pass that acted and on every exit, asks
   the registry for the **wake candidates**: flagged builds with no live
   lease that were not handed out in the last ~2 minutes. It spawns one
   tick per candidate, on that build's own app. A resident build with
   Modal workers does the same after each result it processes.

The registry hands each build out **once per window** and records the
hand-out, so twenty ticks asking at once produce one tick per flagged
build, not twenty. There is no "which builds share a task with me" in the
question — the flag already encodes relevance — so every tick is a bounded
mini-watchdog for its environment, for free.

Skipping the spawn when a scheduler is live is safe only because a tick
cannot exit past a flag it has not seen: it re-reads the flag once before
releasing the lease (set → keep the lease, act again) and once after (set
→ spawn a successor). A wake-up that lands during the release either finds
the lease gone and spawns, or finds it held and is picked up by that
post-release read.

**What this guarantees.** Any recorded status change, and any freed
concurrency slot, reaches every build it concerns, carried by the next
scheduler pass anywhere on the deployment — seconds, while anything is
running. **What it does not:** a change made while _nothing_ on the
deployment is ticking by something without a Modal client (the UI, a
laptop-only build), and events that nobody writes at all — a worker that
died without reporting, whose claim expires with nothing to notice. Those
two are what the watchdog is for.

### The watchdog

`tick_watchdog` is deployed on every app. It lists the running reactive
builds the app owns, **spawns one `tick` for each, and returns** — so a
sweep takes seconds whether the app is running one build or fifty, and
each build gets a container of its own, with its full timeout and its
normal linger, rather than a share of the sweep's. A spawn for a build
that already has a tick still starts a container, but that tick finds the
scheduler lease held and exits without acting.

With `watchdog_period_minutes` set it runs on that period; without it, it
runs when you invoke it — from the Modal UI or `modal run` — which is the
one-click recovery for a stalled build.

The default is off, and that is usually right. A standing sweep polls the
registry whether or not anything is building, enough to keep a
scale-to-zero database awake. Turn it on when leaving a build stalled for
even a few minutes is unacceptable, and pick the period from how long that
is — it is the recovery time for the two cases above, nothing else.

### App ownership

Each reactive build is owned by the app that triggered it (recorded in the
registry). Only the owner's ticks drive it — a tick that reaches another
app forwards the wake-up to the owner rather than running the build with
the wrong code — and each app's watchdog sweeps only its own builds.
Re-triggering a build from another app moves ownership, and re-persists
the task objects under the new app's code.

## Choosing

| you have                                                       | use                                                                                              |
| -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| many short tasks, one machine or one container                 | resident (`sd.build`, or the `build` function)                                                   |
| long or preemptible tasks, unattended runs, cross-build limits | reactive                                                                                         |
| a laptop driver with GPU tasks in Modal                        | resident with `RoutedTaskExecutor` — a hybrid run; it wakes reactive neighbours like a tick does |

Everything on this page beyond the first section requires a registry.
