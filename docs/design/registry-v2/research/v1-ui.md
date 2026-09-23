# Stardag UI sizing for the v2 registry entities (read-only survey)

> Point-in-time research note, 2026-09-23, against `stardag-dev/stardag` at
> commit `f96fb751` (then `main`). Written by a Claude agent for the STA-105
> design; verified against the source at that commit. Not maintained: read
> [../design.md](../design.md) for the design and [../plan.md](../plan.md)
> for current status.

Worktree: `stardag-worktrees/sta-105` at `f96fb751` (== `origin/main`, includes #376).
All paths relative to `app/stardag-ui/src/`. Non-test src ~21.9k LOC; tests ~5.7k LOC.

## 1. Pages / routes and major components

Routing is hand-rolled in `App.tsx:573-645` (pathname regex, no router lib):
`/settings`, `[/:ws[/:env]]/tasks`, `.../limits`, `.../builds/:id`, default = builds list.

| Component                                                                                                                      | Shows                                                                                                                      |
| ------------------------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| `BuildsList.tsx` (761)                                                                                                         | Builds list: status filter, idle filter on `last_activity_at`, reactive app, bulk cancel entry                             |
| `BulkCancelDialog.tsx` (329)                                                                                                   | Dry-run-then-confirm bulk build cancel                                                                                     |
| `BuildView.tsx` (858)                                                                                                          | One build: task table + DAG + task detail pane; claim-held count (`:379-414`)                                              |
| `BuildInfoDialog.tsx` (257)                                                                                                    | Build facts: id, executor/reactive app, **structure scope**, commit, times, **build_config**                               |
| `BuildControlsDialog.tsx` (745)                                                                                                | "Stop" (lists running executions = `stardag builds stop` preview) + override section                                       |
| `BuildOverrideSection.tsx` (272)                                                                                               | Record outcome: mark completed / failed / cancel (build status overrides — _not_ env_overrides)                            |
| `BuildSchedulingPanel.tsx` (880)                                                                                               | Frontier (roots, actionable, running, status counts, external blockers, needs_tick)                                        |
| `TickSummaryTrail.tsx` (364)                                                                                                   | Reactive tick summaries (outcome + open counter dict)                                                                      |
| `BuildFailureReason.tsx`, `BuildStatusBadge.tsx`                                                                               | Build failure text, build status pill                                                                                      |
| `TaskTable.tsx` (169), `TaskFilters.tsx`                                                                                       | Tasks in a build                                                                                                           |
| `DagGraph.tsx` (546), `TaskNode`, `BatchNode`, `DagControls`, `dagLayout.ts`                                                   | Dependency graph (build or task-centred), grouped nodes, dynamic / cross-scope edges                                       |
| `TaskDetail.tsx` (958)                                                                                                         | Task detail: status, claim holder + release/reset, Modal execution details, artifacts, parameters (`task_data`), event log |
| `ArtifactViewer.tsx` (136)                                                                                                     | Markdown/JSON artifacts                                                                                                    |
| `TaskExplorer.tsx` (1167) + `TaskExplorerTable` (479), `TaskExplorerSearch` (241), `ColumnManagerModal` (502)                  | Env-wide task search with `param.*` / `artifact.*` columns, DAG mode, claim-triage mode                                    |
| `ClaimTriage.tsx` (580), `ClaimActionDialog.tsx` (105)                                                                         | Tasks holding claims, oldest first; release/reset                                                                          |
| `ConcurrencyLimits.tsx` (658)                                                                                                  | Concurrency limits admin + slot holders + evict                                                                            |
| `ExecutorBadge.tsx` (125), `utils/modalLinks.ts` (161)                                                                         | Executor chips and Modal dashboard links                                                                                   |
| `WorkspaceSettings` (1126), `CreateWorkspace`, `WorkspaceSelector`, `EnvironmentSelector`, `PendingInvites`, `OnboardingModal` | Workspace/env/member/invite/API-key/target-root admin                                                                      |
| `LandingPageDemo`, `CodeExampleTabs`                                                                                           | Static marketing demo (fixture `task_data: {}` at `LandingPageDemo.tsx:182`)                                               |

**No deployments view exists.** `api/deployments.ts` (`listDeployments`) has zero callers.
No executions view exists either (see 3).

## 2. API endpoints the UI calls

Registry (`api/tasks.ts`, `API_V1`):

| Method + path                                                                                       | Used by                                                    |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------- |
| GET `/builds`                                                                                       | BuildsList                                                 |
| GET `/builds/{id}`                                                                                  | BuildView                                                  |
| GET `/builds/{id}/frontier`                                                                         | BuildSchedulingPanel                                       |
| GET `/builds/{id}/tick-summaries`                                                                   | BuildSchedulingPanel -> TickSummaryTrail                   |
| GET `/builds/{id}/tasks`                                                                            | BuildView                                                  |
| GET `/builds/{id}/graph`                                                                            | BuildView (DagGraph)                                       |
| POST `/tasks/graph`                                                                                 | TaskExplorer DAG mode, `hooks/useTasks.ts`                 |
| GET `/tasks` (status / status_older_than filters)                                                   | ClaimTriage, BuildControlsDialog `:172` (stop list)        |
| GET `/tasks/{task_id}`                                                                              | TaskExplorer deep link, ConcurrencyLimits holder -> detail |
| GET `/tasks/{task_id}/artifacts`, `/events`                                                         | TaskDetail `:470`, `:392`                                  |
| GET `/tasks/search`, `/search/keys`, `/search/values`, `/search/columns`                            | TaskExplorer `:469/288/714`, `fetchAvailableColumns`       |
| POST `/builds/{id}/cancel`, `/builds/bulk-cancel`, `/complete`, `/fail`                             | BuildOverrideSection, BulkCancelDialog                     |
| POST `/builds/{b}/tasks/{t}/cancel`, `/retry`                                                       | TaskDetail claim actions, ClaimTriage                      |
| GET `/deployments`                                                                                  | **nobody** (`api/deployments.ts`)                          |
| GET/PUT/DELETE `/concurrency-limits[/{key}]`, GET `.../holders`, POST `.../holders/{task_id}/evict` | ConcurrencyLimits                                          |
| GET `/version`                                                                                      | ServerVersionFooter                                        |

UI/admin (`API_V1_UI`, `api/workspaces.ts`, `api/auth.ts`): `/me`, `/me/invites`,
`/workspaces[/{id}]` CRUD, `/environments`, `/members`, `/invites` (+accept/decline),
`/api-keys`, `/target-roots`, `/auth/exchange`, `/auth/change-password`. Unaffected by v2.

## 3. v1 concepts the UI depends on

Counts are non-test files; LOC is the code that reads the concept, roughly.

**`task_data`** — small, but semantically loaded. 7 files, ~12 refs.

- `TaskDetail.tsx:783,823` JSON dump as "Task Parameters".
- `TaskExplorerTable.tsx:131-145` resolves `param.<path>` columns by walking `task_data`.
- Pass-through copies: `BuildView.tsx:454`, `TaskExplorer.tsx:603`, `hooks/useTasks.ts:155`.
- The real dependency is server-side: `/tasks/search/keys|values` + `filter=param.x ...` search over `task_data`. If v2 makes `task_data` the hash-mode dump (only significance-1 params) vs an all-parameter instance body, the param columns/filters change meaning. UI edit ~30 LOC; decide which body backs `param.*`.

**`task_id`** — pervasive, the biggest decision. ~13 files, ~80 refs.

- It is the path key for `/tasks/{id}`, `/artifacts`, `/events`, `/builds/{b}/tasks/{t}/cancel|retry`, concurrency holder evict; the URL deep-link key (`TaskExplorer.tsx:184`); the React key/graph node id; `utils/ids.ts` `shortTaskId` documents it as a UUID5 content hash.
- If v2 splits instance id (all params) from claim/completion hash, every call site must pick one: detail/artifacts/params -> instance; claim/cancel/retry/evict/holders -> claim hash. Mostly mechanical once the API decides, but touches ~all task components. ~150-250 LOC plus tests.

**`latest_status_scope_key` / structure scope** — self-contained, mostly deletable. ~100 LOC.

- `utils/scope.ts` (13, `isSyntheticScope`), `utils/graphEdges.ts` (17, edge id includes `scope_key`), `BuildInfoDialog.tsx:75,211-237` `ScopeField`, `DagGraph.tsx:380-390` cross-scope edge style/tooltip, `types/task.ts` (`Build.scope_key`, `BuildFrontier.scope_key`, `TaskEdge*.scope_key/is_cross_scope`, `TaskNodeExtended.scope_key`, `TaskEvent.scope_key`). Plus `scope.test.ts`, `graphEdges.test.ts`.
- The UI never reads a `latest_status_scope_key` field by that name; provenance scope arrives as `TaskNodeExtended.scope_key`.

**`build_config` / `env_overrides`** — tiny. `BuildInfoDialog.tsx:239-257` `ConfigField` (JSON dump, hint ties it to the scope) + types. **`env_overrides`: zero hits.** (`BuildOverrideSection`/"override" matches are status overrides, unrelated.) ~25 LOC to repoint at `exec_config`.

**Dependency edges keyed by scope** — `graphEdges.ts`, DagGraph edge building (`:370-400`), `is_dynamic` styling (DagGraph, 1 ref). Graph endpoints' response shape is the contract; if v2 edges hang off the build's plan, DagGraph/dagLayout survive and the scope bits are deleted.

**Code id / deployment handles** — essentially absent. `api/deployments.ts` (30, unused), `Deployment` type (`types/task.ts`), no `code_id` rendering anywhere except the scope string. `reactive_app_name` is shown (BuildsList, BuildInfoDialog, BuildSchedulingPanel, BulkCancelDialog). Net: v2 deployment is new UI, not migration.

**Build status** — `BuildStatus` union (`types/task.ts:13`), BuildStatusBadge, BuildsList status filter/labels (`:43-84`), BuildOverrideSection (complete/fail/cancel), BulkCancelDialog skip reasons, `is_resumed`, `last_active_at` vs `last_activity_at`. Survives if v2 keeps the enum; ~150 LOC touch if statuses change.

**Claim / executor columns** — the heavy part. Consumers of `latest_status`, `latest_status_at`, `latest_status_build_id`, `latest_status_expires_at`, `latest_preempted_at`, `status_build_id`, `latest_executor*`:

- `utils/claims.ts` (218: actions, `CLAIM_HOLDING_STATUSES`, `restartExpected`, `rootsSatisfiedFrom`)
- `utils/stoppable.ts` (371: mirrors `_cli/_stop.py` selection on the task row)
- `ClaimTriage.tsx` (580), `ClaimActionDialog.tsx` (105)
- `TaskDetail.tsx:335-600` claim holder block (~250), `:94-320` Modal execution details (~220)
- `BuildControlsDialog.tsx` (745, stop list over `GET /tasks`)
- `BuildSchedulingPanel.tsx` (880; `blocked_by_external` is already dead on current servers, `types/task.ts:221`)
- `BuildView.tsx:379-414` ownership count; `StatusBadge.tsx:96-120` cross-build indicator via `statusBuildId`; `TaskTable`, `DagGraph:328`
- `ConcurrencyLimits.tsx` holders, `ExecutorBadge.tsx`, `modalLinks.ts`
- Total ~3,500 LOC of source plus most of the 5.7k test LOC (`ClaimTriage`, `TaskDetail`, `BuildControlsDialog`, `BuildSchedulingPanel`, `claims`, `stoppable` tests).
- Every one of these reads claim state **off the task row**. A v2 separate claim entity means rewiring all of them to a claim record keyed by completion hash; the logic mostly survives, the data source and keys change.

**Executions** — no entity in the UI. Execution identity is inferred from `latest_executor/_ref/_metadata` on the task row + `event_metadata` in the event log (`TaskDetail`), `latest_execution_id` is never read. A v2 executions record is new UI (and would simplify stoppable.ts / ClaimTriage).

**Sizing verdict:** "UI targets v2 entities" is a **large** issue if claims move off the task row (≈3.5k src + ≈4k test LOC touched, mostly rewiring, dominated by the claim/stop/triage cluster). The scope/config/deployment parts are small (~150 LOC delete + ~25 LOC repoint). Split suggestion: (a) delete scope + repoint config (small); (b) task identity split instance vs claim across API calls (medium, mechanical, blocked on API); (c) claim/execution surfaces onto the claim/execution entities (large); (d) new views: plan, deployments, executions (medium, additive).

## 4. In-flight sta-85 UI work

- `andhus/sta-85-align-cancel-stop`: 16 commits vs `origin/main` (d17d689a..b3e2152a, 2026-09-22/23), diffstat 10 files +779/-323: `BuildControlsDialog(.test)`, `BuildOverrideSection`, `BuildView(.test)`, `BuildSchedulingPanel.test`, `ClaimActionDialog`, `TaskDetail(.test)`, `utils/claims.ts`. **Already squash-merged as #376 (`70cb5ced`)**: `git diff origin/main andhus/sta-85-align-cancel-stop -- app/stardag-ui` shows only `package.json`/lock drift from the later dep bump. The branch commits listed by `origin/main..` are pre-squash originals, not pending work.
  - What it did: build dialog has two verbs — Stop (the work) first, then "Record an outcome instead" (complete/fail/cancel); Mark completed withheld while the build may still hold claims (empty/truncated stop scan = unknown); TaskDetail states the claim and the dialog explains it; one label per claim action (`CLAIM_ACTION_LABELS` "Release claim and retry" / "Reset to pending"); unknown claim owner counts as ours (`BuildView.tsx:399-407`); a claim with no recorded holder became releasable; admin permission fix for cross-build release.
- `andhus/sta-85-preview`: **0 commits** beyond `origin/main` (tip = `70cb5ced`, #376). Worktree only has untracked `app/stardag-ui/vite.config.preview.ts` (proxy to preview API on :18000) and `integration-tests/shots/*.png` screenshots. Nothing moving.
- Implication for v2: nothing in flight on the UI; the just-merged #376 deepened the task-row claim model in exactly the cluster v2 would rewire (`claims.ts`, TaskDetail claim block, BuildControlsDialog, BuildView ownership), so v2 builds on #376's semantics, not a moving target.

## 5. Proposed v2 views

- **Task instance page** (replaces TaskDetail header + "Task Parameters"): all-parameter body, namespace/name/version, output URI, artifacts (reuse `ArtifactViewer`), link to its claim. Reuse: TaskDetail layout, `CopyChip`, `FullscreenModal`. `param.*` explorer columns should read the instance body.
- **Task claim panel** (keyed by completion hash): status, holder build, since/expiry, preempted-restart-due, release/reset actions. Reuse almost all of TaskDetail `:335-600`, `ClaimActionDialog`, `claims.ts` (`availableClaimActions`, `restartExpected`), `StatusBadge` cross-build indicator. Show the N instances that map to one claim (new: a claim can be shared by instances differing only in non-significant params).
- **Plan per build** (replaces scope-keyed graph): `BuildView` + `DagGraph`/`dagLayout`/`TaskNode`/`BatchNode`/`DagControls` reused over the build's plan edges; `BuildSchedulingPanel` frontier + `TickSummaryTrail` reused as-is.
- **Deployments**: new page (`/deployments`) listing app -> code versions, current marker, Modal app link (reuse `modalLinks`, `CopyChip`); `api/deployments.ts` already exists. Build info links the build to its deployment instead of showing a scope key.
- **exec_config**: repoint `BuildInfoDialog` `ConfigField` to `exec_config`, drop the "part of the structure scope" hint.
- **Executions**: per-task execution list (attempts, executor, call ref, start/end, outcome) in TaskDetail, reusing `ModalExecutionDetails`/`ModalExecutionCallRef` (`TaskDetail.tsx:94-320`) and `ExecutorBadge`; the build Stop list (`BuildControlsDialog` + `stoppable.ts`) could read executions directly instead of reconstructing from task rows — a simplification, but must stay identical to `stardag builds stop`.
- **Delete**: `utils/scope.ts`, `utils/graphEdges.ts` scope part (edge id -> source-target), `BuildInfoDialog` `ScopeField`, DagGraph cross-scope styling/tooltip, all `scope_key`/`is_cross_scope` type fields, `BuildFrontier.blocked_by_external*` + `BlockerCard` path in BuildSchedulingPanel (already dead), `is_phantom`, and their tests.
