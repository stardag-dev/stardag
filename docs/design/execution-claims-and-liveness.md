# Execution claims and liveness

How stardag answers "is this task being executed right now?", why it is
answered the way it is, what that costs, and which criticisms of it are
wrong.

Written after [#208](https://github.com/stardag-dev/stardag/issues/208), where
several bugs in reactive scheduling turned out to share one root, and where
the same three things were misread more than once.

## The constraints

Four facts about stardag force the shape of everything below.

1. **Tasks are content-addressed.** `task_id` is a deterministic hash of the
   task's parameters, so "the output for this task exists" is a property of
   the _environment_, not of any build. `Task` is unique on
   `(environment_id, task_id)`, and completion is legitimately global.
2. **Overlapping builds are the normal case.** In a shared environment,
   different builds routinely reference the same task. Two builds must not
   both execute it.
3. **Exactly-once therefore needs global arbitration**, and it has to be
   server-side: the participants are separate processes, often separate
   machines.
4. **Reactive scheduling has no resident scheduler.** Ticks are short-lived
   containers that read the frontier, act, and exit. Nothing on the
   scheduling side stays alive for the duration of a task.

## The design: the claim is a status, not a lease

`Task.latest_status == RUNNING` _is_ the execution claim. It is arbitrated in
`_create_task_event` (`routes/builds.py`) under `SELECT … FOR UPDATE` on the
task row, in the same transaction as the event and as any concurrency-limit
slot rows.

### Why — the advantages are load-bearing, not incidental

- **One row, one transaction, no drift.** Claim, status, completion and
  concurrency-slot occupancy are the same row, written together. They
  _cannot_ disagree. A separate lock table would immediately create a
  reconciliation problem: locks whose task has completed, tasks completed
  under a live lock, orphaned locks after deletes.
- **Sticky `COMPLETED` composes for free.** Because completion and claim
  share a column, "completed beats running" is one comparison in
  `apply_event_to_task`, not a cross-entity rule.
- **Every reader is one indexed column.** The frontier query, the
  concurrency-limit count, the UI and the task explorer all read
  `latest_status` directly. A lease table adds a join to each — including a
  frontier that is re-read on every linger poll (~3 s per active build).
- **Zero liveness traffic.** This is the big one and it is easy to skip. A
  real lease needs heartbeats: a six-hour task at a 60 s TTL is ~360 extra
  registry writes, and a thousand concurrent tasks is ~6 writes/second
  sustained _purely to say "still alive"_ — against the same database that
  serves frontiers. For a system whose entire purpose is long-running batch
  work, "liveness costs nothing" is a real scaling property.
- **Fewer user-facing concepts.** Pending / running / completed / failed is
  the Makefile model users already have. A TTL leaks into the UI ("why does
  this say expired?") and into configuration.

This was a deliberate simplification when reactive builds were introduced,
and it removed several competing notions of "is this running". It should not
be undone lightly.

### What it costs

The claim records **no expiry**. A holder that vanishes — crashed worker,
killed container, orchestrator that died without emitting a terminal event —
leaves the claim held forever. Because `latest_status` is environment-global,
that denies the task to _every_ future build, indefinitely, and the
concurrency-limit slots it occupied leak with it. Cancelling the owning build
does not release it either: build cancellation writes a build-level event and
historically did not cascade to tasks.

## The asymmetry that actually matters

It is tempting to summarise the problem as "the claim has no TTL". That is
not quite it, and getting the framing right changes what the fix should be.

- **Within a build**, the claim is not evidence-free. The tick records
  `latest_executor` and `latest_executor_ref`, and can ask the execution
  backend directly whether that call is alive (`detached_status`). That is
  _better_ evidence than a heartbeat, because it is the backend's own truth
  rather than a proxy for it.
- **Across builds**, there is nothing to probe. Build X cannot probe build
  Y's execution; it may not even have that executor configured.

So the gap is narrower and more specific than "no TTL":

> The claim records no liveness evidence that a **third party** can evaluate.

Everything below exists to infer, from the outside, what a single stored
expiry would state directly:

| Mechanism                                                                   | Where                                    |
| --------------------------------------------------------------------------- | ---------------------------------------- |
| `TickConfig.stale_running_no_ref_seconds`                                   | `build/_reactive.py`                     |
| `ClaimConfig.stale_running_no_ref_seconds` (second copy, different default) | `build/_base.py`, `build/_concurrent.py` |
| `TickConfig.stale_external_blocker_seconds` + blocker classification        | `build/_reactive.py`                     |
| `blocked_by_external` + owning-build liveness lookup                        | `routes/builds.py`, `build/_reactive.py` |
| Stale-build reaper (task-claim half)                                        | `services/build_cleanup.py`              |
| `GET /tasks?status=running` ("who holds a claim?")                          | `routes/tasks.py`                        |

`build/_registry_limiter.py` contains a written confession of the same gap:
a slot leaked by a crashed build "has no automatic healer", while a
legitimately long-running limited task "can be force-failed by that build's
`stale_running_no_ref_seconds` heal". Two heuristics for one missing field,
pulling in opposite directions.

## Sketch of the minimal fix (not implemented)

Recorded so the next person does not reach for a lock table.

Add one nullable column — `latest_status_expires_at` — written **once** at
claim time. No heartbeats.

- The claim check becomes
  `RUNNING AND (expires_at IS NULL OR expires_at > now())`. An expired claim
  is simply re-claimable, so it self-heals: no reaper, no manual release, no
  new state a user has to understand.
- Any third party — another build, the frontier query, the limit counter —
  evaluates it with a comparison. No join, no probe, no inference.
- Where a reader _can_ probe, probing still wins and can push the expiry out.
- Workers already emit lifecycle events; those can extend the expiry
  opportunistically, giving renewal on traffic that already exists.
- `NULL` means "no expiry known" — today's behaviour — so it is backward
  compatible by construction.

Every advantage listed above survives: still one row, one transaction, one
indexed column, no join, no heartbeats, no new user-facing state.

**It would remove:** the cross-build blocker classification and
`stale_external_blocker_seconds`; the task-claim half of the reaper; the
"who holds a claim?" question in most cases.

**It would not remove:** executor probing (better evidence than any expiry),
build-level reaping (an abandoned _build_ is a separate problem), or the
two-phase start.

## Corrections — things that keep being misread

### 1. "It needs a lease" overstates the case

Three specifics that survive scrutiny poorly:

- **A lease does not replace probing.** Where an executor ref exists,
  probing the backend is strictly better evidence. You would keep it.
- **`stale_running_no_ref_seconds` guards a much narrower thing than it
  looks.** It only fires when there is _no_ executor ref — the window
  between claiming and recording the ref, i.e. a tick crashing within
  seconds of spawning. A 30-minute timeout guarding a seconds-long crash
  window is a rare-crash backstop, not core machinery.
- **Build-level reaping survives regardless.** A build with no terminal
  event is a different problem from a leaked task claim.

Estimates of the form "thousands of lines collapse if you add a lease" do
not survive contact with those three points.

### 2. Identity and type resolution are **not** welded together

A tempting-but-wrong claim: _"`task_id` hashes a payload that also carries
the type discriminators, so type resolution can never be improved without
repartitioning every task id in existence."_

Wrong in both halves.

- **stardag has separate serialization modes precisely for this.** Hash
  computation runs with `context={"mode": "hash"}`, distinct from an ordinary
  dump, and serializers branch on it — `HashableSet` does exactly this
  (`_core/hashable_set.py`, gating on `CONTEXT_MODE_KEY`). This is one of the
  load-bearing pieces of the stardag ↔ pydantic integration, designed for
  exactly this separation, not a workaround discovered after the fact.

  The practical consequence: **regular-mode serialization is freely
  extensible.** Adding a field for resolution, tooling or debugging costs
  nothing as long as it is excluded from hash mode. The hash payload is the
  constrained one, and it is constrained on purpose.

- **The discriminators being in the hash is a deliberate feature.**
  Namespace and task family are hashed so that identity collisions between
  different task types with identical parameters are controlled rather than
  accidental. That is the point of including them — not an oversight that
  happens to block other changes.

### 3. A `__module` hint in the payload is not the cheap alternative it looks like

The apparent one-line fix — embed the defining module beside the
discriminators and `importlib.import_module` on a registry miss — was
rejected for reasons that are not about hashing:

- **It does not survive refactoring.** A stored module path records where a
  class _was_ when the payload was written. Move the class and every
  historical payload points at the wrong place. `__namespace` / `__name` is a
  stable _logical_ identity that the registry maps to whichever class
  currently declares it — which is the property you want from something
  persisted indefinitely.
- **Importing a module name taken from registry data is an execution
  primitive.** It is the same concern that keeps `AliasTask` off the
  pickle-free path. Making it safe requires an allowlist — that is, a
  declaration — which is `task_modules` again, only implicit.
- **It is unbounded and invisible.** `task_modules` plus deploy-time
  expansion is explicit and auditable: you can see exactly which modules a
  deployment can resolve, and the redeploy requirement is visible at the
  moment you change it.

So the comparison "one key versus a large feature" is not real; the
alternative is a key _plus_ import-on-miss _plus_ an allowlist _plus_ a
refactoring hazard, and it is less explicit.

The fair version of the critique is narrower and worth keeping in view: the
_declaration_ (patterns, deploy-time expansion, container import) is the core
of `task_modules`, while the _elision_ half (trigger-time pre-flight,
conditional pickle writing, `require_pickle_free`) is an optimization on top
and should be justified on its own terms.

Related: elision follows only from an **explicit** `task_modules`
declaration, never from the inferred default. The trigger reads the local app
definition while the tick runs the deployed one, so if inference alone
elided, upgrading the SDK would start dropping pickles that an app deployed
by an older SDK has no baked-in module list to compensate for.

### 4. `last_active_at` and `last_activity_at` are different things

- `last_active_at` is a column on `builds`, bumped by build-level
  **lifecycle** transitions only (resume, complete, fail, cancel, exit-early,
  roots appended). Task events deliberately do not touch it, so the per-task
  hot path does not contend on the build row. **It is not an activity
  signal**: a build that has been running tasks for three days still carries
  the timestamp of its `BUILD_STARTED`.
- `last_activity_at` is the activity signal: the newest of the build's whole
  event stream, its `last_active_at`, and any pending scheduler wake-up
  (`needs_tick_at`).

Reaping or displaying the former would call a busy build idle. This was
misread twice during one piece of work — hence its presence here.

Note that stale-build _ordering_ deliberately still sorts on `last_active_at`,
because it is index-backed and ordering only decides which stale builds a
capped call takes first. Correctness comes from the filter, not the sort.

### 5. `blocked_by_external` is only computed when the build looks stalled

The frontier populates it only when the build has nothing actionable and
nothing running — the same condition the scheduler uses to decide a build
cannot progress, and the only state in which the answer is wanted. Computing
it always would put a per-edge sort on the hot path of every healthy build's
linger polls.

Consequence for every consumer: **an empty list means "not externally
blocked, _or_ not stalled"**. It is never proof that no external blocker
exists for a build that is still making progress.

## Open questions

- Should `RUNNING` continue to carry the "last attempt failed" role, or is
  per-attempt history a separate concern from current state?
- Resident and reactive scheduling are arguably the same scheduler with
  different linger semantics and different frontier sources. Unifying them
  would remove one of each duplicated pair (two claim resolvers, two
  skip-blocked implementations, two staleness heuristics, two discovery
  paths). Worth costing.
- Reactive mode has no task-level retry policy: execution-backend retries
  cover in-container exceptions, not spawn failures, OOM kills, or preemption
  after a partial write. No amount of refactoring supplies this.
