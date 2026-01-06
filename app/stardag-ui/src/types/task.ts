export type TaskStatus = "pending" | "running" | "completed" | "failed";
export type BuildStatus = "pending" | "running" | "completed" | "failed" | "cancelled";

// Build entity
export interface Build {
  id: string;
  workspace_id: string;
  user_id: string | null;
  name: string;
  description: string | null;
  commit_hash: string | null;
  root_task_ids: string[];
  created_at: string;
  status: BuildStatus;
  started_at: string | null;
  completed_at: string | null;
}

export interface BuildListResponse {
  builds: Build[];
  total: number;
  page: number;
  page_size: number;
}

// Task with status (from build context)
export interface Task {
  id: number;
  task_id: string;
  workspace_id: string;
  task_namespace: string;
  task_name: string;
  task_data: Record<string, unknown>;
  version: string | null;
  created_at: string;
  status: TaskStatus;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  asset_count: number;
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
}

// Graph structures
export interface TaskNode {
  id: number;
  task_id: string;
  task_name: string;
  task_namespace: string;
  status: TaskStatus;
  asset_count: number;
}

export interface TaskEdge {
  source: number; // upstream task internal id
  target: number; // downstream task internal id
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
  id: number;
  task_id: string;
  asset_type: TaskAssetType;
  name: string;
  body: Record<string, unknown>;
  created_at: string;
}

export interface TaskAssetListResponse {
  assets: TaskAsset[];
}
