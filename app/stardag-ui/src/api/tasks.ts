import type { Task, TaskListResponse, TaskStatus } from "../types/task";

const API_BASE = "/api/v1";

export interface TaskFilters {
  page?: number;
  page_size?: number;
  task_family?: string;
  status?: TaskStatus;
  user?: string;
}

export async function fetchTasks(filters: TaskFilters = {}): Promise<TaskListResponse> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.task_family) params.set("task_family", filters.task_family);
  if (filters.status) params.set("status", filters.status);
  if (filters.user) params.set("user", filters.user);

  const url = `${API_BASE}/tasks?${params.toString()}`;
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch tasks: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTask(taskId: string): Promise<Task> {
  const response = await fetch(`${API_BASE}/tasks/${taskId}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch task: ${response.statusText}`);
  }
  return response.json();
}
