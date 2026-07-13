// API client for named environment concurrency limits (admin surface).

import type { ExecutorMetadata, TaskStatus } from "../types/task";
import { fetchWithAuth } from "./client";
import { API_V1 } from "./config";

export interface ConcurrencyLimit {
  key: string;
  max_concurrent: number;
}

// A RUNNING task currently occupying one slot of a limit key.
export interface ConcurrencyLimitHolder {
  task_id: string;
  task_namespace: string;
  task_name: string;
  // When the task's RUNNING status was recorded ("running since").
  latest_status_at: string | null;
  latest_executor: string | null;
  latest_executor_ref: string | null;
  latest_executor_metadata: ExecutorMetadata | null;
}

export interface ConcurrencyLimitHoldersResponse {
  key: string;
  // Capped by the `limit` param, oldest running first; `total` carries
  // the full holder count.
  holders: ConcurrencyLimitHolder[];
  total: number;
}

export interface EvictHolderResponse {
  task_id: string;
  status: TaskStatus;
}

function limitsUrl(environmentId: string, path = "", extra?: URLSearchParams): string {
  const params = extra ?? new URLSearchParams();
  params.set("environment_id", environmentId);
  return `${API_V1}/concurrency-limits${path}?${params.toString()}`;
}

export async function fetchConcurrencyLimits(
  environmentId: string,
): Promise<ConcurrencyLimit[]> {
  const response = await fetchWithAuth(limitsUrl(environmentId));
  if (!response.ok) {
    throw new Error(`Failed to fetch concurrency limits: ${response.statusText}`);
  }
  const data: { limits: ConcurrencyLimit[] } = await response.json();
  return data.limits;
}

export async function upsertConcurrencyLimit(
  key: string,
  maxConcurrent: number,
  environmentId: string,
): Promise<ConcurrencyLimit> {
  const response = await fetchWithAuth(
    limitsUrl(environmentId, `/${encodeURIComponent(key)}`),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ max_concurrent: maxConcurrent }),
    },
  );
  if (!response.ok) {
    throw new Error(`Failed to save concurrency limit: ${response.statusText}`);
  }
  return response.json();
}

export async function deleteConcurrencyLimit(
  key: string,
  environmentId: string,
): Promise<void> {
  const response = await fetchWithAuth(
    limitsUrl(environmentId, `/${encodeURIComponent(key)}`),
    { method: "DELETE" },
  );
  if (!response.ok) {
    throw new Error(`Failed to delete concurrency limit: ${response.statusText}`);
  }
}

export async function fetchConcurrencyLimitHolders(
  key: string,
  environmentId: string,
  limit = 100,
): Promise<ConcurrencyLimitHoldersResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  const response = await fetchWithAuth(
    limitsUrl(environmentId, `/${encodeURIComponent(key)}/holders`, params),
  );
  if (!response.ok) {
    throw new Error(
      `Failed to fetch concurrency limit holders: ${response.statusText}`,
    );
  }
  return response.json();
}

// Evict a slot holder: records TASK_FAILED for a task currently RUNNING
// and holding `key`, freeing all its slots. 404s when the task is not a
// current holder.
export async function evictConcurrencyLimitHolder(
  key: string,
  taskId: string,
  environmentId: string,
): Promise<EvictHolderResponse> {
  const response = await fetchWithAuth(
    limitsUrl(
      environmentId,
      `/${encodeURIComponent(key)}/holders/${encodeURIComponent(taskId)}/evict`,
    ),
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(`Failed to evict holder: ${response.statusText}`);
  }
  return response.json();
}
