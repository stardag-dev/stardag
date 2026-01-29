export type TaskStatus =
  | "pending"
  | "running"
  | "suspended"
  | "completed"
  | "failed"
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
  asset_count: number;
  // Asset data - mapping of asset_name -> body_json (populated when asset columns requested)
  asset_data?: Record<string, Record<string, unknown>>;
  // Lock status - true if task is waiting for a global lock held by another build
  waiting_for_lock?: boolean;
  // Build where the status-determining event occurred (for cross-build indicators)
  status_build_id?: string;
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
  asset_count: number;
}

export interface TaskEdge {
  source: string; // upstream task internal id
  target: string; // downstream task internal id
}

export interface TaskGraphResponse {
  nodes: TaskNode[];
  edges: TaskEdge[];
}

// Task assets
export type TaskAssetType = "markdown" | "json";

// Body is always a dict stored in body_json
// - markdown: { content: "<markdown string>" }
// - json: the actual JSON data dict
export interface TaskAsset {
  id: string;
  task_id: string;
  asset_type: TaskAssetType;
  name: string;
  body: Record<string, unknown>;
  created_at: string;
}

export interface TaskAssetListResponse {
  assets: TaskAsset[];
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
