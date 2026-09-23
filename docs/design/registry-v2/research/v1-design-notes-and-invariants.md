# Registry v2: recorded invariants, decisions, harness, open questions

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the source at that commit. Not maintained: read
> [../design.md](../design.md) for the design and [../plan.md](../plan.md)
> for current status.

**Sources**

- The public worktree `stardag-worktrees/sta-105`, abbreviated **PUB**.
- The Linear document **AH**, "Architecture health, September 2026". It was
  reachable.

**Tags**

- **KEEP**: v2 should keep the rule.
- **DROP?**: a candidate for dropping.
- **CONTRA**: the v2 decision already contradicts the rule.

**The v2 decision the tags are measured against**

- Fully breaking, with no migration.
- Two hashes per task instance.
- The scope is deterministic: `(deployment_id, exec_config_hash)`.
- Each build gets a `plan` entity.
- Edges live on task instances.
- Completion and the claim are global, keyed on the completion hash.

---

## 1. Invariants and architectural rules

### Identity, structure, scope

(Source for this group unless noted: PUB `docs/design/scope-keyed-dependency-structure.md`, SKDS.)

| #   | Rule (one sentence)                                                                                                                                                                                          | Source                                  | Tag                                                                                                                                                                      |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 1   | A task id promises the world state that completing the task establishes. It does not promise the upstream set it was built from; a downstream asks for its upstream's world state, not for how it got there. | SKDS "Two identities"; AH principle 1   | KEEP                                                                                                                                                                     |
| 2   | Completion identity hashes the identity parameters only, never code. The user keeps "same id, same output" true by bumping `__version__`.                                                                    | SKDS "Two identities"                   | KEEP (this is the v2 completion hash)                                                                                                                                    |
| 3   | Structure is a function of code and structure-significant config. It matters only for incomplete tasks, because discovery never walks past a complete one.                                                   | SKDS                                    | KEEP                                                                                                                                                                     |
| 4   | Edges are keyed by a scope made of the code id and the `dependencies_only` config hash. The stardag environment is the outer scope. Environment variables are excluded by contract.                          | SKDS "The rule", "The scope key"        | CONTRA: v2 uses `deployment_id` rather than the git-SHA code id, and `exec_config_hash`. If that hash includes `execution_only` values, it also contradicts rule 12.     |
| 5   | Within a scope edges only grow and nothing retracts them. Gating can therefore only over-approximate, and under-gating (the only route to wrong output) is unreachable.                                      | SKDS "Why it is sound"; AH principle 5  | KEEP. This is the core soundness argument; it must be restated for edges on task instances.                                                                              |
| 6   | Sharing structure inside a scope is licensed by the snapshot contract: mutable inputs are snapshotted and referenced by a parameter.                                                                         | SKDS "Why the sharing is licensed"      | KEEP                                                                                                                                                                     |
| 7   | Levels 2 and 3 (`dependencies_only`, `execution_only`) come only from one build config, via field defaults, and are never passed at init. Otherwise one task id could have two structures in one build.      | SKDS "One mechanism"; AH principle 6    | KEEP                                                                                                                                                                     |
| 8   | No per-task dependency id exists. A task's level-2 identity is implicitly the pair (task id, build scope).                                                                                                   | SKDS; (maintainer notes)                | CONTRA. v2's second hash is option B: a level-2 id per task. STA-60 said B "needs per-build-per-task state the registry has no row for"; the v2 plan entity is that row. |
| 9   | Nothing at levels 2 or 3 is persisted per task. `task_data` is the identity-level hash-mode dump, and the build row carries the config.                                                                      | SKDS "What is persisted"                | CONTRA or rework: task instances now carry their structure hash.                                                                                                         |
| 10  | Level-2 values are hashed through their field (hash-mode serialisers, float truncation). `compat_default` is rejected on non-identity fields.                                                                | SKDS                                    | KEEP                                                                                                                                                                     |
| 11  | An environment variable may affect execution, never output or structure. A breach over-gates and never produces wrong output, and the registry warns on within-scope divergence of a dynamic set.            | SKDS; AH principle 7                    | KEEP                                                                                                                                                                     |
| 12  | Changing an `execution_only` setting does not disturb shared structure.                                                                                                                                      | (maintainer notes)                      | CONTRA if `exec_config_hash` covers level 3. Decide explicitly.                                                                                                          |
| 13  | A build has one config for its life. A re-trigger with a different config is refused: "start a new build".                                                                                                   | SKDS; (maintainer notes)                | KEEP                                                                                                                                                                     |
| 14  | The code id is the full git SHA of a clean tree, or a fresh UUID with a warning for a dirty tree. A dirty tree never shares a scope.                                                                         | SKDS "The scope key", "What this costs" | CONTRA or rework under `deployment_id`. The laptop-at-deployment-SHA-shares-scope property probably disappears.                                                          |
| 15  | Only the scheduler compares the code-id half of the scope. Workers compare nothing, and record their yields under their own code's scope.                                                                    | SKDS "Who computes it"                  | KEEP the principle "attribute yields to the code that discovered them". The mechanics change.                                                                            |
| 16  | A redeploy is a new scope, even when only a comment changed.                                                                                                                                                 | SKDS "What this costs"                  | KEEP (implied by `deployment_id`)                                                                                                                                        |

### Plans, rollover, deployments

| #   | Rule                                                                                                                                                                             | Source                                                                                | Tag                                                                                                                  |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- |
| 17  | A build's plan is closed under the dependency relation, pruned at complete tasks. A gate outside the plan is a permanent deadlock.                                               | PUB `execution-claims-and-liveness.md` (ECL) "Builds collaborate"; (maintainer notes) | KEEP. It becomes a property of the plan entity.                                                                      |
| 18  | Closure runs at registration and again whenever a build stalls, so edges a scope-mate wrote later are admitted.                                                                  | SKDS "What it buys"; ECL (superseded paragraph)                                       | KEEP                                                                                                                 |
| 19  | Plan membership is per scope: registration events carry the scope, and the plan is the set of tasks registered under the current scope. Found live, when a rollover under-gated. | (maintainer notes)                                                                    | CONTRA: the plan entity replaces `events.scope_key`. The lesson stays: plan membership and edges must share one key. |
| 20  | A running build follows the live deployment. The first tick on new code re-plans it, and a tick still on the old code exits `superseded`.                                        | SKDS "Rollover"; AH principle 5                                                       | KEEP semantics. In v2 a re-plan is naturally a new plan row.                                                         |
| 21  | Rollover only moves forward: the tick consults the registry's current deployment record, and a deploy that cannot record itself fails loudly.                                    | (maintainer notes)                                                                    | KEEP. `deployment_id` makes the deployment record primary.                                                           |
| 22  | A root whose identity parameters the new code changed cannot be rehydrated. The whole build fails with "re-trigger".                                                             | SKDS "Rollover" step 5                                                                | KEEP                                                                                                                 |
| 23  | A deployment is 1:1 with Modal's, recorded as `(app_name, code_id, deployed_at)`. Nothing is kept alive beside the current one.                                                  | SKDS; (maintainer notes)                                                              | KEEP (the family/handle design is already dropped)                                                                   |
| 24  | Provenance is frozen at status time (`latest_status_scope_key`). The environment-wide graph follows each node's provenance scope.                                                | (maintainer notes); SKDS "Reading the graph"                                          | KEEP the concept. Re-key it to the plan or task instance.                                                            |
| 25  | There are no phantoms: registration requires every declared upstream to be registered first, and an unknown id is a 400.                                                         | SKDS "Reading the graph"; (maintainer notes)                                          | KEEP                                                                                                                 |
| 26  | `dependency_task_ids: list[str] \| None`. `None` means "not declaring"; `[]` means "no upstreams". Registration sends what discovery computed and `None` for tasks it pruned at. | SKDS "What survived"; (maintainer notes)                                              | KEEP                                                                                                                 |
| 27  | Old SDKs are tolerated: unknown upstream ids are dropped for a COMPLETED downstream; synthetic `build:<uuid>` scopes; legacy edges kept with a NULL scope.                       | (maintainer notes)                                                                    | DROP (fully breaking)                                                                                                |

### Claims, executions, cancellation

| #   | Rule                                                                                                                                                                                         | Source                                                    | Tag                                                                                                          |
| --- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------ |
| 28  | A build is a request for roots, not an owner of tasks. Which build completes a task is irrelevant.                                                                                           | ECL; AH principle 3                                       | KEEP                                                                                                         |
| 29  | The execution claim is the only cross-build coordination.                                                                                                                                    | ECL; AH principle 4                                       | KEEP (on the completion hash)                                                                                |
| 30  | The claim is a status, not a lease: `RUNNING` plus an expiry, arbitrated `FOR UPDATE` in the same transaction as the event and the limit slots. There are no heartbeats and no lock table.   | ECL "The design"; (maintainer notes)                      | KEEP. Do not reach for a lock table.                                                                         |
| 31  | Sticky COMPLETED: completion beats running, one comparison on one row.                                                                                                                       | ECL                                                       | KEEP                                                                                                         |
| 32  | Target existence is ground truth. Registry writes are best-effort because the system converges from it.                                                                                      | (maintainer notes); AH principle 2                        | KEEP                                                                                                         |
| 33  | Authority to revoke is build-scoped: a build cancels only what it holds (409 `not_claim_holder`).                                                                                            | ECL; (maintainer notes)                                   | KEEP                                                                                                         |
| 34  | Nothing revokes an execution automatically, and the server never reaches an execution backend.                                                                                               | ECL "Nothing revokes an execution automatically (STA-81)" | KEEP                                                                                                         |
| 35  | A terminal build transition (cancel and fail alike) releases the build's claims, server-side, in one implementation.                                                                         | ECL; (maintainer notes)                                   | KEEP. The two operator `cascade` switches are temporary (STA-103).                                           |
| 36  | "Revocation is not a result": CANCELLED and SKIPPED are reset and run within the budget; FAILED is left to `fail_mode`; RUNNING is the only status that blocks a trigger.                    | SKDS "What a build does with a shared task"; ECL table    | KEEP                                                                                                         |
| 37  | An execution is not a claim. A container whose execution is no longer named may still be running.                                                                                            | PUB `executions-as-records.md` (EAR)                      | KEEP                                                                                                         |
| 38  | The execution id is minted by the client before the claim. The same id from the same build is a retry. A non-claiming start that names a superseded execution under a live claim is refused. | EAR                                                       | KEEP                                                                                                         |
| 39  | Absence is never a mismatch: a missing id falls back to the executor/ref pair (rolling-deploy tolerance).                                                                                    | EAR                                                       | DROP (no version skew in a breaking v2). The asymmetry between a NULL ref and a NULL identity still matters. |
| 40  | The fold preserves the recorded id; `TASK_RETRIED` is the reset.                                                                                                                             | EAR                                                       | KEEP                                                                                                         |
| 41  | No `executions` table. One column, `tasks.latest_execution_id`, which would be a strict prefix of such a table's key.                                                                        | EAR banner                                                | KEEP by default. Re-open only if v2 needs attempts as rows (see open questions).                             |
| 42  | `last_active_at` covers lifecycle only; `last_activity_at` is the activity signal.                                                                                                           | ECL correction 4                                          | KEEP                                                                                                         |
| 43  | The cancellation check defaults to allow (running on is harmless); report validity defaults to deny.                                                                                         | (maintainer notes)                                        | KEEP                                                                                                         |

### Wake-ups and scheduling

| #   | Rule                                                                                                                                                                                                                                             | Source                             | Tag                                                     |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ---------------------------------- | ------------------------------------------------------- |
| 44  | Flagging and spawning are separate. Every task-status transition, including the one to RUNNING, flags every live reactive build holding the task. A transition out of RUNNING also flags builds queued on the task's limit keys.                 | (maintainer notes)                 | KEEP. "Holding" should be defined over plan membership. |
| 45  | The server never spawns user code, including on self-hosted deployments.                                                                                                                                                                         | (maintainer notes)                 | KEEP                                                    |
| 46  | The scheduler is the mini-watchdog. It drains `POST /builds/wake-candidates` (RUNNING, reactive, flagged, no live lease, not handed out within about 120 s; stamped atomically with `SKIP LOCKED`), so each build is handed out once per window. | (maintainer notes)                 | KEEP                                                    |
| 47  | Time-based events (a claim lapsing) are the watchdog's alone; the tick has no timer.                                                                                                                                                             | (maintainer notes); AH principle 8 | KEEP                                                    |
| 48  | One Modal (workspace, environment) per stardag (workspace, environment).                                                                                                                                                                         | (maintainer notes)                 | KEEP (it matters for `deployment_id`)                   |
| 49  | The tick is idempotent and single-flighted by the scheduler lease plus the exit handshake.                                                                                                                                                       | (maintainer notes)                 | KEEP                                                    |

### Process and engineering rules

(Source for this group: maintainer notes ("Architectural rules earned here"), unless noted. All tagged KEEP.)

| #   | Rule                                                                                                                                                                                    | Source                     |
| --- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| 50  | Do not key a decision on what accompanies a fact; key it on the fact.                                                                                                                   |                            |
| 51  | Write the invariant before the code, and choose its default from which error costs more.                                                                                                |                            |
| 52  | The size of a change is a design signal.                                                                                                                                                |                            |
| 53  | Assert on durable state, never on a report from a process that can be preempted. Wait on a state, never on a clock.                                                                     | STA-78; (maintainer notes) |
| 54  | Per-build state has as many consumers as there are execution paths: bootstrap, tick, worker (thread pool, process pool, lifecycle on or off), resident builder, resume, re-trigger, UI. | (maintainer notes)         |
| 55  | Fakes follow the server's seams, not the caller's convenience.                                                                                                                          |                            |
| 56  | "No code path writes this row" is a claim about both engines.                                                                                                                           |                            |
| 57  | An assertion about absence is not a constraint until it has been seen to fail.                                                                                                          |                            |

---

## 2. Decisions and their reasons

### Why per-build config transport was expensive (STA-60, PR #346)

Source: (maintainer notes), "Review round one" through "Round thirteen".

The core mechanics never moved after round five:

- the scope key
- per-scope edges
- over-approximating gating
- global completion and claim

Every later finding sat on a **seam where one fact had two sources of truth
or crossed a process boundary**:

- config carried by contextvar, by env var and as JSON
- the pickle store versus registry rehydration
- the elision flag versus inferred module patterns
- a refusal in one layer absorbed by a tolerate-outages path in another

Concrete defects found:

- Round 1: 13 findings, 7 real.
  - `run_in_executor` copies no ContextVar, so thread-pool and process-pool
    tasks saw default values while the scope was hashed from the config.
  - The resident Modal builder's executor got neither the config nor the
    scope.
  - The config context was entered before the engine's `try`, so it leaked
    into the caller.
  - A migration `DISTINCT gen_random_uuid()` never deduplicated.
- Rounds 2–4:
  - An echoed scope must _equal_ the claimed one, not merely be present.
  - A bare re-trigger must read the stored config.
  - `significance` must be validated when the field is created.
  - The synthetic `build:<uuid>` shape must match exactly, and a client may
    not claim another build's placeholder.
  - A malformed forwarded config must fail the attempt.
- Round 5:
  - The server's reserved-shape check had drifted from the SDK's.
  - Closure expanded from a COMPLETED root.
  - Closure admissions were not charged against the event quota.
- Rounds 6–7:
  - `scope_required` on resume.
  - Async adapters were not forwarding scope and config.
- Rounds 8–13 (rollover):
  - Datetime config values failed late at `json.dumps`.
  - A pickle-loaded task kept the old code's level-2 and level-3 defaults.
  - Deploy exited 0 against a server without the deployments route.
  - The rollover gate read inferred patterns instead of the elision decision.
  - `warn` mode swallowed `RegistryTooOldError`; rule: tolerate an outage,
    never a refusal.
  - A caller-constructed executor forwarded no config.
  - Round 13 was the first with nothing to act on.

Live-tier findings the unit tests missed:

- A worker recomputing the full scope needed every configured class to be
  importable. Fix: compare the code-id half only.
- Plan membership was build-wide while edges were per scope. After a
  rollover the children looked ready and ran before their inputs existed:
  **under-gating**.

The rollover direction change removed a whole layer:

- family/handle names
- `deployment=`
- `gc`
- one watchdog per handle

That layer existed only to serve the premise "a build is bound to one code
version", which soundness never needed.

**Implication for v2.** A plan entity that carries config, scope and
membership explicitly, and that every execution path reads from the
registry, removes most of these seams. It must still be grepped against
every path in rule 54.

### "Don't key on what accompanies a fact"

Source: (maintainer notes).

Three instances:

- Reconstructing "which execution is mine" from event-log proxies, in eight
  places, which twice disagreed.
- Gating a CI retry on the call phase instead of positively identifying a
  transport timeout (seven rounds).
- Asserting that something happened because a preemptible process reported
  it.

**v2 corollary.** Key completion on the completion hash, key structure on
the instance hash, and key membership on the plan id. Never infer one from
another.

### Why cancellation went cooperative

Sources: (maintainer notes) ("Why this exists"); ECL (STA-81); EAR.

Sorting every issue since STA-40 by root cause gave one answer. All three
production incidents sat in **reactive × active cancellation**: a
short-lived scheduler with no handles, reaching into containers.

The failure was structural:

- A cascade releases claims so the next build can take over.
- From that instant the task row names someone else's execution while yours
  still runs.
- So every query about the present is wrong by design.

**Decision.**

- A cancel marks the build and releases its claims (and a fail does the
  same).
- A worker checks at its own checkpoints whether it is still wanted
  (`stardag.cancellation_requested()`) and exits cleanly if not.
- Hard stops are human: `stardag builds stop` lists the running executions
  _while the claims are held_, stops them, then cancels the build.
- The `executions` table was dropped with the feature.

**Accepted cost.** Containers may run on after release. That is harmless
because output is content-addressed. Side effects outside the target were
never protected.

Rejected alternative: going back to resident builds. The economics rule it
out, and STA-21 costed unification as no-go.

### "An edge is evidence asserted by an act"

Source: (maintainer notes); SKDS "The abandoned paths".

The earlier principle, from STA-40/41/42 (PRs #331 and #332): a static edge
is _declared_ and superseded by the next declaration that omits it; a
dynamic edge is _discovered_ and retracted when its attempt is abandoned.
Two live builds declaring different static sets was a conflict, refused by
default.

It was abandoned because:

- **Retraction turns every question into a question about time.** Six
  rounds produced 13 findings, three of them introduced by fixes, nearly all
  "a historical fact mistaken for a current one".
- **Superseding a shared global edge deletes another build's gate.** That
  gives wrong output, not waste: the fatal objection.

What replaced it:

- #340's "immutable declarations" was rejected for its version-bump cascade.
- Per-build edges were sound but re-ran a shared fan-out's pre-yield section
  once per build.
- Scope-keyed edges won.

**Status.** Already superseded. AH restates the survivor as "dependency
edges are evidence asserted by _code_", recorded under a scope. v2 should
keep that restatement and must not reintroduce retraction.

Its companion, **"an execution ref is not a claim"**, stands (rule 37).

### Plan closure and re-running at stall

Sources: ECL; (maintainer notes); SKDS.

History:

- Closure was introduced because a gate outside the plan deadlocked: nothing
  else would run the gating task.
- It originally ran once, at registration. An edge written later by a
  concurrent build's worker yield was invisible, which is why the
  wait-or-fail verdicts and the owner-liveness lookup survived.
- STA-60 made closure correct within a scope, since edges there cannot be
  stale by code. It now re-runs at stall, inside the frontier read, and only
  on the stalled path.
- That made `blocked_by_external`, the owner-liveness lookup and the blocker
  classification deletable, and they were deleted.
- The #331 "skip a cancelled task's dynamic edges" sharpening was explicitly
  _not wanted_: under the same code the abandoned generation is the right
  one.

Left open from the cancel work:

- Plan membership is append-only.
- There is no way to un-admit a task.
- The reset should precede closure.

**v2.** The plan entity should own membership, closure and stall re-closure
directly.

### Wake-up candidates

Source: (maintainer notes), shipped in v0.22.0.

The hole: only a build's own worker woke that build, so a change made by
another build's tick, the resident engine, the UI or a limit slot reached it
only by luck or through the watchdog.

The design:

- **Flag on the server.** One transition hook, on every transition, marks
  every live reactive build holding the task (`needs_tick_at`).
- **Spawn from any scheduler.** Each tick drains wake-candidates at the end
  of every acting pass and on every exit.
- **No relation query.** Precise flagging means every flagged, unserved
  build legitimately needs a tick, whoever asks.

This deleted:

- `_wake_neighbours`
- the "shares a task with me" relation
- `CLAIM_RELEASING_STATUSES`

The rejected options:

| Option                       | Why it was rejected            |
| ---------------------------- | ------------------------------ |
| Every writer spawns          | spawn storm                    |
| The server spawns            | would hold Modal tokens        |
| A waiting tick lingers       | resident cost                  |
| Watchdog as the primary path | polls a scale-to-zero database |

Debt listed in §8:

- Task status is written on five server paths. Centralise them in one
  `transition_task()`.
- The lease rides on the deprecated global lock.
- The linger poll fetches the full frontier to read one flag.
- Attempt counts are window functions over the event log.

---

## 3. Scenario harness shape

Sources: `integration-tests/tests_registry_live/conftest.py`,
`src/stardag_integration_tests/registry_live/*`, DEV_README "Registry-live
tests", (maintainer notes).

### Stack

- `provision up/stop/down` deploys, outside pytest:
  - a registry whose Postgres runs inside its own Modal container
    (`min_containers=1`, explicit CPU and memory)
  - `registry-live-dag`, the worker/tick app
  - `registry-live-watchdog`, a separate app
- The watchdog gets its own app because the sweep is scoped by reactive app
  name. Sharing an app would wake the dormant builds other scenarios depend
  on.
- `rollover_app.py` is deployed twice by the rollover scenario itself.
- Teardown is `modal environment delete`.
- Migrations run from scratch on every container start.

### conftest

It deploys nothing:

- `pytest_configure` reads the provisioned coordinates, sets the SDK env
  overrides, pops `STARDAG_PROFILE` and records the expected API URL.
- Each module calls `registry_live_guard()` at import, so a wrong or missing
  stack is a collection error.

Fixtures:

- `deployment`, session-scoped: a `Deployment` with `api_url` and
  `assert_same_container()`.
- `_registry_survived`, autouse: checks the boot nonce after every scenario.
  A container recycle raises `RegistryContainerRecycled`, and CI
  re-provisions and retries once.

The `pytest_runtest_makereport` wrapper classifies every failure in every
phase:

- a transport timeout is the only retryable failure, and a boot probe
  records it
- anything else forbids a retry
- a classification error fails closed

xdist runs about 12 scenarios concurrently.

### Helpers

- `_wait`:
  - `wait_for_task_status(task_id, expected=, build_id=, timeout=)`
  - `wait_for_terminal(build_id, timeout=)`, which also waits for the
    terminal tick's summary
  - `wait_until`
  - `task_status` / `build_status`
  - `tick_summaries`
  - `assert_trail_complete` / `require_complete_trail` /
    `trail_may_be_truncated`: trail observations become counted skips when
    a reporter was preempted
  - `assert_remaining_work_outlasts_linger` / `assert_dormancy_is_forced`:
    measured preconditions
  - `describe(build_id)`, the failure dump
- `_events`:
  - `task_events`, `events_by(events, build_id)`, `resets_by`
  - `describe_events`
  - `wait_until_registered`, `first_event_at`
  - `spawned_executions`: counts distinct executor refs from the event log,
    not from the trail
  - `earliest_start_and_server_now`
- `tasks.py` fixture tasks:
  - `get_range(limit, salt)`, `square`, `get_sum`
  - `slow(values, seconds, limit_key)`, `cooperative`, `fails`
  - `SuspendingParent`, `FanIn`, `Resumable`
  - `ConfiguredChain` (a `dependencies_only` upstream choice)
  - `ConfiguredFanOut`, `slow_on_worker`, `WorkerFanIn`

### How a scenario reads

Typical length is 110–500 lines, most of it docstring and commented
rationale.

1. The module docstring states the incident or property, and **the
   alternatives the shape rules out**.
2. Module-level constants are sized against each other, for example
   `SHARED_SLEEP_SECONDS=75` against `B_LINGER_SECONDS=15`, with a comment
   saying what breaks if they cross.
3. `pytestmark = [registry_live, timeout(900)]`.
4. One test function taking `deployment`:
   - `salt = uuid4().hex` keeps task ids fresh per run.
   - It builds tasks and runs `app.build_trigger(root, reactive=True, tick_kwargs={linger_seconds, poll_interval_seconds}, build_config=...)`.
   - It waits on a **state**, never a sleep.
   - It asserts a measured precondition, then `wait_for_terminal == "completed"` with `describe(...)` messages.
   - It then asserts on durable state:
     - event-log observables, for example "build 2 has no events on U1"
     - `spawned_executions == N` (exact equality; a weakened bound counts
       as a false pass)
     - `registry.build_get(...).scope_key` comparisons
   - Trail observations go through `require_complete_trail`.
   - Diagnostics print to stderr.

The whole tier takes about 10 minutes, and rollover doubled it.

### Existing scenarios

| Scenario                              | One line                                                                                                                                                        |
| ------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_reactive_e2e`                   | A worker spawns the next tick for a build with no scheduler anywhere.                                                                                           |
| `test_claim_race`                     | Two builds want one task, and the real Postgres arbiter lets exactly one run it.                                                                                |
| `test_cross_build_wake`               | A blocker completing in build A wakes a dormant build B, via a wake-candidate flag; no watchdog is deployed.                                                    |
| `test_wake_storm`                     | Several dormant builds flagged at once are each woken exactly once.                                                                                             |
| `test_limit_slot_wake`                | A freed concurrency slot wakes a build queued on it that shares no task.                                                                                        |
| `test_suspended_blocker`              | A task suspended on dynamic children holds no claim, and is waited on rather than reset.                                                                        |
| `test_failed_blocker`                 | A shared FAILED task is a result, and a second build leaves it alone.                                                                                           |
| `test_watchdog_sweep`                 | One sweep dispatches one tick per build and returns in seconds.                                                                                                 |
| `test_wide_fan_out`                   | A layer wider than the per-pass spawn cap (`max_spawns_per_tick=8`, 24 leaves) throttles and does not stall; each task spawns once.                             |
| `test_scheduler_lease_live` (4 tests) | Concurrent acquires grant exactly one; a lapsed lease is taken over on the real clock; failing renewals are a blip; an outage spanning the TTL stops the lease. |
| `test_cancel_authority`               | A cancelled build cannot touch another build's execution.                                                                                                       |
| `test_structure_scope_static`         | A changed static upstream (via `dependencies_only` config) does not gate the next build, and build 2 has no events on U1.                                       |
| `test_structure_scope_dynamic`        | A narrower build never runs an abandoned wide dynamic generation.                                                                                               |
| `test_shared_structure_scope`         | A scope-mate reuses the parent's yield, and the pre-yield section runs once across two builds.                                                                  |
| `test_rollover`                       | Deploys as code A and redeploys as B mid-build; the build completes on B with its scope moved and `rolled_over` recorded.                                       |
| `test_execution_identity` (3 tests)   | A retried claim is granted to the attempt that won it; a superseded worker's start cannot take the task back; a cancelled build's worker stops itself.          |
| `test_interruption_classification`    | A platform-cancelled input is reported, not misread as a preemption.                                                                                            |
| `test_builds_stop`                    | `stardag builds stop` stops only the selected worker's calls, then cancels.                                                                                     |

Planned in STA-60 but not present: a scenario asserting that an
execution-only config change leaves the scope unchanged.

---

## 4. Open questions the records left for v2

1. **Attempts.** Should an attempt be an execution? Today it comes from a
   window function over start events plus a Python twin, and the retry and
   resumption budgets depend on it (EAR).
2. **RUNNING and failure history.** Should RUNNING keep the "last attempt
   failed" role, or should per-attempt history be separate (ECL)?
3. **Option B cost.** STA-60 took option A (one scope hash) and warned that
   one class's config change invalidates every edge in the scope. v2's
   instance hash is option B. What exactly does it hash: task id plus
   effective level-2 values, or overrides only? Does `compat_default`
   become meaningful again?
4. **Is level 3 in the scope?** Is `exec_config_hash` the full config
   (levels 2 and 3) or level 2 only? Rule 12 depends on the answer.
5. **Rollover under a plan entity.** Does a re-plan create a new plan row
   (history kept) or mutate one? What does the old tick compare to exit
   `superseded`?
6. **Claim × plan at rollover.** STA-74 was to state and pin these
   invariants live. It included "a real-scoped build accepts only scoped
   registrations". AH ranks this the likeliest next subtle bug.
7. **Late old-code workers.** Lifecycle reports carry no scope, so an
   old-code worker finishing after a rollover is attributed to the new
   scope (deferred to STA-72). Should the v2 execution id tie reports to a
   plan or instance?
8. **Executions dropped by a re-plan.** Cancelling executions a re-planned
   build no longer needs is STA-67, optional.
9. **Edge retention.** Retention for edges of retired scopes is STA-68; old
   rows are kept today.
10. **Authority holes.** Authority rules have holes wherever the task holds
    nothing: STA-93, -94, -96, -99, -100. STA-94 is a preempted tick between
    claim and spawn stalling the build for the TTL.
11. **Cascade switches.** Removing the two remaining operator `cascade`
    switches so claim release is unconditional is STA-103.
12. **Declared starts.** A non-detached start is inferred from absence, not
    declared (STA-90).
13. **Status write paths.** Task status is written on five server paths.
    One `transition_task()` would make the flag hook unbypassable.
14. **Lease location.** The scheduler lease sits on a deprecated global
    lock. A `builds.scheduler_lease_until` column could replace it.
15. **Linger poll.** The linger poll re-reads the full frontier to read one
    flag. Use a slim endpoint or long-poll (the linger-poll-cost record).
16. **Retry policy.** There is no task-level retry policy for spawn
    failure, OOM or a preempted partial write (ECL).
17. **Per-build infrastructure knobs.** cpu, memory and timeout per build
    via `with_options` is STA-64. Does the timeout feed the claim TTL?
18. **Plan un-admission.** Can a task be un-admitted from a plan, and should
    reset precede closure on a trigger (cancel-authority "Left open")?
19. **Rollover preconditions.** Collapse them into one decision function
    (the STA-60 root-cause debt item).
20. **Registration code.** Registration is written twice in a
    ~5000-line `routes/builds.py` (STA-71), with a deadlock-prone lock
    structure (STA-63).
21. **Harness exposure.** The harness puts the whole database in one
    container. Revisit PGDATA-on-a-Volume only if a timeout is ever shown
    to be the registry's own slowness. STA-86 (hypothesis C, a body-read
    stall) is open, and `RetryTransport` cannot retry a body-read stall
    (STA-54).
22. **Local builds and `deployment_id`.** Does a local `sd.build()` get a
    deployment id? Can a clean laptop still share a deployment's scope?
    What happens to dirty trees?

---

## 5. Docs a v2 supersedes or rewrites

### Design notes (`docs/design/`)

| Note                                  | What v2 does to it                                                                                                                                                                                               |
| ------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `scope-keyed-dependency-structure.md` | **Supersede or rewrite.** The scope key, "no per-task dependency id", "nothing persisted per task", plan membership and code-id mechanics all change. Keep "The abandoned paths" and "What survived" as history. |
| `execution-claims-and-liveness.md`    | **Partial update.** Re-key the claim to the completion hash. Update the closure paragraphs and the table of stale heuristics (mostly deleted already). The claim-as-status reasoning stays.                      |
| `executions-as-records.md`            | **Mostly stands.** Drop the rolling-deploy "absence" tolerance. Re-open "attempt as execution" if v2 adds attempts.                                                                                              |
| `README.md`                           | Update the index.                                                                                                                                                                                                |

A `principles.md` is also planned (the AH principles half, STA-82). v2
should write it as its foundation.

### User docs (`docs/docs/`)

| Page                                                         | What it claims today                                                                                                                                                                                                                                                                                             | v2 action                                                                          |
| ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| `concepts/parameters.md`                                     | "Parameter Hashing"; "Three levels of significance" (levels 2/3 from the build config, never at init; the env-var contract at about line 151); "The Task ID" (derived from name, namespace, version and parameters; the full lineage in the hash)                                                                | Rewrite: two hashes                                                                |
| `concepts/build-execution.md`                                | "The registry as the execution ledger" (task state per environment, one row per task id); "Builds collaborate"; "Cancelling work: the worker asks"; "Structure scope" (code id plus `dependencies_only` hash, edges only grow, the scope moves on redeploy, one config per build); "Shared tasks" (status table) | Rewrite                                                                            |
| `concepts/modal-orchestration.md`                            | "Reactive scheduling / A build's life", "Retries and interruptions", "Wake-ups", "The watchdog", "Deployments and code versions" (a deployment is `(app_name, code_id, deployed_at)`, rollover and `superseded`, workers code-agnostic, yields under their own scope)                                            | Rewrite the deployments and rollover sections; the wake-ups sections largely stand |
| `how-to/evolve-dags.md`                                      | The five-step guide: significance, `build_config` per build, change dependencies with no bump, deploy new code and roll over, migrate from `hash_exclude`; plus "What to expect in the UI" (scope chip, provenance)                                                                                              | Rewrite; drop the `hash_exclude` migration                                         |
| `how-to/integrate-modal.md`                                  | "Build config: per-build knobs…" (config stored with the build, installed by bootstrap, tick and worker; a re-trigger reuses it), "Redeploying while builds run", "Declaring your task modules", reactive sections                                                                                               | Update                                                                             |
| `concepts/dependencies.md`                                   | Declaring dependencies and injection                                                                                                                                                                                                                                                                             | Minor update: edges on instances                                                   |
| `concepts/index.md`                                          | Links parameters and the three levels                                                                                                                                                                                                                                                                            | Minor update                                                                       |
| `platform/api.md`                                            | Endpoint list for builds, tasks and locks; stale (no scope, deployments or plan routes)                                                                                                                                                                                                                          | Rewrite for the v2 entities                                                        |
| `platform/ui.md`, `getting-started/registry-ui.md`           | DAG visualisation and task details                                                                                                                                                                                                                                                                               | Update for the plan and instance views                                             |
| `configuration/profiles.md` "Environment Variable Overrides" | Covers `STARDAG_*` connection overrides only; not the env-var contract                                                                                                                                                                                                                                           | Probably unaffected                                                                |

Also affected:

- `RELEASE_NOTES.md` and `CHANGELOG.md`: a v2 entry.
- The DEV_README scenario table: it lists 10 scenarios, is stale against
  the 18 modules, and needs updating.

---

## 6. "Architecture health, September 2026" (Linear, reachable)

Dated 2026-09-19, at the end of STA-60 (#346). It is a dated judgement to be
revisited. Its principles half is meant for public `docs/design/principles.md`.

### Eight principles that held

1. A task id is a promise about output, not about the upstream set.
2. A target makes completion a fact about the world; discovery stops there.
3. A build is a request, not an owner; the only must-not is two concurrent
   executions.
4. The claim (RUNNING plus an expiry, taken atomically) is the only
   cross-build coordination; authority to revoke is build-scoped.
5. Edges are evidence asserted by code, recorded under a structure scope,
   and within a scope they only grow. A build's scope follows the live
   deployment.
6. Three levels of significance with one mechanism: identity at init, levels
   2 and 3 from one build config per build.
7. Env vars may affect execution, never output or structure.
8. The registry is the scheduler state: a write flags the builds it
   concerns, and the watchdog covers claim lapses.

### Assessment

The architecture is right for serverless execution. STA-60's core survived
nine rounds and one live hole: plan membership was build-wide while edges
were per scope.

Weighted concerns:

- **The pickle store.** Since retired by STA-69.
- **Two coordination mechanisms meet at rollover.** The per-task claim and
  the per-build scope interact when an old worker is mid-flight on a task
  the new plan no longer contains. It is correct because the claim alone
  prevents double execution, and it is the likeliest next bug (STA-74).
- **Forward-only rollover depends on the deployment record.** The document
  calls this the right call.
- **Old SDKs against a new server** can only over-gate.
- **No soak test yet** (STA-73 canary).

### Accidental complexity

- `routes/builds.py` is about 5000 lines, with registration written twice
  (STA-71/63).
- Executions are reconstructed from the event log (STA-50, since resolved by
  deletion).
- A pickle path sits beside the registry path (STA-69).
- The tick has about 20 counters (STA-72).
- The API suite runs on SQLite while production is Postgres (STA-72).

The registry-live tier is "the most valuable asset in the repo". Keep it
fast and green (STA-65 and STA-66 are real flakes).

### Recommended first after STA-60

STA-69 together with STA-71, then STA-49.
