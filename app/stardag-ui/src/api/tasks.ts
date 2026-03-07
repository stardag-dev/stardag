import type {
  Build,
  BuildListResponse,
  Task,
  TaskArtifactListResponse,
  TaskEvent,
  TaskGraphExtendedResponse,
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
  environment_id?: string;
}

export async function fetchBuilds(
  filters: BuildFilters = {},
): Promise<BuildListResponse> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.environment_id) params.set("environment_id", filters.environment_id);

  const url = `${API_BASE}/builds?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch builds: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchBuild(
  buildId: string,
  environmentId: string,
): Promise<Build> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);

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
  environment_id?: string;
}

export async function fetchTasksInBuild(
  buildId: string,
  filters: TaskFilters = {},
): Promise<Task[]> {
  const params = new URLSearchParams();
  if (filters.task_name) params.set("task_name", filters.task_name);
  if (filters.status) params.set("status", filters.status);
  if (filters.environment_id) params.set("environment_id", filters.environment_id);

  const url = `${API_BASE}/builds/${buildId}/tasks?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch tasks: ${response.statusText}`);
  }
  return response.json();
}

export interface TraversalOptions {
  upstream_depth?: number;
  downstream_depth?: number;
  max_per_type_per_level?: number;
  max_total_nodes?: number;
}

export type UpstreamTraversalOptions = TraversalOptions;

export async function fetchBuildGraph(
  buildId: string,
  environmentId?: string,
  options?: UpstreamTraversalOptions,
): Promise<TaskGraphResponse | TaskGraphExtendedResponse> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);
  if (options?.upstream_depth)
    params.set("upstream_depth", String(options.upstream_depth));
  if (options?.downstream_depth)
    params.set("downstream_depth", String(options.downstream_depth));
  if (options?.max_per_type_per_level)
    params.set("max_per_type_per_level", String(options.max_per_type_per_level));
  if (options?.max_total_nodes)
    params.set("max_total_nodes", String(options.max_total_nodes));

  const url = `${API_BASE}/builds/${buildId}/graph?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch graph: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTaskGraph(
  taskIds: string[],
  environmentId: string,
  options?: UpstreamTraversalOptions,
): Promise<TaskGraphExtendedResponse> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);
  params.set("task_ids", taskIds.join(","));
  if (options?.upstream_depth)
    params.set("upstream_depth", String(options.upstream_depth));
  if (options?.downstream_depth)
    params.set("downstream_depth", String(options.downstream_depth));
  if (options?.max_per_type_per_level)
    params.set("max_per_type_per_level", String(options.max_per_type_per_level));
  if (options?.max_total_nodes)
    params.set("max_total_nodes", String(options.max_total_nodes));

  const url = `${API_BASE}/tasks/graph?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch task graph: ${response.statusText}`);
  }
  return response.json();
}

// Global task API (environment-scoped, no status)

export interface GlobalTaskFilters {
  page?: number;
  page_size?: number;
  task_name?: string;
  environment_id?: string;
}

export async function fetchTasks(
  filters: GlobalTaskFilters = {},
): Promise<{ tasks: Task[]; total: number; page: number; page_size: number }> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.task_name) params.set("task_name", filters.task_name);
  if (filters.environment_id) params.set("environment_id", filters.environment_id);

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

export async function fetchTaskArtifacts(
  taskId: string,
  environmentId?: string,
): Promise<TaskArtifactListResponse> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);

  const url = `${API_BASE}/tasks/${taskId}/artifacts?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch task artifacts: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTaskEvents(
  taskId: string,
  environmentId?: string,
): Promise<TaskEvent[]> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);

  const url = `${API_BASE}/tasks/${taskId}/events?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch task events: ${response.statusText}`);
  }
  return response.json();
}

// Build actions

export async function cancelBuild(
  buildId: string,
  environmentId?: string,
  triggeredByUserId?: string,
): Promise<Build> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);
  if (triggeredByUserId) params.set("triggered_by_user_id", triggeredByUserId);

  const url = `${API_BASE}/builds/${buildId}/cancel?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to cancel build: ${response.statusText}`);
  }
  return response.json();
}

export async function completeBuild(
  buildId: string,
  environmentId?: string,
  triggeredByUserId?: string,
): Promise<Build> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);
  if (triggeredByUserId) params.set("triggered_by_user_id", triggeredByUserId);

  const url = `${API_BASE}/builds/${buildId}/complete?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to complete build: ${response.statusText}`);
  }
  return response.json();
}

export async function failBuild(
  buildId: string,
  environmentId?: string,
  triggeredByUserId?: string,
): Promise<Build> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);
  if (triggeredByUserId) params.set("triggered_by_user_id", triggeredByUserId);

  const url = `${API_BASE}/builds/${buildId}/fail?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to fail build: ${response.statusText}`);
  }
  return response.json();
}

// Task actions (build-scoped)

export async function cancelTask(
  buildId: string,
  taskId: string,
  environmentId?: string,
): Promise<void> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);

  const url = `${API_BASE}/builds/${buildId}/tasks/${taskId}/cancel?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(`Failed to cancel task: ${response.statusText}`);
  }
}

// Column management API

export interface AvailableColumnsResponse {
  core: string[];
  params: string[];
  artifacts: string[];
}

export async function fetchAvailableColumns(
  environmentId: string,
  filter?: string,
  q?: string,
): Promise<AvailableColumnsResponse> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);
  if (filter) params.set("filter", filter);
  if (q) params.set("q", q);

  const url = `${API_BASE}/tasks/search/columns?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch available columns: ${response.statusText}`);
  }
  return response.json();
}
