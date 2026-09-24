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
- [ ] I0 — Vertical spike: in progress — step 1 (schema, deletion, Postgres
      default) done; step 2 (registration, frontier and transitions for the
      static path, server side, under `/api/v2`) done, PR #380; step 3a
      (build lifecycle, deployments, settings, wake-ups, notify, scheduler
      lease, reactive meta, tick summaries; the authority refinement and
      closure-first seal) done, PR #382; step 3b (`/yield`, the remaining
      transitions, skip-blocked, the exclusion cascade, `builds stop` and
      orphans) pending; step 4 (worker path; live scenario) pending.
      (skipped so far: skip-blocked and the exclusion cascade,
      `xfail("v2: I3")`)
- [ ] I1 — v2 schema
- [ ] I2 — Registration service
- [ ] I3 — Frontier and transitions
- [ ] I4 — Deployments, builds, wake-ups, reads
- [ ] I5 — Extract what stays
- [ ] I6 — SDK core (in progress — hashing and field layer done; I7 pending)
- [ ] I7 — SDK engines + Modal (in review, draft PR against `v2`). The
      client, both engines, the tick, the worker and `stardag modal deploy`
      run on `/api/v2`, against the step-3b routes; `build_config.py` is
      gone and `settings` replaces it. Coded against routes the registry
      does not serve yet, each marked **(assumed)** in
      `registry/_api_routes.py` / `_api_registry.py`: `GET /builds`
      (running, by reactive app: the watchdog), `GET /plans/{id}/roots`
      (root bodies for rollover), `GET /tasks/{id}` (`from_registry`),
      `POST /tasks/{id}/artifacts`. Left to I8: `builds list/stop/cleanup`,
      `tasks`, `concurrency-limits`. The live tier is I0 step 4.
- [ ] I8 — CLI
- [ ] I9 — UI
- [ ] I10 — tests
- [ ] I11 — docs
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
