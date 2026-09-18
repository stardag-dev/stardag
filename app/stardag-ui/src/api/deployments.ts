import type { DeploymentListResponse } from "../types/task";
import { fetchWithAuth } from "./client";
import { API_V1 } from "./config";

export interface ListDeploymentsOptions {
  environmentId: string;
  // Only this app family.
  family?: string;
  // Also list deployments already retired.
  includeRetired?: boolean;
}

/**
 * The environment's deployments, newest first — which code version runs
 * under which app handle, and how many running builds each still drives.
 */
export async function listDeployments({
  environmentId,
  family,
  includeRetired,
}: ListDeploymentsOptions): Promise<DeploymentListResponse> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);
  if (family) params.set("family", family);
  if (includeRetired) params.set("include_retired", "true");

  const response = await fetchWithAuth(`${API_V1}/deployments?${params.toString()}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch deployments: ${response.statusText}`);
  }
  return response.json();
}
