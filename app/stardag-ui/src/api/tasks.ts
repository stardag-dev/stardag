import type {
  Build,
  BuildListResponse,
  Task,
  TaskAssetListResponse,
  TaskGraphResponse,
  TaskStatus,
} from "../types/task";
import { fetchWithAuth } from "./client";
import { API_V1 } from "./config";

const API_BASE = API_V1;

// Build API

export interface BuildFilters {
  page?: number;
  page_size?: number;
  workspace_id?: string;
}

export async function fetchBuilds(
  filters: BuildFilters = {},
): Promise<BuildListResponse> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.workspace_id) params.set("workspace_id", filters.workspace_id);

  const url = `${API_BASE}/builds?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch builds: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchBuild(buildId: string, workspaceId: string): Promise<Build> {
  const params = new URLSearchParams();
  params.set("workspace_id", workspaceId);

  const response = await fetchWithAuth(
    `${API_BASE}/builds/${buildId}?${params.toString()}`,
  );
  if (!response.ok) {
    throw new Error(`Failed to fetch build: ${response.statusText}`);
  }
  return response.json();
}

// Task API (build-scoped)

export interface TaskFilters {
  task_name?: string;
  status?: TaskStatus;
  workspace_id?: string;
}

export async function fetchTasksInBuild(
  buildId: string,
  filters: TaskFilters = {},
): Promise<Task[]> {
  const params = new URLSearchParams();
  if (filters.task_name) params.set("task_name", filters.task_name);
  if (filters.status) params.set("status", filters.status);
  if (filters.workspace_id) params.set("workspace_id", filters.workspace_id);

  const url = `${API_BASE}/builds/${buildId}/tasks?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch tasks: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchBuildGraph(
  buildId: string,
  workspaceId?: string,
): Promise<TaskGraphResponse> {
  const params = new URLSearchParams();
  if (workspaceId) params.set("workspace_id", workspaceId);

  const url = `${API_BASE}/builds/${buildId}/graph?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch graph: ${response.statusText}`);
  }
  return response.json();
}

// Global task API (workspace-scoped, no status)

export interface GlobalTaskFilters {
  page?: number;
  page_size?: number;
  task_name?: string;
  workspace_id?: string;
}

export async function fetchTasks(
  filters: GlobalTaskFilters = {},
): Promise<{ tasks: Task[]; total: number; page: number; page_size: number }> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.task_name) params.set("task_name", filters.task_name);
  if (filters.workspace_id) params.set("workspace_id", filters.workspace_id);

  const url = `${API_BASE}/tasks?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch tasks: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTask(taskId: string): Promise<Task> {
  const response = await fetchWithAuth(`${API_BASE}/tasks/${taskId}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch task: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTaskAssets(
  taskId: string,
  workspaceId?: string,
): Promise<TaskAssetListResponse> {
  const params = new URLSearchParams();
  if (workspaceId) params.set("workspace_id", workspaceId);

  const url = `${API_BASE}/tasks/${taskId}/assets?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch task assets: ${response.statusText}`);
  }
  return response.json();
}
