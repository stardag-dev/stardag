export type TaskStatus = "pending" | "running" | "completed" | "failed";

export interface Task {
  task_id: string;
  task_family: string;
  task_data: Record<string, unknown>;
  status: TaskStatus;
  user: string;
  commit_hash: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  dependency_ids: string[];
}

export interface TaskListResponse {
  tasks: Task[];
  total: number;
  page: number;
  page_size: number;
}
