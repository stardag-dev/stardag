# Registry server logic: verification report

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the source at that commit. Not maintained: read
> [../design.md](../design.md) for the design and [../plan.md](../plan.md)
> for current status.

All paths are relative to `app/stardag-api/src/stardag_api/` unless
prefixed `lib/` (SDK).

## 1. HTTP API surface (all under `/api/v1`, main.py:109-136)

Caller abbreviations: W = SDK worker (Modal runner), D = SDK driver or resident
engine, T = reactive tick or bootstrap, C = CLI, U = UI. They come from grepping
`lib/stardag/src/stardag` and `app/stardag-ui/src` for the paths.

**Builds: lifecycle** (routes/builds.py)
| M | Path | Purpose | Caller |
|---|---|---|---|
| POST | /builds | create build, BUILD_STARTED, synthetic scope `build:<id>` if none (1008-1083) | D/T |
| GET | /builds | list; status / reactive_app_name / idle filters (1086) | C/U/T(watchdog) |
| POST | /builds/bulk-cancel | reaper and bulk cancel, dry_run, cascade (1235) | C/U |
| POST | /builds/wake-candidates | hand out flagged reactive builds and stamp tick_requested_at (1531) | T, D(Modal) |
| GET | /builds/{id} | one build (1573) | D/T/U |
| POST | /builds/{id}/complete | BUILD_COMPLETED, **does not release claims** (1609) | D/T/U |
| POST | /builds/{id}/fail | release claims, skip the blocked closure, BUILD_FAILED (1648) | D/T/U |
| POST | /builds/{id}/cancel | release claims, BUILD_CANCELLED (1756) | C/U/D |
| POST | /builds/{id}/exit-early | BUILD_EXIT_EARLY (1879) | D |
| POST | /builds/{id}/resume | BUILD_RESUMED if there is activity; optional scope move and config check (1914) | D/T |
| POST/PUT/DELETE | /builds/{id}/scheduler-lease | acquire, renew, release the per-build tick lease (2066-2144) | T |
| POST/GET/DELETE | /builds/{id}/notify | set, read, clear `needs_tick_at` (2147-2282) | W (POST), T (GET/DELETE) |
| POST | /builds/{id}/roots | append root ids (2285) | D/T |
| PUT | /builds/{id}/reactive-meta | app name and tick kwargs (2317) | T (trigger) |
| PUT | /builds/{id}/scope | set or roll over scope_key and fix build_config (2516) | T (bootstrap), D |
| POST | /builds/{id}/skip-blocked | recursive-CTE skip of the blocked closure (2626) | T |
| GET | /builds/{id}/frontier | actionable, running, roots, counts, attempt counts; **may write** (2675) | T, U |
| GET | /builds/{id}/tasks, /events, /graph | read views (4916, 5044, 5084) | U/C |
| POST/GET | /builds/{id}/tick-summaries | last-N tick diagnostics (tick_summaries.py:51,141) | T / U |

**Builds: tasks within a build** (routes/builds.py)
| M | Path | Purpose | Caller |
|---|---|---|---|
| POST | /builds/{id}/tasks | single-task register (3410) | D (sequential fallback only, `lib/.../_sequential.py:405-428`) |
| POST | /builds/{id}/tasks/bulk | bulk register, ≤1000 per call, `id_only` (3606) | D/T/W |
| POST | .../tasks/{tid}/start | TASK_STARTED; `claim`, `execution_id`, `claim_ttl_seconds`, `enforce_limits`, `limit_key` (4028) | T (claim), W (self-report), D |
| GET | .../tasks/{tid}/execution-status | cooperative-cancel check, read-only (4266) | W |
| POST | .../complete, /fail, /suspend, /resume, /skip, /retry, /waiting-for-lock | task events (4377-4786) | W/D/T (retry: T discovery, U) |
| POST | .../interrupt, /preempt | end-of-execution reports with an identity check (4412, 4478) | W |
| POST | .../cancel | TASK_CANCELLED, holder-scoped (4647) | U/C/D |
| POST | .../dependencies | dynamic (or static) edges for an existing task (4558) | W, D |
| POST | .../artifacts | upsert artifacts (4788) | W/D |

**Other resources**
| M | Path | Purpose | Caller |
|---|---|---|---|
| GET | /tasks, /tasks/{tid}, /{tid}/artifacts, /{tid}/events, /{tid}/metadata; POST /tasks/graph | env-wide task views (tasks.py:29-305) | U, C (`_cli/_stop.py` uses GET /tasks), D (metadata) |
| GET | /tasks/search, /keys, /values, /columns | search (search.py) | U |
| POST | /locks/{name}/acquire, /renew, /release | distributed lock; release can record TASK_COMPLETED (locks.py:51-246) | D (`registry/_lock.py`) |
| GET | /locks, /locks/{name}; /locks/tasks/{tid}/completion-status | lock list; "is there any TASK_COMPLETED event" (locks.py:249-350) | D/C |
| GET/PUT/DELETE | /concurrency-limits[/{key}], GET /{key}/holders, POST /{key}/holders/{tid}/evict | limits admin; evict writes TASK_FAILED (concurrency_limits.py:92-414) | C/U/T |
| GET/POST | /deployments | deployment records, upsert on (env, app, code_id) (deployments.py:107,135) | C (deploy), T |
| GET | /target-roots | (target_roots.py:26) | C |
| GET | /version, /health | main.py:131-136 | D |

**Version-skew surface.** `sdk_compat.py` defines **no routes**. It holds a
header parser and a dormant minimum-version floor, `minimum_version=None`
(sdk_compat.py:82). The skew handling lives inside routes:

- `cancel?cascade=` is a deprecated no-op (builds.py:1763-1774).
- `tasks/{tid}/cancel?if_executor/if_executor_ref` is refused with 400
  `conditional_cancel_removed` (4691-4706).
- The frontier's `blocked_by_external` is always empty, "kept on the wire for
  older SDKs" (2826-2831).
- `dependency_task_ids=None` means "not declaring". Unknown upstreams of a
  COMPLETED downstream are dropped for pre-`None` SDKs (3348-3407).
- `execution_id` is absent for pre-identity SDKs, and the server falls back to
  the `(executor, executor_ref)` pair (561-626, status.py:241-348).
- `scope_key` is optional everywhere, and None means the build's current scope
  (2442-2457).
- A missing `/wake-candidates` route is documented as a 404 that the SDK
  tolerates (1553-1555).

## 2. Registration transactions

There are **four code paths** that write task rows and/or edges.

**(A) Single register**, `register_task` (3410-3598), one commit:

1. A plain SELECT checks whether the task exists (3465). Only the limit check
   uses it.
2. `_declared_upstreams` resolves the declared upstreams or raises 400 (3472).
3. `take_task_rows` runs over sorted `{task, *upstreams}` (3497-3520). The own
   row is a real row; the upstreams get `_lock_probe_row` placeholders. The
   statement is `INSERT ... ON CONFLICT (uq_task_environment_taskid) DO UPDATE
SET task_id=task_id WHERE false RETURNING task_id` (3063-3113). This inserts
   the row if it is missing, and locks without rewriting if it exists.
4. The row is re-read without FOR UPDATE (3536).
5. `_reconcile_dependency_edges` (3558) repeats what step 3 did. It re-SELECTs
   the upstreams (3261), **runs `take_task_rows` a second time** over the same
   sorted set (3312-3320), then inserts edges with `ON CONFLICT
(scope_key, upstream, downstream) DO NOTHING` (3120-3147).
6. It writes a TASK_PENDING event if the row was created and TASK_REFERENCED
   otherwise, stamped with `scope_key=edge_scope` (3571-3581), through
   `transition_task`.
7. `_close_plan_over_dependencies` (3583), then commit (3591).

**(B) Bulk register**, `register_tasks_bulk` (3606-4025): **one request, one
commit per chunk**. The SDK sends chunks of 50 sequentially, in post-order
(`lib/.../_reactive/_discovery.py:179,322-337`). The server:

- de-dupes first-wins (3670) and resolves upstreams (3727);
- runs one `take_task_rows` over sorted `{batch ∪ referenced upstreams}`
  (3812-3832);
- reads the batch back (3843);
- does an **inline** edge build and insert (3885-3906). It does not go through
  `_reconcile`, so it skips the impure-structure warning and the second lock;
- adds per-task events with µs-offset timestamps (3915-3933);
- replaces limit keys, only for rows it created or pre-saw, and never for a
  RUNNING task (3960-3969);
- runs `transition_task` per event (3978), then closure over the whole batch
  (3981), then commits (3991).

**(C) Dynamic edges**, `add_task_dependencies` (4558-4630), one commit. It
finds the downstream by environment, not by build (4607), and calls
`_reconcile_dependency_edges(is_dynamic=request.is_dynamic)` (4619). It writes
**no event, no plan membership and no closure**, and it flags **no wake-up**.
An edge insert is not a status transition.

**(D) Closure admission**, `_close_plan_over_dependencies` (2928-3060). It adds
TASK_REFERENCED events, which is plan membership only, and is called from (A),
(B) and the frontier's stall path (2792).

**"The registration transaction is written twice": confirmed, and it is closer
to three times.**

- (A) and (B) duplicate the row dict (3500-3513 and 3790-3804), the
  create-or-lock step, the event choice, the limit checks and the closure call.
- Edge insertion exists in two shapes: inline in (B) at 3886-3906, and via
  `_reconcile` for (A) and (C).
- (A) locks the same rows twice in one transaction. That is harmless because it
  is the same sorted order, but it is redundant.

**Re-registration on rollover** is not a separate path. The scheduler re-runs
(B) with `payload.scope_key` = the new scope. Tasks that already exist get
TASK_REFERENCED events under the new scope, and new-scope edges are inserted.
Old-scope rows are untouched (2466-2469).

**"task_data is frozen at first registration": confirmed.** The conflict branch
is `DO UPDATE SET task_id = task_id WHERE false` (3107-3111), so an existing
row is never rewritten. The same applies to `task_name`, `task_namespace`,
`version` and `output_uri`. The only per-registration mutable state is
`TaskLimitKey` (3960) and the events. The payload's `task_data` on a conflict
is silently discarded, with no mismatch check.

**Edge semantics:**

- `is_dynamic` is set by the first insert and never flipped (3232-3234).
- Edges are **append-only per scope**. No code path deletes a `TaskDependency`
  row: the grep found no `delete(TaskDependency` anywhere in the src tree.
- Unknown upstream ids return 400 `unknown_upstream_task_ids` (3179-3195). The
  old phantom rows are gone, but the `is_phantom` column is still written as
  False (3509, 3800, 3172).

## 3. The runnable rule as implemented

**Plan membership.** `_plan_task_ids(build, scope)` (2417-2439) is the set of
DISTINCT `events.task_id` where the build matches, the event type is
TASK_PENDING or TASK_REFERENCED, and `events.scope_key == scope`. Membership is
per (build, scope), and it is re-derived from the event log on every query.

**"Registered dependencies"** are `task_dependencies` rows with `scope_key ==
build.scope_key`. They are **scope-global, not per-build**: any scope-mate's
registration, or any worker's yield, gates this build. Legacy NULL-scope edges
gate nothing (2733). The graph view does count them, though (graph.py:88-93),
which is a small divergence.

**Upstream completion is global per task row:** `upstream.latest_status !=
COMPLETED` (2734-2743). This is the environment-wide denormalised status, not
per build and not per scope.

**The actionable set** is (2745-2760):

```
Task.id IN plan(build, build.scope_key)
AND latest_status IN (PENDING, SUSPENDED, RUNNING, INTERRUPTED, CANCELLED, SKIPPED)
AND NOT EXISTS (edge in build.scope_key whose upstream is not COMPLETED)
```

The status list is at 157-182. RUNNING is included in "actionable". `running`
is a separate query over every RUNNING plan member, gated or not (2762-2778).
FAILED is excluded, because failure handling is `fail_mode` on the SDK side.

**Stall path.** If both sets are empty, the server re-closes the plan over the
scope's edges from its non-terminal members, commits **inside a GET**, and
recomputes (2783-2824).

**Blocked / skip-blocked.** `_blocked_by_failures` (2543-2623) is a recursive
CTE.

- Seeds are FAILED, CANCELLED and SKIPPED plan members.
- It propagates down scope edges only through upstreams in {FAILED, CANCELLED,
  SKIPPED, PENDING, SUSPENDED, INTERRUPTED}. A COMPLETED or RUNNING
  intermediate stops propagation.
- It selects PENDING, SUSPENDED and INTERRUPTED plan members FOR UPDATE,
  ordered by task_id. These become TASK_SKIPPED through `/skip-blocked` (2626)
  and, inside `/fail`, in the same transaction (1706-1730).
- SKIPPED re-enters `actionable` once it is gated open (175-181).

**SUSPENDED.** It is in the frontier set (159). A SUSPENDED parent is gated by
its dynamic edges P→Ci (same scope), and becomes actionable when every Ci is
COMPLETED. The tick then issues a claiming TASK_STARTED. Because
`claim_is_live` needs RUNNING (claims.py:148), the claim is granted, and the
fold sets RUNNING and overwrites the executor fields (status.py:826-890). The
server has no "resume" semantics on that path. `/tasks/{tid}/resume`
(TASK_RESUMED) exists for the resident engine. So **"SUSPENDED → run it, a
resume from scratch" is confirmed server-side.** The SDK agrees: "resuming a
SUSPENDED task records a fresh start" (`lib/.../_reactive/_tick.py:289-294`).
TASK_RETRIED also resets SUSPENDED to PENDING and clears the executor and
identity (status.py:38-44, 891-915).

**(a) Can Ci be seen with zero upstreams before its upstream is registered?**
Server side: **no, provided the SDK ordering holds.** The worker's
`suspended()` does three things in order (`lib/.../integration/modal/_runner.py:743-757`):

1. `discover_and_register_aio` on the yielded struct, as bulk chunks in
   post-order, with each child's `requires()` sent as `dependency_task_ids`;
2. `task_add_dependencies(P, Cs, is_dynamic)`;
3. `task_suspend`.

Within a bulk call, Ci's plan-membership event, its row and its static edges
Ci→Uj all land in **one commit** (3875-3991). Uj is in the same or an earlier
chunk (post-order, sequential chunks), and an Uj that is missing is a 400,
never a phantom. So Ci is never a plan member without its declared edges.

The real windows sit next to that:

- **Complete-at-discovery nodes are PENDING with no edges until the final
  mark.** Discovery registers pruned-complete tasks with no
  `dependency_task_ids` and marks them `task_complete` only **after all
  chunks** (`_discovery.py:355-361`). A brand-new row created that way is a
  PENDING plan member with zero edges, and therefore actionable, for that
  window. A concurrent tick can claim and run a task whose target already
  exists. That is wasted work and the output is correct.
- **A swallowed registration failure turns P into a re-run.**
  `_register_dynamic_deps` runs under `_guard`, which swallows errors, and the
  suspend is posted regardless (753-756). If registration failed, P is
  SUSPENDED with no dynamic edges and immediately actionable, so it re-runs
  from scratch, re-yields, and can loop.
- **Children are invisible after a rollover.** Children and P→Ci edges are
  written under the **worker's** scope (`_runner.py:865-875,909-911`). If the
  build has rolled over, Ci is not in the current plan and P under the new
  scope has no dynamic edges. P then re-runs from scratch under the new code.
  That is by design, and it means gating is _absent_, not _early_.
- **Scope-mates are not woken.** (C) writes no event and flags no build. A
  scope-mate build that holds P in its plan but never admitted the Ci is
  gated on the Ci. It recovers only through the stall-time closure (2783),
  which runs only when that build has nothing actionable and nothing running.

**(b) Pruned-complete task that later becomes incomplete.**

- The server has **no status-reset path for COMPLETED.** COMPLETED is sticky:
  the early return is at status.py:816-824, and the comment at status.py:1122
  says "COMPLETED means the target exists".
- TASK_RETRIED excludes COMPLETED (38-44, 895).
- A claiming start on a COMPLETED task is 409 `task_already_completed`
  (builds.py:838-842).
- The lock-release path can only _add_ COMPLETED (services/lock.py:302-345).
  No route deletes events or edges.

So the claimed failure (a zero-upstream plan member treated as runnable) does
**not** happen from a deleted target. The actual failure is the opposite. The
row stays COMPLETED, it is never in `_FRONTIER_NON_TERMINAL_STATUSES`, closure
refuses to expand from it (3013-3014), and it can never be claimed. A deleted
target is **never regenerated through the registry**, and its downstreams gate
open immediately on a stale COMPLETED.

If a later discovery sees it incomplete, the SDK walks `requires()` and bulk
registers it with its static edges (the upstreams go first). Those edges are
**added** to the scope and nothing is removed, but they gate nothing because
the row is COMPLETED.

A zero-edge runnable plan member can arise only where the row is non-COMPLETED
while no scope edge was ever written for it:

1. the discovery window above;
2. a failed or omitted `task_complete` mark after pruning;
3. a row in FAILED/CANCELLED/... whose target now exists and which was pruned.

Edges are scope-global, though, so any scope-mate that expanded it earlier
still gates it.

## 4. Claims and executions

- **The claim _is_ `latest_status == RUNNING`** on the single task row
  (claims.py:1-35).
- **Liveness** is `RUNNING AND (expires_at IS NULL OR expires_at > app_now)`
  (claims.py:132-167). A NULL expiry is live forever and needs an operator to
  release it.
- **Compare-and-set** happens in `_create_task_event`, on the FOR UPDATE task
  row (791). With `claim=true` it denies 409 `task_already_running` when
  `claim_is_live` holds and the start is not `_claim_is_this_same_execution`
  (805-837). It denies 409 `task_already_completed` when the task is COMPLETED
  (838-842).
- **Same execution** requires the same `latest_status_build_id`, and then
  either equal `execution_id` (the id decides alone), or, with no id, equal
  `(executor_ref, executor)`. A request with neither is denied (561-626).
- **Takeover.** An expired claim denies nothing. The start overwrites build,
  executor, identity and expiry in one fold (status.py:826-890).
- **Non-claiming starts** have two refusals:
  - 409 `task_cancelled` if the task is CANCELLED or SKIPPED and the start
    names an `execution_id` (687-708, 844-874);
  - 409 `execution_superseded` if a live claim holds a different
    `execution_id` (711-750, 876-943).
- **Expiry** is `event.created_at + claim_ttl_seconds`, or the server default
  (claims.py:94-113). It is set on every STARTED and RESUMED
  (status.py:888-890, 925-928), cleared on leaving RUNNING, and pulled forward
  to the grace window on PREEMPTED (1045-1051).
- **execution_id (STA-50).** A granted claim (`metadata.claim`) always writes
  it, including a NULL. Other starts write it only when they name one, and it
  is preserved on silence (status.py:864-882). TASK_RETRIED clears it (915). A
  claim redelivery (same id, no executor) does not blank the ref (854-863).
- **Per task_id, globally per environment.** The row is unique on
  (environment_id, task_id) and not keyed by build or scope. Two builds and two
  scopes contend for one claim.
- **Revocation** by `/cancel` is holder-scoped through `may_revoke` for
  RUNNING, SUSPENDED and INTERRUPTED (claims.py:61-91; builds.py:945-962).
- **Completion is global and sticky.** `TASK_COMPLETED` sets COMPLETED
  unconditionally, from any build, with **no holder or identity check**
  (status.py:802-813; builds.py:4377-4388). The same absence of an authority
  check applies to `/fail`, `/suspend`, `/skip` and `/resume` (4391-4410,
  4544-4555, 4633-4644, 4717-4728). Only STARTED, CANCELLED, INTERRUPTED and
  PREEMPTED are guarded.
- **Concurrency slots** are the `TaskLimitKey` rows joined to
  `live_claim_filter`. `enforce_limits` locks the limit rows FOR UPDATE, ordered
  by key (4203-4263).

## 5. Rollover / scope change

- **Entry points.** `PUT /scope` (2516-2540) and `/resume?scope_key=` (1970-1982)
  both call `_apply_scope` (2460-2513).
- **What changes: only `builds.scope_key`**, plus adopting `build_config` if
  it was NULL. A different config is 409 `scope_mismatch`. A synthetic-shaped
  key is 400 `synthetic_scope_claimed` (2384-2408).
- **What stays.** Nothing else changes: no task rows, no events, no edges.
  Old-scope edges remain for other builds (2466-2469).
- **Order.** The docstring requires the caller to register the plan under the
  new scope _before_ moving the scope (2525-2527). Until it does, the new
  scope's plan is empty. After the move, the frontier, skip-blocked and
  closure all read `build.scope_key` only.
- **Provenance.** Every non-registration task event is stamped with the
  build's scope at write time (status.py:720-731), and the fold copies it to
  `latest_status_scope_key`, which the graph uses (graph.py:68-73).
- **Plan closure.** `_close_plan_over_dependencies` is a level-by-level BFS
  over scope edges from the given pks. It adds non-COMPLETED upstreams that are
  not yet in the plan, and never expands from a COMPLETED downstream. It
  checks the event quota per level and flushes per level (2928-3060). It runs
  in every registration, (A) and (B), and in the frontier's stall path. There
  is no other "stall re-run".
- **Wake-ups.**
  - `transition_task` calls `flag_after_task_transition` on every
    `latest_status` change. That sets `needs_tick_at` on every _other_ RUNNING
    reactive build that has any event for the task, locked `SKIP LOCKED`
    ordered by id. On a transition out of RUNNING it also flags builds holding
    PENDING tasks with the same limit keys (wakeups.py:89-192). This relation
    is by event, not by scope.
  - `/notify` sets the flag on the caller's own RUNNING build and stamps
    `tick_requested_at`, un-stamping if a lease is live
    (builds.py:2183-2240).
  - `/wake-candidates` hands out ≤20 flagged, lease-free builds that are not
    stamped within 120 s, FOR UPDATE SKIP LOCKED, oldest flag first
    (wakeups.py:323-376).
  - The scheduler lease is two columns on the build, owner-checked
    (wakeups.py:203-306).
- **Tick summaries** are purely diagnostic: a verbatim JSON insert with an
  insert-time prune to N per build (tick_summaries.py:51-138). They have no
  scheduling effect.

## 6. Build status

- **Storage.** A stored denormalised column, `builds.latest_status` (plus
  started, completed, is_resumed and triggered_by), folded in the same
  transaction as each build event by `apply_event_to_build`
  (status.py:1096-1176), under `_get_build_for_update` (builds.py:433-468).
- **No stickiness.** It is last-event-wins by arrival. RUNNING goes to
  {COMPLETED, FAILED, CANCELLED, EXIT_EARLY}, and any status goes back to
  RUNNING through BUILD_RESUMED.
- **The server never derives build completion from task states.** A build
  goes COMPLETED only when a client posts `/complete` (the tick or driver
  terminal detection, or the UI).
- **Who drives transitions:**
  - Server: the reaper (`sweep_stale_builds`, `run_periodic_sweep`, opt-in,
    build_cleanup.py:380-465) and `/bulk-cancel`.
  - SDK tick or driver: complete, fail, exit-early, resume.
  - UI or CLI: cancel, complete, fail.
- **Cleanup.** `cascade_cancel_build_tasks` (build_cleanup.py:205-307) emits
  TASK_CANCELLED for tasks with any event in the build, in RUNNING, SUSPENDED
  or INTERRUPTED, where `latest_status_build_id == build`. It locks FOR UPDATE
  ordered by task_id, and PENDING is left alone. Its task selection is **all
  build events, not the scoped plan**.
  - `/cancel` and `/fail` always cascade. `/fail` also runs the blocked
    closure.
  - Bulk-cancel and the reaper honour a `cascade` switch, flagged as
    temporary (STA-103, 217-227).
  - **`/complete` and `/exit-early` release nothing**, so a build marked
    complete while it still holds RUNNING tasks keeps their claims until they
    expire.
  - Reactive builds are excluded from reaping unless asked (1267-1272). Idle
    is measured as max(event time, last_active_at, wake-up) (1257-1265).

## 7. Contradictions and surprises

1. **The lock release commits a completion even when it refuses.**
   `release_lock_with_completion` writes TASK*COMPLETED \_before* it checks lock
   ownership (services/lock.py:325-345). The route commits (locks.py:237) and
   _then_ raises 409 when not released (239-243). A non-owner release with
   `task_completed=true` therefore durably completes the task and returns 409.
   It also bypasses every claim or identity rule.
2. **No authority on completion or failure reports.** A stale worker from a
   superseded execution can `/fail`, `/suspend` or `/skip` a task another build
   now RUNS. That releases the live holder's claim, because the fold's FAILED
   branch overwrites RUNNING (status.py:931-941). `/complete` from anyone wins
   stickily. Only INTERRUPTED and PREEMPTED carry the ownership and identity
   rule (status.py:164-210).
3. **Deadlock-avoidance machinery**, and it is substantial:
   - `take_task_rows` uses `ON CONFLICT DO UPDATE ... WHERE false` as an
     insert-or-lock in one statement, and needs `_lock_probe_row` placeholder
     dicts (3063-3176).
   - One global sorted-by-task_id order is shared by (A), (B), (C),
     skip-blocked and cascade (2612-2616, 3287-3290).
   - `_flag_builds` uses SKIP LOCKED explicitly to dodge a build→task versus
     task→build inversion (wakeups.py:98-106).
   - A limit-keys trade drops keys on a raced row to avoid a lock wait
     (3943-3959).
   - "Not known to be reachable" 409 guards exist for a row that is neither
     created nor present (3542-3553, 3852-3868).
4. **A frontier GET writes and commits** (stall-path closure, 2810-2811). The
   frontier is also "the hottest read" at about 3 s per build (2856-2862). It
   issues roughly 7 statements plus a CTE, and plan membership is a DISTINCT
   scan over events every time.
5. **Plan membership and cascade disagree.** Plan membership is per
   (build, scope) from events, but `cascade_cancel`, `_preview_cascade_task_ids`,
   `list_tasks_in_build`, `/graph` and the wake-up holder relation all use "any
   event in the build". A task from an earlier scope is outside the plan but is
   still cancelled on cascade and still woken.
6. **Dynamic edges are silent.** `add_task_dependencies` writes no event and no
   wake-up, does not check that the downstream belongs to the build, and does
   not run closure (4558-4630). Structure written by one build becomes gating
   for scope-mates with no notification.
7. **`/complete` does not release claims**, which is asymmetric with `/fail`
   and `/cancel`. The comment at 1659-1665 says "a terminal transition releases
   the build's claims", but only two of the four terminals do.
8. **Minor divergences:**
   - Legacy NULL-scope edges gate nothing in the frontier but count in the
     graph (2733 versus graph.py:88).
   - `/locks/tasks/{tid}/completion-status` checks for any TASK_COMPLETED
     event (lock.py:72-80), not `latest_status`. That is equivalent only
     because COMPLETED is sticky.
   - `list_tasks_in_build` still calls `get_all_task_global_statuses` event
     replays (4975) rather than the denormalised columns.
   - `register_task` locks the same rows twice (3497, 3312).
   - `is_phantom` is still written.
9. **Row writes without FOR UPDATE:** `/roots`, `/reactive-meta` and
   `DELETE /notify` write the build row without the lock (2299, 2347, 2279).
   They fold no event, so this does not break the documented rule, but they
   are racy against lifecycle writes.
10. **Two replays and one row fold.** `get_task_status_in_build` and the
    attempt and interrupt count queries re-implement the fold rules in Python
    and SQL twins (status.py:127-141, 410-455, 499-680, 1179-1333), with
    explicit "must agree" notes. That is a standing drift risk.

**Size estimate for builds.py:** 5142 lines, of which 2797 are code, 1216
docstring, 618 comment and 511 blank.

Replaceable by a v2 with a single registration and plan model (about 45-50% of
the code):

- both registration paths, edge reconcile, `take_task_rows`, probes and
  declared-upstream tolerance (≈1100 lines, 2928-4025);
- the frontier, closure and skip-blocked (≈380, 2543-2923);
- the claim-identity and report-authority helpers inside `_create_task_event`
  (≈450, 561-1002, with status.py's twins);
- the compat shims.

Largely kept (thin, correct, well-bounded):

- build CRUD, list and bulk-cancel (≈600, 1008-1594);
- lifecycle endpoints (≈430);
- lease, notify and wake-candidates (≈330, plus wakeups.py);
- scope set (≈185);
- artifacts, lists, events, graph (≈355);
- task-event route wrappers (≈400).

services/status.py (1461 lines) is the other half. `_apply_event_to_task`
(759-1071) is the one fold worth keeping as the spec. The per-build replays
(1179-1461) and SQL twins are candidates for deletion if v2 keeps per-(build,
task) state in a row instead of in event replays.
