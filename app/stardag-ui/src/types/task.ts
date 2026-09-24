/**
 * Wire types of the `/api/v2` registry routes the UI reads.
 *
 * They mirror `stardag_api/schemas_v2.py`. Vocabulary, kept apart
 * everywhere (design.md, "Two hashes, one flag"):
 *
 * - a **task** is a completion, keyed by `task_id`: its global status and
 *   claim. It holds no parameters.
 * - an **instance** is a registry row, one construction of a task under a
 *   scope `(deployment_id, settings_hash)`: its body holds the parameters.
 *   `instance_hash` is never an identifier on its own — the UI addresses an
 *   instance by its row id, and shows the hash only next to its scope.
 * - a **plan** is one request of a build under one scope; exactly one plan
 *   per build is active.
 * - an **execution** is one attempt at a task, under one plan, from one
 *   instance: the ledger.
 */

export type TaskStatus =
  | "pending"
  | "running"
  | "suspended"
  // Execution taken away by the platform (function timeout, reclaimed
  // container). Not a failure and not terminal; holds no claim.
  | "interrupted"
  | "completed"
  | "failed"
  | "skipped"
  | "cancelled";

export type BuildStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"
  | "exit_early";

export type DeploymentKind = "modal" | "local";

/** How an execution's claim ended; written by the server. */
export type ClaimOutcome =
  | "completed"
  | "failed"
  | "suspended"
  | "interrupted"
  | "cancelled"
  | "taken_over"
  | "lapsed"
  | "released";

/** How an execution itself ended; written only by its own report or a stop. */
export type ExecutionOutcome =
  | "completed"
  | "failed"
  | "suspended"
  | "interrupted"
  | "preempted"
  | "stopped";

// Descriptive metadata about the executor backend that ran an execution or
// triggered a build. For Modal: {kind: "modal", app_name, workspace,
// environment, function_name, app_id, function_id} — every key optional,
// so consumers must handle missing fields (see utils/modalLinks.ts).
export interface ExecutorMetadata {
  kind?: string;
  app_name?: string;
  workspace?: string;
  environment?: string;
  function_name?: string;
  app_id?: string;
  function_id?: string;
  // Build-level only: true when triggered in reactive (tick-scheduled) mode
  reactive?: boolean;
  [key: string]: unknown;
}

// ---- Builds ----

export interface Build {
  id: string;
  name: string;
  description: string | null;
  status: BuildStatus;
  // The request at completion-id level, stable across rollover.
  root_task_ids: string[];
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  // Bumped by build lifecycle transitions only, not by task events.
  last_active_at: string;
  is_resumed: boolean;
  // External id of the user behind a manual status change.
  status_triggered_by_user_id: string | null;
  executor_metadata: ExecutorMetadata | null;
  // The app whose ticks drive this build; null for a resident build.
  reactive_app_name: string | null;
  reactive_tick_kwargs: Record<string, unknown> | null;
  // Why the build is FAILED (its last BUILD_FAILED's message); null for
  // any other status.
  error_message: string | null;
}

export interface BuildListResponse {
  builds: Build[];
  // Matching the filters, over every page.
  total: number;
  // Pass back as `cursor` for the next page; null on the last one.
  next_cursor: string | null;
}

// ---- The frontier: what a scheduler tick sees of the active plan ----

export interface FrontierMember {
  task_id: string;
  instance_id: string;
  instance_hash: string;
  // The task's global status, not "its status in this plan".
  status: TaskStatus;
  is_root: boolean;
  // All parameters, registry-mode dump; `__namespace` / `__name` included.
  body: Record<string, unknown>;
}

/** A runnable or running member, with the counts the tick budgets on. */
export interface FrontierItem extends FrontierMember {
  // Executions of the task under any of the build's plans.
  attempts: number;
  // Those that ended interrupted or preempted.
  interruptions: number;
}

export interface ClosureConflict {
  task_id: string;
  member_instance_id: string;
  other_instance_id: string;
  fields: string[];
}

export interface Closure {
  admitted: number;
  conflicts: ClosureConflict[];
  build_failed: boolean;
}

export interface BuildFrontier {
  build_id: string;
  // Null when the build has no active plan yet.
  plan_id: string | null;
  deployment_id: string | null;
  settings_hash: string | null;
  sealed: boolean;
  plan_complete: boolean;
  build_status: BuildStatus | null;
  reactive_app_name: string | null;
  reactive_tick_kwargs: Record<string, unknown> | null;
  runnable: FrontierItem[];
  discovery_jobs: FrontierMember[];
  running: FrontierItem[];
  closure: Closure | null;
}

export interface PlanRoots {
  plan_id: string;
  build_id: string;
  deployment_id: string;
  settings_hash: string;
  roots: FrontierMember[];
}

/**
 * One of a build's plans, as `GET /builds/{id}/plans` lists them (newest
 * generation first): lifecycle, scope with its deployment, member counts.
 */
export interface PlanDetail {
  id: string;
  build_id: string;
  deployment_id: string;
  deployment: Deployment;
  settings_hash: string;
  // Server-assigned per build, monotonic.
  generation: number;
  created_at: string;
  // The build's active plan from here (the first on create, a
  // replacement on seal).
  activated_at: string | null;
  // The static phase is fully stated and verified.
  sealed_at: string | null;
  // Set when a replacement plan activated.
  superseded_at: string | null;
  is_active: boolean;
  member_count: number;
  root_count: number;
  // Given-up members; counted apart from `member_counts`.
  excluded_count: number;
  // Non-excluded members by their task's global status.
  member_counts: Partial<Record<TaskStatus, number>>;
}

export interface PlanListResponse {
  build_id: string;
  plans: PlanDetail[];
}

// ---- Plan membership and edges (assumed: not served on this branch yet) ----

export type AdmittedBy = "root" | "static" | "dynamic" | "closure";
export type ExclusionReason = "operator" | "discovery_failed" | "upstream_excluded";

/**
 * One member of a plan, as `GET /plans/{plan_id}/graph` is assumed to
 * return it. **Assumed**: this branch's registry does not serve the route
 * yet (see `api/registry.ts`, `fetchPlanGraph`); the shape mirrors
 * `PlanGraphMemberResponse` (`schemas_v2_reads.py`, STA-106) —
 * `plan_member`'s columns joined to the task's identity and global status,
 * plus the attempt/interruption counts the frontier already carries for a
 * runnable member.
 */
export interface PlanMember {
  task_id: string;
  instance_id: string;
  instance_hash: string;
  task_namespace: string;
  task_name: string;
  status: TaskStatus;
  is_root: boolean;
  admitted_by: AdmittedBy | null;
  excluded_at: string | null;
  excluded_reason: ExclusionReason | null;
  // Executions of the task under any of the build's plans, and those of
  // them that ended interrupted or preempted (as on the frontier).
  attempts: number;
  interruptions: number;
}

/** An instance edge, `task_instance_dependency` (assumed route). */
export interface PlanEdge {
  upstream_instance_id: string;
  downstream_instance_id: string;
  is_dynamic: boolean;
}

export interface PlanGraph {
  plan_id: string;
  build_id: string;
  deployment_id: string;
  settings_hash: string;
  members: PlanMember[];
  edges: PlanEdge[];
}

// ---- Persisted reactive-scheduler tick summaries ----

// Why a tick did (or did not) do anything. Widened to `string` at the
// response boundary so an outcome a newer SDK adds still renders.
export type TickOutcome =
  | "not_reactive"
  | "lease_held"
  | "terminal"
  | "lingered_out"
  | "foreign_app"
  | "superseded";

export interface BuildTickSummary {
  id: string;
  build_id: string;
  // A TickOutcome value; typed wide on purpose (see above).
  outcome: string;
  /**
   * The tick's own summary, verbatim and **deliberately open**: the SDK
   * grows counters faster than this UI ships. Render known keys with a
   * label and unknown ones generically — never drop what you don't
   * recognise.
   */
  summary: Record<string, unknown>;
  created_at: string;
}

export interface BuildTickSummaryListResponse {
  build_id: string;
  summaries: BuildTickSummary[];
}

// ---- Tasks and instances ----

/** One instance of a completion: its body under one scope. */
export interface TaskInstance {
  id: string;
  deployment_id: string;
  settings_hash: string;
  instance_hash: string;
  body: Record<string, unknown>;
  // The closure flag: requires() evaluated under this scope.
  expanded_at: string | null;
  created_at: string;
}

/** A completion with its instances in the environment, newest first. */
export interface Task {
  task_id: string;
  task_namespace: string;
  task_name: string;
  version: string | null;
  output_uri: string | null;
  status: TaskStatus;
  status_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  // The claim: live while status is running and this is in the future.
  claim_expires_at: string | null;
  // The claim's holder while RUNNING (live or lapsed): the plan it was
  // granted through, and that plan's build.
  claim_plan_id: string | null;
  claim_build_id: string | null;
  // The current execution (the claim's, while running).
  execution_id: string | null;
  instances: TaskInstance[];
}

// ---- Executions ----

export interface Execution {
  id: string;
  task_id: string;
  build_id: string;
  plan_id: string;
  instance_id: string;
  executor: string | null;
  executor_ref: string | null;
  executor_metadata: ExecutorMetadata | null;
  started_at: string;
  claim_released_at: string | null;
  claim_outcome: ClaimOutcome | null;
  ended_at: string | null;
  outcome: ExecutionOutcome | null;
  // False for an orphan: its plan is not the build's active plan.
  in_current_plan: boolean;
}

export interface ExecutionListResponse {
  build_id: string;
  executions: Execution[];
}

// ---- Deployments and settings ----

export interface Deployment {
  id: string;
  kind: DeploymentKind;
  app_name: string;
  code_id: string;
  image_id: string | null;
  modal_app_id: string | null;
  // Server-assigned, monotonic per (kind, app_name).
  generation: number;
  deployed_at: string;
  // NULL rows are never current and cannot host a plan.
  activated_at: string | null;
  is_current: boolean;
}

export interface DeploymentListResponse {
  deployments: Deployment[];
}

export interface Settings {
  hash: string;
  body: Record<string, string>;
}

// ---- Transitions ----

export interface TransitionResponse {
  applied: boolean;
  status: TaskStatus;
  execution_id: string | null;
  claim_expires_at: string | null;
}

// ---- Task artifacts ----

export type TaskArtifactType = "markdown" | "json";

// - markdown: { content: "<markdown string>" }
// - json: the actual JSON data dict
export interface TaskArtifact {
  id: string;
  task_id: string;
  artifact_type: TaskArtifactType;
  name: string;
  body: Record<string, unknown>;
  created_at: string;
}

export interface TaskArtifactListResponse {
  artifacts: TaskArtifact[];
}
