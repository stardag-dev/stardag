# Registry v2: implementation plan and status

This plan is updated in the same PR as the code it describes, so its
history is the plan's history: the status section below and each work
package's PR list move together with the commits that make them true.

It lives on the `v2` branch, alongside the design it implements,
[design.md](design.md); the numbered decisions behind that design, with
their runner-ups, are in [decisions.md](decisions.md); the point-in-time
research it rests on is under [research/](research/).

## How the work is organised

- **A long-lived `v2` feature branch in `stardag-dev/stardag`**, cut from
  `main`, receiving incremental PRs (each with a Copilot review), merged to
  `main` once when the line ships. Named `v2` rather than `server-v2`
  because the branch carries SDK, CLI and UI too; trivial to rename.
  `ci.yml` triggers on `pull_request: branches: "**"`, so PRs against `v2`
  get full CI already; publishing is tag-triggered and unaffected;
  `docs.yml` deploys from `main` only (correct). The repository's Copilot
  review ruleset covers only the default branch, so its ref list must gain
  `refs/heads/v2`, or every PR on the branch needs the manual REST
  re-request. That ruleset patch is an outward-facing repo-settings change
  and waits for the maintainer's go-ahead (see "Delivery steps" below). CI
  constraints on the branch (e.g. the registry-live tier while the harness
  is being ported) can be loosened in `ci.yml` on the branch itself and
  restored before the merge to `main`.
- **The plan lives in the public repo, next to the design**, so plan
  changes travel in the same PRs as the code and are visible as commits:
  `docs/design/registry-v2/design.md` (the design) and
  `docs/design/registry-v2/plan.md` (this file: work packages, sequencing,
  status). Both are free of private detail by construction: they cite
  STA-\* ids (allowed) and nothing else private. No other plan document
  exists for this effort.
- **A handful of Linear issues carry high-level scope only** and link this
  file; status lives here and in the PR list, not in long Linear comments.
  Mapping is in "Linear issues" below.

## Methodology

Three rules for how the line is built, agreed with the maintainer on
2026-09-24.

### A vertical spike first, then increments complete only for what they touch

`v2` never reaches `main` until the line ships, so a PR against it does not
have to be feature-complete; it has to keep the branch's own CI green for
what it touches. CLI, UI and docs are separate work packages that trail the
server and SDK. Before the surfaces are built out, **I0 proves the design end
to end** with the thinnest slice that exercises every new entity: the
migration, the registration service and frontier for the static path, the
reactive Modal worker path, and one registry-live scenario. v1 code paths it
replaces are deleted, not kept alive. I0 is reviewed like any other PR and
its status entry lists what it skipped, so the spike does not quietly become
the product.

Two mechanics keep incremental PRs honest on the branch:

- A test whose mechanism has not landed yet is marked
  `xfail(reason="v2: I<n>")`, never deleted; the marks are removed by the
  work package they name, and a PR that leaves a mark behind says so.
- The in-memory fake registry is updated in the same PR as the server seam
  it fakes ("fakes follow the server's seams"), so SDK tests never pass
  against a fake the server would refuse.

### Tests: what must still hold, and what is written before the code

**Must still hold.** These registry-live scenarios encode promises that v2
keeps unchanged. Their assertions stay; only route names and
`build_config` → `settings` move. A PR may not weaken or delete them, and
they are the first thing I0 re-points:

`test_reactive_e2e`, `test_claim_race`, `test_cross_build_wake`,
`test_wake_storm`, `test_limit_slot_wake`, `test_suspended_blocker`,
`test_failed_blocker`, `test_watchdog_sweep`, `test_wide_fan_out`,
`test_scheduler_lease_live` (four tests), `test_cancel_authority`,
`test_shared_structure_scope`, `test_rollover`, `test_execution_identity`
(three tests), `test_interruption_classification`, `test_builds_stop`.

**Dies with its mechanism.** `test_structure_scope_static` and
`test_structure_scope_dynamic` test `dependencies_only` through
`build_config`; they are replaced by S8, S22 and S24. Every unit test of
`build_config.py`, the ContextVar transport, the scope-key parser and the
`/locks` routes goes with the code it tests.

**Canonical tests, written before the code.** The scenario table in
`design.md` names a test tier for each of S1–S39: `unit` (SDK, no server),
`api-pg` (the FastAPI suite against Postgres, which becomes the default),
`resident` (the in-process engines with the fake registry), `live` (the
registry-live tier). The `api-pg` tests for the registration and transition
invariants — S2, S4, S10–S12, S15–S19, S23, S28–S32, S34–S36, S38, S39 — are
written **before** the service they pin, as the maintainer's rule "write the
invariant before the code" asks; the PR that adds the service turns them from
red to green. "Passes under both v1 and v2" is realistic only for the
must-still-hold list at the live tier, because they drive the system through
the SDK; nothing else is expected to be portable.

### Engineering rules for the v2 line

Few, specific to the shapes of v1 debt the research notes recorded, and
checkable in review:

1. A route is a thin wrapper; logic lives in a service. v1's
   `routes/builds.py` reached 5,142 lines with its registration transaction
   written twice.
2. One writer per fact: one `transition_task()` for every task event, one
   registration path for static, dynamic and closure admission.
3. A module may not cross 800 lines without being split in the same PR.
4. A fact the queries depend on is a typed column, never a JSON flag
   (`report_applied` in v1 metadata).
5. The Postgres suite is the default for API tests; SQLite-only behaviour is
   not evidence.
6. No compatibility shims, version gates or dual-writing in v2 code.
7. A fake per server seam, changed in the same PR as the seam.

### Signalling scope to reviewers, Copilot included

Copilot reviews every PR against `v2` (the ruleset covers the branch) and,
without context, flags every missing surface as a defect. Two standing
mechanisms set the expectation:

- **`.github/copilot-instructions.md`** carries the branch conventions:
  what "Not in this PR, by design" means, that v1 → v2 has no compatibility
  layer, that `research/` is unmaintained, what `xfail(reason="v2: …")`
  marks are, and what _is_ worth flagging (a contradiction with `design.md`,
  an unenforced invariant, a touched scenario without its test, a weakened
  must-still-hold test, a broken engineering rule).
- **`.github/PULL_REQUEST_TEMPLATE/v2.md`**, used with
  `gh pr create --base v2 --template v2.md`, gives every PR the same four
  sections: **In this PR**, **Not in this PR, by design** (with the work
  package each deferral belongs to), **Design references** (sections,
  scenarios with tiers, decisions relied on) and a checklist. A reviewer,
  human or not, who finds a gap looks there first; a finding about a listed
  deferral is answered by pointing at the line.

Findings that survive that filter are addressed on their merit, and a
disagreement is written down on the thread and, when it is about the design,
in `decisions.md`.

## Work packages

Server first. Each row's status and PR list are tracked in the Status
section below; Linear issues (next section) group them.

| #   | Surface              | Issue                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Blocked by                         |
| --- | -------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| I0  | server + SDK + tests | **Vertical spike**: the migration, registration service and frontier for the static path only, the reactive Modal worker path, the fake registry re-pointed, the must-still-hold scenarios re-pointed and one of them green (`test_reactive_e2e`); v1 paths it replaces deleted; everything else `xfail(reason="v2: I<n>")`. Its status entry lists what it skipped.                                                                                                                                                                                                                                                                           | design merged into `v2`            |
| I1  | server               | **v2 schema**: models + one migration that drops the v1 core tables (group (a)) and creates `task`, `deployment`, `settings`, `task_instance`, `task_instance_dependency`, `plan`, `plan_member`, `execution`, re-pointed `event`, `task_artifact`, `task_limit_key`; drops `distributed_locks`; native enums/CHECKs; Postgres test suite default (STA-72 item)                                                                                                                                                                                                                                                                                | design approved                    |
| I2  | server               | **Registration service** (`services/registration.py`): plans lookup-or-create, chunked `POST /plans/{id}/members`, `/seal`, `/members/{task_id}/yield`, `instance_conflict`, the flag, idempotency tests (STA-48/51/54 patterns), scope-consistency of edges                                                                                                                                                                                                                                                                                                                                                                                   | I1                                 |
| I3  | server               | **Frontier and transitions**: closure step + runnable/discovery-job/running queries over `plan_member`; skip-blocked and exclusion cascade; one `transition_task()` with the authority rule for every event, one terminal report per execution, lapsed-claim takeover; `execution` ledger writes (two ends); observation-driven invalidation (`observed_at` guard, no operator route); `plan_complete` recomputed in `/complete`                                                                                                                                                                                                               | I1                                 |
| I4  | server               | **Deployments, builds, wake-ups, reads**: client-minted deployments with `kind`; `settings`; build lifecycle releasing claims on `complete`, `fail` and `cancel` (`exit-early` releases nothing); wake-ups over membership; `builds stop`/orphans over executions; read routes (instances, plans, executions, graph over instance edges); delete every v1 compat shim; `/api/v2` prefix, `/api/v1` removed                                                                                                                                                                                                                                     | I1                                 |
| I5  | server               | **Extract what stays** from `routes/builds.py` into services (the ~50% kept), so the v2 file is not 5,000 lines (absorbs STA-71)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | I2–I4                              |
| I6  | SDK core             | `StardagField(significant)`; two hashes on `BaseTask`; remove `significance`, `hash_exclude` and `build_config.py` (`compat_default` stays, D12), the ContextVar transport, `RegistryTooOldError` gate; instance body dump and rehydration; nested-model `significant`; discovery with `InstanceConflictError`; the instance hash as the hash of the canonical body, sets sorted in the body dump, the round-trip stability check (`UnstableSerializationError`) at registration; the instance/task-object vocabulary in the docstrings of `BaseTask`, `task_from_registry_data` and the registry client; `task_uuid5_namespace_provider` kept | design approved (parallel with I1) |
| I7  | SDK engines + Modal  | v2 registry client; plan-based registration (chunks + seal) and `/yield` in both engines, same order; tick: discovery jobs, deployment-id rollover, `superseded`; executor passes `STARDAG_PLAN_ID` + `settings` env, drops `STARDAG_BUILD_CONFIG`/`STARDAG_SCOPE_KEY`; `stardag modal deploy` mints and bakes `STARDAG_DEPLOYMENT_ID`, creates before and activates after the deploy; local deployments lookup-or-create; hybrid drivers and `reactive_discovery="local"` plan under the app's current deployment (D13)                                                                                                                       | I2–I4, I6                          |
| I8  | CLI                  | `stardag build` (STA-70: roots from `module:attr`, `--settings KEY=VALUE`, `--app`); `builds stop --not-in-current-plan`; `executions list`; `deployments list` with `kind`; `tasks check` (runs `complete()` locally and reports the observation); `plans show`; `tasks show` surfaces `TASK_STRUCTURE_DIVERGED`                                                                                                                                                                                                                                                                                                                              | I4, I7                             |
| I9  | UI                   | Task instance page + task (claim) panel listing instances; plan per build over instance edges; deployments page; executions per task; settings in build info; `TASK_STRUCTURE_DIVERGED` shown on the task detail, not buried in the event log; delete scope code, `is_phantom`, `blocked_by_external`; `stoppable.ts` mirrors the CLI over executions                                                                                                                                                                                                                                                                                          | I4                                 |
| I10 | tests                | Registry-live scenarios for S1–S20 where a live worker matters (S1, S3, S4, S5, S7, S8, S11, S13, S14, S19, S20 at least); harness: deploy records `kind`, `rollover_app.py` under two deployment ids; unit/Postgres tests for the rest                                                                                                                                                                                                                                                                                                                                                                                                        | I7                                 |
| I11 | docs                 | Rewrite `concepts/parameters.md`, `concepts/build-execution.md`, `concepts/modal-orchestration.md` (deployments), `how-to/evolve-dags.md`, `how-to/integrate-modal.md`, `platform/api.md`; minor pages; `docs/design/README.md`; supersede banner on SKDS; `principles.md` (STA-82) written against v2 entities; DEV_README scenario table                                                                                                                                                                                                                                                                                                     | I6–I9                              |
| I12 | release              | v2 release line: versioning (the maintainer's call: SDK 1.0.0 / server 1.0.0?), CHANGELOG/RELEASE_NOTES, server image → prod deploy (with approval) → SDK tag; "existing registries start empty" runbook for the hosted deployment; upgrade-together policy                                                                                                                                                                                                                                                                                                                                                                                    | all                                |

Sequencing: I0 → I1 → (I2, I3, I4 in parallel, one session each, files disjoint)
→ I5; I6 in parallel with I1; I7 after I2–I4 + I6; I8/I9/I10 after I7/I4;
I11 trails; I12 last. Registry-live is the merge gate throughout. Every PR
targets the `v2` branch and updates this file in the same PR.

## Linear issues

Each description: two paragraphs of scope, a "Done when", and a link to
`plan.md` on the `v2` branch. No plan detail in Linear; status is read from
`plan.md` and the PR list. Created on approval, Backlog, team Stardag,
assignee the maintainer.

| Issue                         | Scope                                                                                                                | Work packages |
| ----------------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------- |
| STA-105                       | Umbrella: design approved, `v2` branch and PRs, decisions log; closes when v2 ships                                  | design PRs    |
| STA-106 (v2 server)           | Schema, registration service, frontier + transitions + ledger, deployments/builds/wake-ups/reads, service extraction | I1–I5         |
| STA-107 (v2 SDK)              | Two hashes and `significant`, engines and Modal integration, registry client, deploy CLI                             | I6–I7         |
| STA-108 (v2 CLI)              | `stardag build`, stop filter, executions/plans/deployments commands, `tasks check`                                   | I8            |
| STA-109 (v2 UI)               | Instances, claim panel, plan view, deployments, executions; delete scope code                                        | I9            |
| STA-110 (v2 live scenarios)   | Registry-live scenarios S1–S20 and harness changes                                                                   | I10           |
| STA-111 (v2 docs and release) | Docs rewrite, principles.md, release line and cut-over                                                               | I11–I12       |

`blockedBy` relations follow the sequencing above.

## Adjacent issues

- Close as subsumed, with a comment pointing at STA-105 and the note:
  STA-95. (STA-43/41/42/60 are already closed; nothing to do.)
- Cancel as dissolved by v2 when I1–I4 land: STA-71 (absorbed by I5),
  STA-63 (C is v2; B is folded into I2 as a request-boundary retry, kept
  because claim arbitration still serialises on `task`), STA-77 (already
  Done; its nested-model test moves to I6).
- Re-scope onto v2 (edit description, move to P-STA-1): STA-74 (invariants
  above + two live scenarios, into I10), STA-70 (→ I8), STA-54 (→ I2's
  idempotency tests), STA-73 (canary under v2 rollover), STA-82 (principles
  against v2, → I11), STA-68 (retention of instances/edges per deployment),
  STA-64 (infra knobs are not `settings`; note it), STA-61, STA-39.
  STA-67: leave Canceled; the orphan filter is I8 (STA-80's filter).
  STA-58: decide against (structure is not in the completion hash) and close.
- Continue on v1 unchanged (they land before v2 or their rule carries
  over): STA-104, 100, 103, 99, 94, 93, 96, 90, 84, 75, 76, 85, 78. Note on
  each that v2's `transition_task()` (I3) is where the rule must be
  re-implemented, so the v1 fix is written as a rule, not a patch.
- STA-72: strike the moot items when I4 lands; the surviving items are in
  I1 (Postgres suite), I4 (`deployed_at` by API clock, `graph.py`), I9 (UI).
- STA-78: add a comment recording v2 as the explicit exception to decision
  3 (D10), phases 2–3 continue on v1.
- STA-62: propose closing as superseded by STA-76 (stale).

## Status

- [x] Design approved for review (PR against v2)
- [x] I0 — Vertical spike: done. Step 1 (schema, deletion, Postgres
      default); step 2 (registration, frontier and transitions for the
      static path, under `/api/v2`), PR #380; step 3a (build lifecycle,
      deployments, settings, wake-ups, notify, scheduler lease, reactive
      meta, tick summaries), PR #382; step 3b (`/yield`; interrupt,
      preempt, skip, a single task's cancel; skip-blocked and the exclusion
      cascade; `builds stop`, orphans, rate limit, creation quota), PR
      #383; step 3c (wake-up flags on `build_wake`; the read routes the SDK
      calls; attempt counts on the frontier; `/activate` records
      `modal_app_id` and `image_id`), PR #385; step 4 (the live proof), PR
      #386: the registry-live harness on `/api/v2`, `test_reactive_e2e`
      and the must-still-hold list green against a provisioned v2
      registry, the `modal_live` lib tier green on Modal. Step 4 added the
      reads and routes the live tier needs (`GET /tasks/{id}/events`,
      `GET /builds/{id}/events`, `GET /builds/{id}/executions?include_ended`,
      `PUT/GET/DELETE /concurrency-limits/{key}`) and fixed what the live
      run found (below).

      | Scenario (live)                    | Outcome                        |
      | ---------------------------------- | ------------------------------ |
      | `test_reactive_e2e`                | green                          |
      | `test_claim_race`                  | green                          |
      | `test_cross_build_wake`            | green                          |
      | `test_wake_storm`                  | green                          |
      | `test_limit_slot_wake`             | green                          |
      | `test_suspended_blocker`           | green                          |
      | `test_failed_blocker`              | green                          |
      | `test_watchdog_sweep`              | green                          |
      | `test_wide_fan_out`                | green                          |
      | `test_scheduler_lease_live` (4)    | green                          |
      | `test_cancel_authority`            | green                          |
      | `test_shared_structure_scope`      | green                          |
      | `test_rollover`                    | green                          |
      | `test_execution_identity` (3)      | green                          |
      | `test_interruption_classification` | green                          |
      | `test_builds_stop`                 | skipped, `v2: I8` (CLI)        |
      | `test_structure_scope_static/_dyn` | deleted (S8/S22/S24, I10)      |

      Skipped by I0, owned elsewhere: `stardag builds stop` over executions
      and every other CLI surface (I8; the routes are served); the UI (I9);
      the graph over instance edges as a read route (I4 — `test_rollover`
      asserts the new scope through the parent's instances instead); the
      registry-live scenarios for S1–S20 (I10); docs (I11). The live tier
      was run serially per scenario on a developer stack, not as CI's
      concurrent tier; the CI run is the merge gate.

      What the live run found, by layer. SDK: the lease release dropped
      the server's `held` answer (`scheduler_lease_release` now returns it;
      unit tests on client and fake); `stardag modal deploy` never sent
      `modal_app_id` on `/activate` (CLI and client tests). Server: none of
      the step-3 rules failed live; the gaps were reads and routes the
      tier depends on (above, `api-pg` tests). Harness and tests: v1 route
      and model assumptions throughout, the lib live test's crashed-plan
      setup (a claiming start re-checks upstreams, S39; an observation is
      refused against a live claim, so the holder's completion is relayed),
      and the live tier was never type-checked (the pyright hook now covers
      `tests_registry_live`). Carried from #384's review: the local
      deployment id is client-minted, the bootstrap is one build per
      process, and the tick re-checks its lease after discovery and after
      the executor-metadata await.

- [ ] I1 — v2 schema
- [ ] I2 — Registration service
- [ ] I3 — Frontier and transitions
- [ ] I4 — Deployments, builds, wake-ups, reads
- [ ] I5 — Extract what stays
- [ ] I6 — SDK core (in progress — hashing and field layer done; I7 pending)
- [ ] I7 — SDK engines + Modal (PR #384 merged). The client, both
      engines, the tick, the worker and `stardag modal deploy` run on
      `/api/v2`; `build_config.py` is gone and `settings` replaces it. Every
      route the client calls is served (step 3c, step 4); proven live in I0
      step 4. Left to I8: `builds list/stop/cleanup`, `tasks`,
      `concurrency-limits` as CLI commands.
- [ ] I8 — CLI (in review, draft PR against `v2`). `stardag build`
      (roots from `module:attr`, `--settings`, `--app`, `--reactive`,
      `--resume`, `--dry-run`); `builds` list, show, frontier, ticks,
      stop, cancel, complete and fail, `stop` over the execution ledger with
      `--not-in-current-plan`; `executions list`; `plans show`;
      `deployments list` (`stardag modal deployments` is its Modal alias);
      `tasks show/check/retry/cancel/exclude`. Client reads added:
      `build_list`, `plan_roots_info`, `task_list_artifacts`, all on served
      routes. Open server-contract items: no `GET /plans/{id}` (timestamps
      and member counts are known only for the active plan, via the
      frontier), no event read (`tasks show` cannot surface
      `TASK_STRUCTURE_DIVERGED`), no route for a bare observation
      (`tasks check --report` is refused).
- [ ] I9 — UI (in review, draft PR #388 against `v2`). Every registry call
      is on `/api/v2`; scope keys, `build_config`, phantoms, external
      blockers and `/locks` are gone. Builds list, the build view over the
      active plan (plan header, members, DAG over instance edges, frontier,
      settings and deployment in build info), the stop list over
      `GET /builds/{id}/executions` with orphans, the task page (claim,
      instances under their scopes, artifacts) and a deployments page.
      Coded against one route the registry does not serve, marked
      **(assumed)** in `api/registry.ts`: `GET /plans/{id}/graph` (members
      and instance edges); until it lands the view shows roots plus the
      frontier and says it is partial. Removed for want of a v2 route: task
      search/explorer, claim triage, bulk cancel, concurrency limits; the
      build failure reason and the task event log (so
      `TASK_STRUCTURE_DIVERGED`) have no field or route to read.
- [ ] I10 — tests (PR #389, draft). One registry-live module per `live`
      row of the design's scenario table, each run serially green against a
      provisioned v2 registry; S3 stays `test_rollover`. The scenario table
      in `design.md` is unchanged; this is the mapping.

      | Scenario | Test                                                   | Outcome |
      | -------- | ------------------------------------------------------ | ------- |
      | S1       | `test_s1_scopes_diverge_on_structure`                  | green   |
      | S3       | `test_rollover` (must-still-hold)                      | green   |
      | S5       | `test_s5_deleted_target_is_invalidated`                | green   |
      | S6       | `test_s6_redeploy_without_change`                      | green   |
      | S7       | `test_s7_old_deployment_yield_after_switch`            | green   |
      | S8       | `test_s8_settings_scopes`                              | green   |
      | S14      | `test_s14_resume_under_new_settings`                   | green   |
      | S20      | `test_s20_root_identity_changed_at_rollover`           | green   |
      | S21      | `test_s21_dead_worker_claim_is_taken_over`             | green   |
      | S22      | `test_s22_closure_admits_a_withdrawn_yield`            | green   |
      | S24      | `test_s24_distinct_instances_do_not_share_yields`      | green   |
      | S26      | `test_s26_hybrid_driver_plans_under_the_app`           | green   |
      | S33      | `test_s33_seal_refuses_a_superseded_rollover`          | green   |
      | S37      | `test_s37_deploy_recorded_late`                        | green   |
      | —        | `test_builds_stop` (on `stardag builds stop`, #387)    | green   |

      S8, S22 and S24 replace the deleted `test_structure_scope_static/_dyn`.
      One product defect, SDK layer, found by S5 in CI: a Modal Volume
      mounted into a warm container served a deleted target as present (a
      hit never reloaded), so a bootstrap reusing such a container observed
      it complete and the completion was never invalidated. A walk now
      begins an observation fence (`stardag.target._freshness`), and a
      mounted-volume hit older than it reloads once per volume per walk;
      pinned by unit tests in `test__target_reload.py`, and S5 now runs its
      sticky half first so the invalidating build meets that warm view. The
      other red runs were harness or scenario assumptions (a stale deployed
      image; an S7 expectation of one restart where the design gives two
      executions under the new plan, since the restart yields again into
      its own plan; S6 counting rows from a previous attempt). Three things are synthesised
      and say so in their docstrings: S21's short claim (the dying worker
      renews its own claim down to seconds through `claim/renew`; a real
      detached TTL is timeout + 15 min); S33's D2 tick (the SDK's own
      `roll_over_aio` run in-process under D2's id, D3 deployed for real
      just before its seal — two live ticks of two deployments cannot be
      timed); S37's missing activation (the real CLI with its activation
      step replaced). S21 and S37 recover through a watchdog sweep, by
      design: a lapse or a re-sent record flags nothing, and a lingering
      tick polls the flag, not the frontier.

      Harness: `lapse_app` (a third provisioned app, so a sweep reaches only
      its own builds); the rollover app named per scenario by the deploying
      process (`_rollover.ROLLOVER_APP_NAMES`, collected by the log dump),
      with an optional root variant baked into its image (S20);
      `_targets.delete_target`; ledger helpers attributing an execution to
      a build. CI: the full tier green in one attempt (33 passed, the
      `builds stop` skip) in 10m47s; an earlier attempt under contention took
      ~14 min. The long poles are S7 and
      `test_rollover`, each paying several pre-yield windows sized to a
      deploy. Nothing sleeps; shrinking those windows is the lever.
      The docker-compose e2e tier (`integration-tests/tests`) is re-pointed to v2 in PR #391 (merged).

- [ ] I11 — docs. Principles and release notes drafted, in review (PR
      #392): `docs/design/principles.md`, the v2 entries in `CHANGELOG.md`
      and `RELEASE_NOTES.md`; versioning TODO(Anders). The user docs under
      `docs/docs/` are the other half, on a separate branch.
- [ ] I12 — release

## Delivery steps

1. **Branch `v2`** from `origin/main` in a separate worktree, one commit
   adding `docs/design/registry-v2/plan.md` and `design.md` as
   placeholders (title, one-line purpose, "filled in by the STA-105 design
   PR") plus the `docs/design/README.md` row. Push; open a **Draft PR
   `v2` → `main`** titled "Registry v2: core entities re-design (STA-105)"
   whose body states the line's scope and that it merges once when v2
   ships. (done: PR #378)
2. **Rebase the sta-105 branch onto `v2`** (it has no commits yet) and
   write the review set under `docs/design/registry-v2/`, so every part
   can be commented on in the PR (the maintainer's ask, 2026-09-23):

   - `design.md` — the design document, 79-char wrap.
   - `plan.md` — work packages, sequencing, Linear mapping, status
     checkboxes, delivery steps.
   - `decisions.md` — D1–D13 with recommendation and runner-up, plus the
     adversarial review's dispositions (accepted / rejected with reason).
   - `research/README.md` and one file per verification report:
     `v1-schema.md`, `v1-server-logic.md`, `v1-sdk.md`,
     `v1-design-notes-and-invariants.md`, `v1-ui.md`,
     `adjacent-issues.md` (Linear state as of 2026-09-23),
     `review-2026-09-23.md` (the thirty findings verbatim). Each carries a
     header: date, source commit, "point-in-time notes, not maintained".
     Before committing, each is swept for anything not publishable:
     absolute local paths made repo-relative, no private paths or
     task-record names, no deployment/customer specifics (the source
     material summarised private detail; keep only its reasoning). STA-\*
     ids stay.
   - The "Superseded by" banner on `scope-keyed-dependency-structure.md`
     and the `docs/design/README.md` row.

   Pre-commit hooks on the changed files; commit; push; **PR sta-105 →
   `v2`** titled "Design: registry v2 core entities". Request the Copilot
   review by the REST call (the ruleset does not cover base `v2`); propose
   the ruleset patch (`conditions.ref_name.include += refs/heads/v2`) for
   the maintainer's go-ahead so later PRs get it automatically.

3. **Update STA-105**: description gains a "Design and plan" section
   linking both PRs and the paths; a `[By Claude]` comment records the
   corrections to the 2026-09-23 summary (STA-69/50/65 Done, STA-67
   Canceled, no executions row in v1, `env_overrides` is the
   worker-selector env, §4.1 has no window today but three neighbours do,
   §4.2 fails the other way, no `family--` handle exists) and the
   decisions D1–D10 awaiting the maintainer's read; move STA-105 to In
   Progress.
4. **After the maintainer has reviewed the design PR** (comments
   addressed, PR merged into `v2`): create the six Linear issues under
   P-STA-1 with blockers, apply the adjacent-issue actions (comments
   prefixed `[By Claude]`), close STA-95 as subsumed, comment on STA-78
   recording the exception (D10). Not before: the issue split can move
   with the review.
5. Implementation starts only on the maintainer's explicit go for the
   first server PR (I1), in a fresh worktree per work package, all PRs
   against `v2`.

## Verification of the deliverable

- Each of S1–S20 names a deciding column/constraint that exists in the
  schema section (checked by reading the note end to end).
- Each claim in the note was verified against the source by the agent
  reports under [research/](research/); the two starting-summary claims
  that the code contradicted (§4.1 window exists today; §4.2
  false-runnable) are stated with their corrected form.
- No private detail in the note or PR: grep the note for workspace,
  customer, account and Linear-internal-only strings before pushing
  (Linear ids are allowed).
- The PR carries the note only; no code.
