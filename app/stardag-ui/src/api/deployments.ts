import type { DeploymentListResponse } from "../types/task";
import { fetchWithAuth } from "./client";
import { API_V1 } from "./config";

export interface ListDeploymentsOptions {
  environmentId: string;
  // Only this app.
  appName?: string;
}

/**
 * The environment's deployments, newest first — which code versions of
 * which apps have been deployed. The newest row for an app is its current
 * deployment; a build follows the current deployment at its next
 * scheduler pass, so there is nothing here to retire or count against.
 */
export async function listDeployments({
  environmentId,
  appName,
}: ListDeploymentsOptions): Promise<DeploymentListResponse> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);
  if (appName) params.set("app_name", appName);

  const response = await fetchWithAuth(`${API_V1}/deployments?${params.toString()}`);
  if (!response.ok) {
    throw new Error(`Failed to fetch deployments: ${response.statusText}`);
  }
  return response.json();
}
