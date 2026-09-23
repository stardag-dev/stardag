# Linear state for the STA-105 planning session (stardag workspace)

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the source at that commit. Not maintained: read
> [../design.md](../design.md) for the design and [../plan.md](../plan.md)
> for current status.

Workspace checked with `get_workspace`: name "stardag". Nothing was written to Linear.

Tags: DISSOLVED / RE-SCOPE-ONTO-V2 / CONTINUE-ON-V1 / SUBSUMED / UNRELATED. For issues already closed, the tag says what v2 does with the problem or mechanism the issue settled.

## 0. Headline: where the STA-105 summary is out of date

1. **STA-69 is Done.** PR #349 merged as `ae8fb09c` on 2026-09-21 and shipped in SDK v0.25.0. The summary still says "decide whether to finish it on v1 or fold it into v2". That decision was made by events. It landed in the identity-only `task_data` shape. The 09-22 "variant-aware" sequencing comment was written after the merge, so it never applied.
2. **STA-50 is Done.** PR #361 merged as `c2bcbdd3` on 2026-09-22. The SDK half is in v0.25.0 but does nothing yet, and the server half waits for `server-v0.5.0`. It shipped **identity only**: one column, `tasks.latest_execution_id`. STA-78 dropped the executions table. The summary's §5 defines an orphan as "an execution (STA-50's row)" and says "executions attach to `task_claim`". **No such row exists.** v2 must either add an executions table or define orphans against `latest_execution_id` plus plan membership.
3. **STA-65 is Done.** PR #350 (`cadf74cb`) shipped in v0.25.0. "Continue" is moot. Its rule (the worker is the authority on how its execution ended; the probe is a fallback after `worker_report_grace_seconds`) is design input for v2.
4. **STA-67 is Canceled** (2026-09-21, under STA-78). The reason given: the scheduler never stops containers, and reclaiming compute is a later `--not-in-current-plan` filter on `stardag builds stop` (STA-80). The summary calls STA-67 "own issue, already exists today". To act on it, reopen it or file a v2 issue for the filter.
5. **STA-63 part B:** STA-78's Phase 2 still lists it as pending after STA-81, and STA-81 is now merged. The STA-105 summary calls it dissolved. These two coordination issues disagree. v2 still serialises claim arbitration on `task_claim`, so a request-boundary deadlock retry may still be worth having.
6. **STA-78's "stabilisation window" conflicts with starting v2.** Decision 3 says no new scheduling features until Phases 1–3 are done. Done-when includes four weeks of live-tier data with no product-defect reds, plus `principles.md` merged. v2 is the largest feature possible. STA-78 needs an explicit amendment, or v2 needs to be recorded as the exception.
7. **STA-72 is more than "partly moot".** Several of its items describe v1 entities that v2 re-creates: the deployment ordering by `deployed_at`, `code_id()` reading the cwd (this bears on local placeholder deployments), `graph.py` BFS versus a recursive CTE, the UI deployments view, and plan versus build-wide task lists.
8. **STA-85's remaining work is only the visual design pass** (dark theme, one accent colour, type scale). The structural work is merged (#371, #372, #374, #375, #376). It barely touches entities, so the conflict risk the summary warns about is small.
9. **STA-62** (September batch release) is still In Progress, although STA-76 superseded it. It looks stale.

## 1. The issues requested

**STA-95: Per-variant structure keys, level 2/3 values at init, one variant per task id per build**

- **State:** Backlog. Anders. High. Already in project P-STA-1.
- **What it is:** A design issue from 2026-09-22. Level 2/3 values move back to init, stored per variant id (identity plus level 2/3). A single check refuses two variants of one task id per plan. It deletes the `build_config` transport and proposes an `annotation` level, lenient rehydration across code versions, and storing overrides (`model_fields_set`) rather than resolved values.
- **Latest comment:** None.
- **v2:** **SUBSUMED.** v2 is the same move with two hashes instead of three. Its lenient-rehydration and store-overrides reasoning still applies to v2 instance bodies. Close it when implementation issues are cut. Matches the summary.

**STA-69: Retire the pickle-based task store**

- **State:** **Done** (2026-09-21). Anders. High.
- **What it is:** Removed `BuildTaskStore`. `task_modules` is now required for reactive builds. The coverage pre-flight is now fatal (`TaskModulesError`) and `require_pickle_free` is a deprecated no-op. A task is rehydrated only from the registry's `task_data`.
- **Latest comment (09-21):** Merged as `ae8fb09c`, CI 20/20 green. Row added to STA-76, ephemeral stack deleted. Lessons recorded: a clean rebase is a claim about text, not meaning; a test double that raises the wrong error type hides the class of bug that narrowing a catch fixes.
- **v2:** **CONTINUE-ON-V1, already complete.** v2 inherits "rehydrate only from stored data". The body becomes the full instance body per scope instead of identity-only. **Contradicts the summary**, which treats this as undecided.

**STA-50: Make the claim idempotent, with an execution id minted at claim (closes STA-56)**

- **State:** **Done** (2026-09-22). Anders. Urgent.
- **What it is:** It was cut twice. First the executions table was dropped (STA-78). Then the worker-carried identity moved to STA-79. What shipped: `tasks.latest_execution_id`, minted outside the retry loop by both engines, so a retried claim from the same build with the same id is granted.
- **Latest comment (09-21 23:28):** The resident engine was included. Four Copilot rounds. The one regression, fixed: a granted re-delivery blanked the executor ref. 21/21 registry-live. Merge followed as `c2bcbdd3`, and the server half is unreleased (STA-76).
- **v2:** **CONTINUE-ON-V1.** The mechanism carries over as a column on `task_claim`. **Contradicts the summary's model**: there is no executions row for orphans to reference (see §0.2).

**STA-65: A tick's probe of a platform-cancelled input records a failure before the worker's report lands**

- **State:** **Done** (2026-09-20). Anders. Medium.
- **What it is:** The fix is a tick-local `_ReportWindow`. When the backend says a call is gone, the probe waits up to 30 s for the worker's report before recording a failure.
- **Latest comment (09-20):** Two follow-ups. The flaky `test_a_grace_too_large_for_the_container_is_trimmed_to_fit` fails about 3 runs in 6. The changelog entry was missing and was later added in #351. Released in v0.25.0.
- **Residuals recorded on STA-50:** the fallback `/fail` is unconditional, and the report window does not outlive a tick. Both need a conditional, per-execution end on the server, which is v2 design input if executions become records.
- **v2:** **CONTINUE-ON-V1, complete.** The rule carries over unchanged. "Continue" is moot.

**STA-63: Task registration is hard to make deadlock-free (one row, two lifetimes; phantoms)**

- **State:** Backlog. Anders. High.
- **What it is:** Three levers:
  - A: remove phantoms.
  - B: retry `DeadlockDetected` at the request boundary.
  - C: split identity from state.
- **Latest comment (09-20):** Part A was done by STA-60 (unknown upstream is a 400; the migration deleted phantom rows). B and the `ORDER BY … FOR UPDATE` comment fixes are sequenced after STA-50. STA-78 later moved them to after STA-81, which is now merged.
- **v2:** **DISSOLVED** for C and for registration locking: v2 _is_ option C, with insert-only instance upserts and a separate `task_claim`. **B is arguable.** Claim arbitration still serialises on `task_claim`, and B is cheap and covers writers not yet written. See §0.5 for the conflict with STA-78.

**STA-71: Extract task registration from `routes/builds.py`**

- **State:** Backlog. Anders. Medium.
- **What it is:** About 5000 lines, with the three-phase registration transaction written twice. The ask is one `services/registration.py` and `services/frontier.py`, with no behaviour change.
- **Latest comment:** None. STA-78 parks it until after STA-63 B.
- **v2:** **DISSOLVED.** v2 rewrites registration and the frontier queries. Build the v2 server with that service split from the start. Matches the summary.

**STA-67: Cancel the executions a re-planned build no longer needs after a rollover**

- **State:** **Canceled** (2026-09-21). Anders.
- **What it is:** Automatically cancel old-plan executions after a rollover.
- **Latest comment (09-21):** Canceled under STA-78. The scheduler no longer stops containers. They run out, or exit at their next checkpoint (STA-79). A manual `--not-in-current-plan` filter on `builds stop` (STA-80) is a "later nicety".
- **v2:** **RE-SCOPE-ONTO-V2, but as a new or reopened issue.** Under v2 it is a list-only filter: "executions whose task is in no active plan". **Contradicts the summary**, which assumes an open issue. Its orphan definition also needs an execution record (§0.2).

**STA-74: State the claim × scope interaction at rollover as invariants, and pin it with a live scenario**

- **State:** Backlog. Anders. High.
- **What it is:**
  - Write down what happens to an execution started under scope A of a build now planned under scope B.
  - Add two live scenarios: the task is in the new plan, and it is not.
  - Add a server 400 so a real-scoped build accepts only scoped registrations.
- **Latest comment (09-21):** Rescoped under STA-78, to be written against `latest_execution_id` and cooperative cancellation. "Still blocked by STA-50". STA-50 is now done, so the v1 blocker is gone.
- **v2:** **RE-SCOPE-ONTO-V2.** Write the invariants against plan membership, `task_claim` and instance edges. The summary's §5 rollover behaviour is most of the content. The scoped-registration 400 becomes "a registration names its plan". Matches the summary.

**STA-70: `stardag build`, trigger a build with a build config from the command line**

- **State:** Backlog. Anders. Low. Parked by STA-78.
- **What it is:** A CLI that resolves roots from `module:attr` and accepts `--build-config` JSON and `--app`.
- **Latest comment:** None.
- **v2:** **RE-SCOPE-ONTO-V2.** `--build-config` becomes `--env-overrides`/`exec_config`, and roots carry all their parameters. The root-resolution part is still useful. Matches "reconsider".

**STA-72: STA-60 debt, smaller follow-ups**

- **State:** Backlog. Anders. Low. Grab-bag, no comments.
- **Items that become moot in v2:**
  - `is_phantom` dead column.
  - `/resume` taking `build_config` in query parameters.
  - Lifecycle reports stamped with the build's current scope.
  - `WorkerEnv` scope parsing.
  - The worker-without-`STARDAG_BUILD_ID` scope preflight.
- **Items that carry into v2:**
  - `deployments.deployed_at` stamped by the API clock, which decides "current".
  - `code_id()` reading git from the cwd.
  - `graph.py` with one query per BFS level.
  - A UI deployments view.
  - Plan versus build-wide task list in the UI.
  - The Postgres-default API suite.
  - Tooling items: ruff pin, pyright venv, `provision up` note, rollover-scenario marker.
- **v2:** **Partly DISSOLVED, partly RE-SCOPE-ONTO-V2.** See §0.7. More survives than the summary suggests.

**STA-85: Clean up the very bloated UI**

- **State:** Todo (reopened 2026-09-23). Anders. Label UI.
- **What it is:** UI declutter.
- **Latest comment (handover):** #376 merged as `70cb5ced`. Merged overall: #371 header, #372 builds-list id, #374 dialogs and STA-83 row cap, #375 override copy, #376 cancel/stop wording. 322 UI tests. Remaining is the design pass only: dark primary theme, blue as the single accent, status badges keep their colours, `system-ui`. STA-103 and STA-104 are split out. It rides the next server image.
- **v2:** **CONTINUE-ON-V1** (the visual pass). v2 graph and entity views need their own UI issue. The remaining work is largely independent of entities (§0.8).

**STA-76: Next batch release, checklist and coordination (after v0.24.0 / server 0.4.0)**

- **State:** Todo (reopened after an auto-close). Anders. Medium.
- **What it is:** Release coordination. Released so far: `server-v0.4.0` (09-20) and SDK `v0.25.0` (09-22, SDK-only by exception).
- **Pending for the next round:** `server-v0.5.0` with:

  - #361's server half (one additive migration);
  - the STA-80 panel;
  - #365 (STA-88);
  - #368 (STA-79);
  - #373 (STA-81);
  - the STA-85 UI.

  Then SDK `v0.26.0`, which breaks custom `submit_detached`/`RegistryABC` overrides.

- **Latest comments (09-22 evening):** Go/no-go says **GO, server-first, on 2026-09-23**. Compatibility policy until GA: server and SDK are upgraded together, with no compatibility window.
- **v2:** **CONTINUE-ON-V1.** v2 is its own release line. The "upgrade together" policy already fits v2's no-compatibility stance. Matches the summary.

**STA-43: Hash-excluded parameters are frozen at first registration**

- **State:** Canceled (2026-09-20).
- **What it is:** `task_data` was written once per task id, so later builds ran the first build's `hash_exclude` values.
- **Latest comment:** No longer applicable after STA-60 (identity-only `task_data` plus `build_config`).
- **v2:** **SUBSUMED.** v2's per-scope instance body is the real fix. This is the "smoking gun" the STA-105 description cites.

**STA-41: Dependency edges are permanent and environment-global**

- **State:** Done (2026-09-20). PRs #332 and #340.
- **What it is:** Static-declaration conflict detection, and later immutable declarations, then superseded by scope-keyed edges.
- **Latest comment (09-20):** Resolved by STA-60's scope-keyed edges. Retention moved to STA-68.
- **v2:** **SUBSUMED.** Edges live on deterministic task instances. The "detect at discovery and name both paths" pattern is reused for v2's `instance_conflict`.

**STA-40: A cancelled reactive build re-cancels shared tasks, killing a later build's executions**

- **State:** Done (2026-09-15). PR #330.
- **What it is:** The cancel authority rule (409 `not_claim_holder`), `GET /builds/{id}/executions`, and `notify` flagging only running builds. The executions listing was later deleted by STA-81.
- **Latest comment:** None.
- **v2:** **UNRELATED.** The authority rule carries over onto `task_claim`. The automated revocation it was part of is gone.

**STA-42: Abandoned dynamic dependencies gate their parent forever**

- **State:** Done (2026-09-20). PR #331.
- **What it is:** Edge retraction (`superseded_at`), which STA-60 then removed.
- **Latest comment:** STA-60 supersedes the retraction. An earlier open point: plan closure versus reset ordering, and "a plan cannot be un-admitted".
- **v2:** **SUBSUMED.** Per-instance, per-scope edges. The "un-admit from a plan" gap is worth putting on v2's scenario checklist next to the summary's §4.2 (a completed task becoming incomplete).

**STA-44: A timeout 8 s short of its window is classified as a preemption and stalls the build**

- **State:** Done (2026-09-14). PRs #334 and #335.
- **What it is:** Classify by the exception chain. A status-neutral `TASK_PREEMPTED` plus `latest_preempted_at`. A 900 s restart grace. An authority rule for interruptions.
- **Latest comment:** Merged. Lessons recorded: a recorded-but-refused event must be traced to every consumer; wait on a state, never a clock. Modal has no delayed one-off invocation.
- **v2:** **UNRELATED.** Carry `latest_preempted_at` and the grace into `task_claim`.

**STA-60: Solve the central DAG structure challenge in reactive scheduling**

- **State:** Done. Released as SDK v0.24.0 / server 0.4.0 (2026-09-20). PR #346, thirteen Copilot rounds.
- **What it is:** Scope-keyed edges (`code_id` plus `dependencies_only` config), three `significance` levels, per-scope plan membership, `deployments` records, rollover by re-plan, phantoms removed.
- **Latest comments (09-19):** Rollover built and verified live. Plan membership is per scope, which fixed an under-gating hole where an old worker's yield landed in the new plan. Rollover preconditions at that point: a recorded deployment and pickle-free (the second was later removed by STA-69).
- **v2:** **SUBSUMED.** v2 replaces its scope key and `build_config` transport. Keep: "plan membership is per scope", edges only grow within a scope, the deployment record, and rollover re-plans.

**STA-77: `significance=` on a non-Task `StardagBaseModel` cannot survive a build**

- **State:** Done (2026-09-21). PR #354, v0.25.0.
- **What it is:** A gated build-config registry for non-task models.
- **Latest comment:** Five review rounds, all findings about key-space collisions.
- **v2:** **DISSOLVED.** `significance=` and the build config go away. `significant: bool` on nested models must still reach the instance hash, and nested models inherit the hash-exclusion question, so add a test.

**STA-56: Neither engine's claim carries an identity, so a retried claim reports a loss to the winner**

- **State:** Done (2026-09-22). Closed by #361.
- **Latest comment (09-22):** The same symptom has a second cause, STA-94: a tick preempted between claim and spawn.
- **v2:** **UNRELATED.** The mechanism carries onto `task_claim`.

**STA-47: The registry-live tier fails unrelated PRs**

- **State:** Done (2026-09-21).
- **Latest comment:** Closed. The surviving read-timeout class was handed to STA-86. STA-26 was ruled out as the fix.
- **v2:** **UNRELATED.** It is CI. The tier gates v2 work as well.

**STA-48: Two builds registering a shared task at once get a 500**

- **State:** Done. PR #341.
- **What it is:** `ON CONFLICT DO NOTHING … RETURNING`, one sorted lock point.
- **Latest comment:** Fixed. `FOR UPDATE` cannot lock a row that does not exist.
- **v2:** **RE-SCOPE-ONTO-V2**, as a lesson. v2's insert-if-not-exists on `task_claim`, `deterministic_task_instance` and membership must use this pattern and carry its Postgres race tests.

**STA-51: Bulk and single registration insert in different orders and deadlock**

- **State:** Done. PR #343.
- **Latest comment:** Foreign keys take `FOR KEY SHARE` locks nobody wrote. The rule is holding-while-waiting, not sort order.
- **v2:** **RE-SCOPE-ONTO-V2**, as a lesson. v2's edge table has foreign keys to instances, and the membership table references `task_claim`. Re-check lock order.

**STA-52: A retried lease acquire reports a loss to the tick that won it**

- **State:** Done. PR #342.
- **What it is:** Idempotent lease re-acquire. The retry-policy comment now states what a second delivery costs.
- **v2:** **UNRELATED.** The lease is per build and carries over.

**STA-49: A non-claiming `TASK_STARTED` overwrites a live claim held by another build**

- **State:** Done (2026-09-22). Closed by #368 (STA-79).
- **What it is:** 409 `execution_superseded` when all three hold: a live claim, an identity on both sides, and differing ids. Build ownership is deliberately not a condition.
- **v2:** **UNRELATED.** The rule carries onto `task_claim`.

**STA-57: An inherited dynamic dependency that can never complete gates its parent forever**

- **State:** Canceled (2026-09-20).
- **Latest comment:** Dissolved by STA-60's scoped edges.
- **v2:** **DISSOLVED.**

**STA-59: Gate the completed-task declaration shim on the caller's SDK version**

- **State:** Canceled (2026-09-20).
- **Latest comment:** Dissolved by STA-60.
- **v2:** **DISSOLVED.** v2 has no compatibility shims.

**STA-55: Phantom rows are charged against the 24 h task quota**

- **State:** Canceled (2026-09-20).
- **Latest comment:** Phantoms are gone.
- **v2:** **DISSOLVED.** Note for v2: decide which table the 24 h creation quota counts. It is `task_claim` or instances, and the choice changes what "a task" means for quota.

**STA-78: Get complexity under control**

- **State:** In Progress. Anders. Urgent. Top-level coordination.
- **What it is:**
  - Phase 0: STA-86.
  - Phase 1 (STA-50, 79, 80, 81): code complete on 09-22.
  - Phase 2: STA-104 first, then STA-94, STA-100, STA-103, STA-63 B, STA-74, STA-54, STA-75.
  - Phase 3: STA-73 canary, flake-rate metric, plan-scale timing, STA-82 principles.
  - Parked: STA-58, 64, 70, 68, 71, 61, 23, 34.
- **Latest comments (09-22):**
  - 19:53: Phase 1 complete. Plan for 09-23: release server-first, upgrade downstream, STA-94.
  - 21:03: Phase 2 re-ordered, STA-104 first.
- **v2:** **CONTINUE-ON-V1.** It must be reconciled with v2 (§0.6). Its Phase 3 "non-goals" list for STA-82 includes "dependency identity is not part of the task id". v2 keeps that for the completion hash, but it needs rewording now that there is an instance hash.

## 2. Open issues not in the list above

| Issue   | Status           | One line                                                                                                                             | Tag                                                                                                                                           |
| ------- | ---------------- | ------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------- |
| STA-105 | Todo             | The v2 design issue itself (P-STA-1)                                                                                                 | n/a                                                                                                                                           |
| STA-104 | Todo, High       | A user's task cancel is undone by the next tick; CANCELLED means both "revoke and retry" and "given up on"; no per-task stop         | CONTINUE-ON-V1. v2 design should decide where "given up on" lives (plan membership is the natural place)                                      |
| STA-100 | Backlog, High    | A claiming start revives a CANCELLED task, including an idempotent retry of the cancelled claim                                      | CONTINUE-ON-V1 (the claim state machine carries to `task_claim`)                                                                              |
| STA-103 | Backlog          | Remove the remaining cascade switches; a terminal transition always releases claims                                                  | CONTINUE-ON-V1 (the v2 API should simply omit them)                                                                                           |
| STA-99  | Backlog          | Re-attaching after taking over a lapsed claim asserts a different identity                                                           | CONTINUE-ON-V1                                                                                                                                |
| STA-94  | Backlog, High    | A successor tick cannot recognise its own build's abandoned claim (tick preempted between claim and spawn), so it stalls for the TTL | CONTINUE-ON-V1 (the same gap exists on `task_claim`)                                                                                          |
| STA-93  | Backlog          | `TASK_FAILED` has no authority rule and can clobber another build's live claim                                                       | CONTINUE-ON-V1                                                                                                                                |
| STA-96  | Backlog          | A late start can revive a task between `TASK_RETRIED` and the next claim                                                             | CONTINUE-ON-V1                                                                                                                                |
| STA-90  | Backlog          | A non-detached execution cannot be told apart from a Modal claim whose metadata lookup failed                                        | CONTINUE-ON-V1                                                                                                                                |
| STA-54  | Backlog          | Idempotency audit: what a second delivery costs at each retried POST                                                                 | RE-SCOPE-ONTO-V2 (design every v2 POST, including plan creation and batch commits, idempotent from the start)                                 |
| STA-84  | Backlog          | `builds stop` at scale: unbounded table, serial Modal cancels                                                                        | CONTINUE-ON-V1                                                                                                                                |
| STA-86  | Todo, Urgent     | Registry-live tier timeouts against its own registry (nine occurrences; hypothesis C)                                                | UNRELATED (CI; gates v2 too)                                                                                                                  |
| STA-102 | Todo, High       | Classifier does not treat `RemoteProtocolError` as a transport fault                                                                 | UNRELATED                                                                                                                                     |
| STA-97  | Backlog          | An outage during pytest collection is invisible to the timeout count                                                                 | UNRELATED                                                                                                                                     |
| STA-98  | Backlog          | A leaked Modal environment on an open PR is not collected until the PR closes                                                        | UNRELATED                                                                                                                                     |
| STA-26  | Backlog          | Move CI's Modal workload to the `stardag-ci` workspace                                                                               | UNRELATED                                                                                                                                     |
| STA-36  | Backlog          | A PR with merge conflicts runs no CI and `gh pr checks` reports passing                                                              | UNRELATED                                                                                                                                     |
| STA-35  | Backlog, High    | The SDK declares Python 3.10 support that is not tested                                                                              | UNRELATED (a v2 major is a chance to drop 3.10)                                                                                               |
| STA-37  | Backlog          | Under `from __future__ import annotations`, a class task passed to `Depends[T]` warns of a false generic mismatch                    | UNRELATED                                                                                                                                     |
| STA-73  | Backlog          | Post-release canary that redeploys mid-build                                                                                         | RE-SCOPE-ONTO-V2 (under v2, rollover means a new plan in the same build)                                                                      |
| STA-82  | Todo, High       | Public `docs/design/principles.md` with non-goals                                                                                    | RE-SCOPE-ONTO-V2 (write it against v2's entities or it ages immediately)                                                                      |
| STA-58  | Backlog (parked) | Opt-in: fold declared upstream ids into the task hash                                                                                | RE-SCOPE-ONTO-V2 (decide against v2's two-hash model; it conflicts with "structure is not in the completion hash")                            |
| STA-68  | Backlog (parked) | Retention window for edges of retired structure scopes                                                                               | RE-SCOPE-ONTO-V2 (retention of instances and edges per old deployment)                                                                        |
| STA-64  | Backlog (parked) | Per-build worker infrastructure settings without a redeploy                                                                          | RE-SCOPE-ONTO-V2 (overlaps `exec_config`/per-trigger `env_overrides`; infrastructure such as GPU or memory is not an env var, so only partly) |
| STA-61  | Backlog (parked) | DagGraph node/edge construction inside `useMemo`, untested                                                                           | RE-SCOPE-ONTO-V2 (the graph model changes)                                                                                                    |
| STA-39  | Backlog          | Refresh `.claude/skills/stardag/`, which has drifted                                                                                 | RE-SCOPE-ONTO-V2 (do it after v2)                                                                                                             |
| STA-75  | Backlog          | A deployed resident builder cannot opt a custom `run_function` out of lifecycle reporting                                            | CONTINUE-ON-V1                                                                                                                                |
| STA-66  | Backlog, Low     | Scheduler-lease takeover granted inside the TTL under load                                                                           | UNRELATED                                                                                                                                     |
| STA-23  | Backlog (parked) | Deep-chain wake-up latency                                                                                                           | UNRELATED                                                                                                                                     |
| STA-34  | Backlog (parked) | A re-flagged build waits out the 120 s window                                                                                        | UNRELATED                                                                                                                                     |
| STA-62  | **In Progress**  | September batch release (the predecessor of STA-76)                                                                                  | UNRELATED. Looks stale; probably close                                                                                                        |

## 3. Project P-STA-1, "Registry v2: core entities re-design"

It holds only **STA-105** (Todo, Urgent) and **STA-95** (Backlog, High). No implementation issues exist yet.

## 4. Conventions for filing implementation issues

- **Team:** "Stardag" (id `3b6e8fde-e4f8-4f5b-85f3-10c5c9aabb14`). Every issue is assigned to Anders.
- **Statuses:**

  | Status      | Type      |
  | ----------- | --------- |
  | Backlog     | backlog   |
  | Todo        | unstarted |
  | In Progress | started   |
  | In Review   | started   |
  | Done        | completed |
  | Canceled    | canceled  |
  | Duplicate   | duplicate |

- **Status usage:**
  - Design and unscheduled issues sit in Backlog.
  - Scheduled work moves to Todo.
  - "In Review" appears in older histories; recent PR work tends to go straight from In Progress to Done.
  - "No longer applicable" closures use Canceled with a comment naming what dissolved them.
- **Labels:**
  - `Bug`, `Improvement` and `Feature` are used most.
  - `UI` means the change touches `app/stardag-ui`.
  - `orchestration-cleanup` is a team label for scheduler debt from the v0.22.0 era.
  - Many recent issues carry no label.
  - There is no server, SDK or v2 label. Surface is stated in the issue text, for example "Surface: server + SDK" rows on STA-76.
- **Priority:** Used routinely. Urgent (1) for gate-blocking or production symptoms, High (2) for real defects and design items.
- **Issue shape:** Every well-formed issue follows the same pattern:
  1. Observed or Context.
  2. Why it matters.
  3. Direction or Decision, with date and "(Anders)".
  4. A "Done when" checklist.
  5. A Coordination section: files owned, parallel sessions, the release row on STA-76.
- **Other conventions:**
  - Comments start with `[By Claude]`.
  - Branch names come from `gitBranchName` (`andhus/sta-N-…`).
  - A release PR on a matching branch auto-closes its issue, which has happened to STA-76 twice.
