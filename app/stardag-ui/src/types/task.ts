export type TaskStatus =
  | "unregistered"
  | "pending"
  | "running"
  | "suspended"
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

// Descriptive metadata about the executor backend that ran a task or
// triggered a build (recorded on TASK_STARTED / build creation events).
// For Modal executions: {kind: "modal", app_name, workspace, environment,
// function_name, app_id, function_id} — every key is optional (older SDKs
// may record a subset), so consumers must handle missing fields (see
// utils/modalLinks.ts).
export interface ExecutorMetadata {
  kind?: string;
  app_name?: string;
  workspace?: string;
  environment?: string;
  function_name?: string;
  // Modal object ids captured at execution time (best-effort; absent on
  // data recorded before the SDK started capturing them). Used to build
  // stop/redeploy-proof dashboard deep links — see utils/modalLinks.ts.
  //   app_id      — Modal App object id, "ap-…"
  //   function_id — Modal Function object id, "fu-…"
  app_id?: string;
  function_id?: string;
  // Build-level only: true when triggered in reactive (tick-scheduled) mode
  reactive?: boolean;
  [key: string]: unknown;
}

// User info for manual status triggers
export interface StatusTriggeredByUser {
  id: string;
  email: string;
  display_name: string | null;
}

// Build entity
export interface Build {
  id: string;
  environment_id: string;
  user_id: string | null;
  name: string;
  description: string | null;
  commit_hash: string | null;
  root_task_ids: string[];
  created_at: string;
  status: BuildStatus;
  started_at: string | null;
  completed_at: string | null;
  // User who triggered the status change (for manual overrides)
  status_triggered_by_user: StatusTriggeredByUser | null;
  // True iff the latest build-level event is BUILD_RESUMED — set when
  // the SDK reused this build via sd.build(resume_build_id=...). Used
  // by the UI to render "running (resumed)" instead of plain "running".
  // Optional in the type so older API responses (without the field)
  // deserialize without runtime errors.
  is_resumed?: boolean;
  // Executor-descriptive metadata of the trigger that created the build
  // (e.g. the Modal app of a build_trigger call). Optional: absent on
  // older API responses and null for builds without a recorded trigger
  // executor.
  executor_metadata?: ExecutorMetadata | null;
  // Reactive-scheduling owner: the app whose ticks drive this build. Null
  // for ordinary (resident) builds — its presence is the reactive marker.
  reactive_app_name?: string | null;
  // ---- Liveness. Two different numbers; do not confuse them. ----
  //
  // `last_active_at` is the column the API orders the build list by. It is
  // bumped by build-level LIFECYCLE transitions only (resume, complete,
  // fail, cancel, exit-early, roots appended) — never by task events — so
  // it is NOT an activity signal: a build that has been running tasks for
  // three days still reports its BUILD_STARTED timestamp here.
  //
  // `last_activity_at` is the activity signal: the newest of the build's
  // whole event stream (task events included), its `last_active_at`, and
  // any pending scheduler wake-up. This is what the stale-build reaper
  // measures idleness against, so it is the one to show and filter on.
  // Both are optional: absent on API responses predating them.
  last_active_at?: string | null;
  last_activity_at?: string | null;
}

// Response of POST /builds/{id}/cancel — a superset of Build. The cascade
// fields are empty/zero unless the call passed cascade=true.
export interface BuildCancelResult extends Build {
  cascaded_task_ids: string[];
  cascaded_task_count: number;
}

// Why bulk-cancel did not act on an explicitly requested build id. Kept as
// a union of the reasons the API documents today, widened to `string` at
// the response boundary so an unknown future reason still renders.
export type BulkCancelSkipReason =
  | "not_found"
  | "not_running"
  | "reactive"
  | "not_idle"
  | "limit_reached";

// Body of POST /builds/bulk-cancel. At least one of `build_ids` /
// `idle_for_seconds` is required — the API answers 422 otherwise.
export interface BulkCancelBuildsRequest {
  build_ids?: string[];
  // Idleness measured against `last_activity_at`, not `last_active_at`.
  idle_for_seconds?: number;
  reactive_app_name?: string | null;
  include_reactive?: boolean;
  // Also cancel the build's RUNNING/SUSPENDED tasks, releasing the
  // execution claims and concurrency slots they hold.
  cascade?: boolean;
  dry_run?: boolean;
  limit?: number;
  reason?: string | null;
}

export interface CancelledBuildRef {
  build_id: string;
  name: string;
  last_activity_at: string | null;
  reactive_app_name: string | null;
  cascaded_task_ids: string[];
}

export interface BulkCancelBuildsResponse {
  dry_run: boolean;
  builds: CancelledBuildRef[];
  build_count: number;
  task_count: number;
  // build_id -> reason (see BulkCancelSkipReason).
  skipped: Record<string, string>;
  // More builds matched the filter than `limit` allowed; call again.
  truncated: boolean;
}

export interface BuildListResponse {
  builds: Build[];
  total: number;
  page: number;
  page_size: number;
}

// Task with status (from build context)
export interface Task {
  id: string;
  task_id: string;
  environment_id: string;
  task_namespace: string;
  task_name: string;
  task_data: Record<string, unknown>;
  version: string | null;
  // Output URI (path to task output if it has a FileSystemTarget)
  output_uri: string | null;
  created_at: string;
  status: TaskStatus;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  artifact_count: number;
  // Artifact data - mapping of artifact_name -> body_json (populated when artifact columns requested)
  artifact_data?: Record<string, Record<string, unknown>>;
  // Lock status - true if task is waiting for a global lock held by another build
  waiting_for_lock?: boolean;
  // Build where the status-determining event occurred (for cross-build indicators)
  status_build_id?: string;
  // Git commit hash from the event that determined the current status
  commit_hash?: string | null;
  // True for placeholder rows the API auto-creates when an edge points at
  // a not-yet-registered task (legacy/safety-hatch path; with the SDK's
  // post-order discover this is rare). UI hides phantoms from list views.
  is_phantom?: boolean;
  // Executor identity of the most recent TASK_STARTED event. Optional:
  // absent on older API responses, null for tasks never started via an
  // executor that records it.
  latest_executor?: string | null;
  latest_executor_ref?: string | null;
  latest_executor_metadata?: ExecutorMetadata | null;
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
}

// Graph structures
export interface TaskNode {
  id: string;
  task_id: string;
  task_name: string;
  task_namespace: string;
  status: TaskStatus;
  artifact_count: number;
}

export interface TaskEdge {
  source: string; // upstream task internal id
  target: string; // downstream task internal id
  is_dynamic?: boolean; // true if the edge was yielded dynamically at runtime
}

export interface TaskGraphResponse {
  nodes: TaskNode[];
  edges: TaskEdge[];
}

export interface TaskNodeExtended extends TaskNode {
  is_primary: boolean;
  traversal_depth: number;
}

export interface GroupSummary {
  group_id: string;
  task_name: string;
  task_namespace: string;
  count: number;
  sample_task_ids: string[];
  depth: number;
  status: TaskStatus;
  downstream_task_pks: string[];
}

export interface TaskEdgeExtended {
  source: string;
  target: string;
  is_dynamic?: boolean;
}

export interface TaskGraphExtendedResponse {
  nodes: TaskNodeExtended[];
  edges: TaskEdgeExtended[];
  groups: GroupSummary[];
  truncated: boolean;
  total_upstream_count: number;
  total_downstream_count: number;
}

// Task artifacts
export type TaskArtifactType = "markdown" | "json";

// Body is always a dict stored in body_json
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

// Event types
export type EventType =
  | "build_started"
  | "build_completed"
  | "build_failed"
  | "build_cancelled"
  | "build_exit_early"
  | "task_pending"
  | "task_referenced"
  | "task_started"
  | "task_suspended"
  | "task_resumed"
  | "task_waiting_for_lock"
  | "task_completed"
  | "task_failed"
  | "task_skipped"
  | "task_cancelled";

export interface TaskEvent {
  id: string;
  build_id: string;
  task_id: string | null;
  event_type: EventType;
  created_at: string;
  error_message: string | null;
  event_metadata: Record<string, unknown> | null;
}

// Task with filter/DAG context
export interface TaskWithContext extends Task {
  isFilterMatch: boolean;
}

// Type guard for extended graph response
export function isExtendedResponse(
  graph: TaskGraphResponse | TaskGraphExtendedResponse,
): graph is TaskGraphExtendedResponse {
  return "groups" in graph;
}
