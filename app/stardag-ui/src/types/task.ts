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
