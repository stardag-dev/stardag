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

## Work packages

Server first. Each row's status and PR list are tracked in the Status
section below; Linear issues (next section) group them.

| #   | Surface             | Issue                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          | Blocked by                         |
| --- | ------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------- |
| I1  | server              | **v2 schema**: models + one migration that drops the v1 core tables (group (a)) and creates `task`, `deployment`, `settings`, `task_instance`, `task_instance_dependency`, `plan`, `plan_member`, `execution`, re-pointed `event`, `task_artifact`, `task_limit_key`; drops `distributed_locks`; native enums/CHECKs; Postgres test suite default (STA-72 item)                                                                                                                                                                                                                                                                                | design approved                    |
| I2  | server              | **Registration service** (`services/registration.py`): plans lookup-or-create, chunked `POST /plans/{id}/members`, `/seal`, `/members/{task_id}/yield`, `instance_conflict`, the flag, idempotency tests (STA-48/51/54 patterns), scope-consistency of edges                                                                                                                                                                                                                                                                                                                                                                                   | I1                                 |
| I3  | server              | **Frontier and transitions**: closure step + runnable/discovery-job/running queries over `plan_member`; skip-blocked and exclusion cascade; one `transition_task()` with the authority rule for every event, one terminal report per execution, lapsed-claim takeover; `execution` ledger writes (two ends); observation-driven invalidation (`observed_at` guard, no operator route); `plan_complete` recomputed in `/complete`                                                                                                                                                                                                               | I1                                 |
| I4  | server              | **Deployments, builds, wake-ups, reads**: client-minted deployments with `kind`; `settings`; build lifecycle releasing claims on all terminal transitions; wake-ups over membership; `builds stop`/orphans over executions; read routes (instances, plans, executions, graph over instance edges); delete every v1 compat shim; `/api/v2` prefix, `/api/v1` removed                                                                                                                                                                                                                                                                            | I1                                 |
| I5  | server              | **Extract what stays** from `routes/builds.py` into services (the ~50% kept), so the v2 file is not 5,000 lines (absorbs STA-71)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               | I2–I4                              |
| I6  | SDK core            | `StardagField(significant)`; two hashes on `BaseTask`; remove `significance`, `hash_exclude` and `build_config.py` (`compat_default` stays, D12), the ContextVar transport, `RegistryTooOldError` gate; instance body dump and rehydration; nested-model `significant`; discovery with `InstanceConflictError`; the instance hash as the hash of the canonical body, sets sorted in the body dump, the round-trip stability check (`UnstableSerializationError`) at registration; the instance/task-object vocabulary in the docstrings of `BaseTask`, `task_from_registry_data` and the registry client; `task_uuid5_namespace_provider` kept | design approved (parallel with I1) |
| I7  | SDK engines + Modal | v2 registry client; plan-based registration (chunks + seal) and `/yield` in both engines, same order; tick: discovery jobs, deployment-id rollover, `superseded`; executor passes `STARDAG_PLAN_ID` + `settings` env, drops `STARDAG_BUILD_CONFIG`/`STARDAG_SCOPE_KEY`; `stardag modal deploy` mints and bakes `STARDAG_DEPLOYMENT_ID`, creates before and activates after the deploy; local deployments lookup-or-create; hybrid drivers and `reactive_discovery="local"` plan under the app's current deployment (D13)                                                                                                                       | I2–I4, I6                          |
| I8  | CLI                 | `stardag build` (STA-70: roots from `module:attr`, `--settings KEY=VALUE`, `--app`); `builds stop --not-in-current-plan`; `executions list`; `deployments list` with `kind`; `tasks check` (runs `complete()` locally and reports the observation); `plans show`; `tasks show` surfaces `TASK_STRUCTURE_DIVERGED`                                                                                                                                                                                                                                                                                                                              | I4, I7                             |
| I9  | UI                  | Task instance page + task (claim) panel listing instances; plan per build over instance edges; deployments page; executions per task; settings in build info; `TASK_STRUCTURE_DIVERGED` shown on the task detail, not buried in the event log; delete scope code, `is_phantom`, `blocked_by_external`; `stoppable.ts` mirrors the CLI over executions                                                                                                                                                                                                                                                                                          | I4                                 |
| I10 | tests               | Registry-live scenarios for S1–S20 where a live worker matters (S1, S3, S4, S5, S7, S8, S11, S13, S14, S19, S20 at least); harness: deploy records `kind`, `rollover_app.py` under two deployment ids; unit/Postgres tests for the rest                                                                                                                                                                                                                                                                                                                                                                                                        | I7                                 |
| I11 | docs                | Rewrite `concepts/parameters.md`, `concepts/build-execution.md`, `concepts/modal-orchestration.md` (deployments), `how-to/evolve-dags.md`, `how-to/integrate-modal.md`, `platform/api.md`; minor pages; `docs/design/README.md`; supersede banner on SKDS; `principles.md` (STA-82) written against v2 entities; DEV_README scenario table                                                                                                                                                                                                                                                                                                     | I6–I9                              |
| I12 | release             | v2 release line: versioning (the maintainer's call: SDK 1.0.0 / server 1.0.0?), CHANGELOG/RELEASE_NOTES, server image → prod deploy (with approval) → SDK tag; "existing registries start empty" runbook for the hosted deployment; upgrade-together policy                                                                                                                                                                                                                                                                                                                                                                                    | all                                |

Sequencing: I1 → (I2, I3, I4 in parallel, one session each, files disjoint)
→ I5; I6 in parallel with I1; I7 after I2–I4 + I6; I8/I9/I10 after I7/I4;
I11 trails; I12 last. Registry-live is the merge gate throughout. Every PR
targets the `v2` branch and updates this file in the same PR.

## Linear issues

Each description: two paragraphs of scope, a "Done when", and a link to
`plan.md` on the `v2` branch. No plan detail in Linear; status is read from
`plan.md` and the PR list. Created on approval, Backlog, team Stardag,
assignee the maintainer.

| Issue               | Scope                                                                                                                | Work packages |
| ------------------- | -------------------------------------------------------------------------------------------------------------------- | ------------- |
| STA-105 (exists)    | Umbrella: design approved, `v2` branch and PRs, decisions log; closes when v2 ships                                  | design PRs    |
| v2 server           | Schema, registration service, frontier + transitions + ledger, deployments/builds/wake-ups/reads, service extraction | I1–I5         |
| v2 SDK              | Two hashes and `significant`, engines and Modal integration, registry client, deploy CLI                             | I6–I7         |
| v2 CLI              | `stardag build`, stop filter, executions/plans/deployments commands, `tasks check`                                   | I8            |
| v2 UI               | Instances, claim panel, plan view, deployments, executions; delete scope code                                        | I9            |
| v2 live scenarios   | Registry-live scenarios S1–S20 and harness changes                                                                   | I10           |
| v2 docs and release | Docs rewrite, principles.md, release line and cut-over                                                               | I11–I12       |

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
- [ ] I1 — v2 schema
- [ ] I2 — Registration service
- [ ] I3 — Frontier and transitions
- [ ] I4 — Deployments, builds, wake-ups, reads
- [ ] I5 — Extract what stays
- [ ] I6 — SDK core
- [ ] I7 — SDK engines + Modal
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
