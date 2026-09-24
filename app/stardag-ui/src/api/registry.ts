/**
 * The registry API, `/api/v2`. Every call the UI makes about builds,
 * plans, tasks, executions and deployments goes through here.
 *
 * The routes authenticate an internal JWT with an `environment_id` query
 * parameter (or an API key), so every call takes the environment id.
 */
import type {
  Build,
  BuildFrontier,
  BuildListResponse,
  BuildStatus,
  BuildTickSummaryListResponse,
  Deployment,
  DeploymentKind,
  DeploymentListResponse,
  Execution,
  ExecutionListResponse,
  PlanGraph,
  PlanRoots,
  Settings,
  Task,
  TaskArtifactListResponse,
  TransitionResponse,
} from "../types/task";
import { fetchWithAuth } from "./client";
import { API_V2 } from "./config";

/** A refusal from the registry, with its machine-readable code if any. */
export class RegistryError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null) {
    super(message);
    this.name = "RegistryError";
    this.status = status;
    this.code = code;
  }
}

/**
 * The error a failed response carries.
 *
 * v2 refusals put `{code, detail}` under `detail` (the service's
 * `RegistryError.to_dict()`); FastAPI's own errors put a string or a list
 * of validation errors there. Falls back to the status line.
 */
async function toError(response: Response, what: string): Promise<RegistryError> {
  let message = `${what}: ${response.statusText || response.status}`;
  let code: string | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    const detail = body.detail;
    if (typeof detail === "string" && detail) {
      message = detail;
    } else if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === "string") message = first.msg;
    } else if (detail && typeof detail === "object") {
      const d = detail as { code?: unknown; detail?: unknown; message?: unknown };
      if (typeof d.code === "string") code = d.code;
      const text = d.detail ?? d.message;
      if (typeof text === "string" && text) message = text;
      else if (code) message = `${what}: ${code}`;
    }
  } catch {
    // Not JSON: keep the status line.
  }
  return new RegistryError(message, response.status, code);
}

function url(path: string, environmentId: string, query: Record<string, string> = {}) {
  const params = new URLSearchParams({ environment_id: environmentId, ...query });
  return `${API_V2}${path}?${params.toString()}`;
}

async function getJson<T>(requestUrl: string, what: string): Promise<T> {
  const response = await fetchWithAuth(requestUrl);
  if (!response.ok) throw await toError(response, what);
  return response.json() as Promise<T>;
}

async function postJson<T>(
  requestUrl: string,
  what: string,
  body?: unknown,
): Promise<T> {
  const response = await fetchWithAuth(requestUrl, {
    method: "POST",
    headers: body === undefined ? undefined : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await toError(response, what);
  return response.json() as Promise<T>;
}

// ---- Builds ----

export interface BuildFilters {
  status?: BuildStatus;
  reactiveAppName?: string;
  // 1..500 on the server; there is no pagination and no total.
  limit?: number;
}

/** The environment's builds, newest first (`GET /builds`). */
export async function fetchBuilds(
  environmentId: string,
  filters: BuildFilters = {},
): Promise<Build[]> {
  const query: Record<string, string> = {};
  if (filters.status) query.status = filters.status;
  if (filters.reactiveAppName) query.reactive_app_name = filters.reactiveAppName;
  if (filters.limit) query.limit = String(filters.limit);
  const data = await getJson<BuildListResponse>(
    url("/builds", environmentId, query),
    "Failed to fetch builds",
  );
  return data.builds;
}

export function fetchBuild(buildId: string, environmentId: string): Promise<Build> {
  return getJson(url(`/builds/${buildId}`, environmentId), "Failed to fetch build");
}

/** What a scheduler tick sees of the build's active plan. */
export function fetchBuildFrontier(
  buildId: string,
  environmentId: string,
): Promise<BuildFrontier> {
  return getJson(
    url(`/builds/${buildId}/frontier`, environmentId),
    "Failed to fetch build frontier",
  );
}

/** The build's recent tick summaries, newest first. */
export function fetchBuildTickSummaries(
  buildId: string,
  environmentId: string,
  limit?: number,
): Promise<BuildTickSummaryListResponse> {
  return getJson(
    url(
      `/builds/${buildId}/tick-summaries`,
      environmentId,
      limit ? { limit: String(limit) } : {},
    ),
    "Failed to fetch tick summaries",
  );
}

/**
 * The build's executions with no end reported, over all its plans — what
 * `stardag builds stop` lists. Each row says whether it is under the
 * active plan (`in_current_plan`); the orphans are those that are not,
 * which is what the route's `not_in_current_plan` filter keeps. The UI
 * reads the whole list and filters on the same field.
 */
export async function fetchBuildExecutions(
  buildId: string,
  environmentId: string,
): Promise<Execution[]> {
  const data = await getJson<ExecutionListResponse>(
    url(`/builds/${buildId}/executions`, environmentId),
    "Failed to fetch executions",
  );
  return data.executions;
}

// ---- Build overrides ----

/**
 * Record the build completed. Refused while members are outstanding unless
 * `force` — and `force` never overrides a missing seal or an excluded root.
 */
export function completeBuild(
  buildId: string,
  environmentId: string,
  force = false,
): Promise<Build> {
  return postJson(
    url(`/builds/${buildId}/complete`, environmentId),
    "Failed to complete build",
    { force },
  );
}

export function failBuild(
  buildId: string,
  environmentId: string,
  errorMessage?: string,
): Promise<Build> {
  return postJson(
    url(`/builds/${buildId}/fail`, environmentId),
    "Failed to fail build",
    {
      error_message: errorMessage ?? null,
    },
  );
}

/** Cancel: release the claims the build's plans hold. Stops nothing. */
export function cancelBuild(buildId: string, environmentId: string): Promise<Build> {
  return postJson(
    url(`/builds/${buildId}/cancel`, environmentId),
    "Failed to cancel build",
  );
}

// ---- Plans ----

export function fetchPlanRoots(
  planId: string,
  environmentId: string,
): Promise<PlanRoots> {
  return getJson(
    url(`/plans/${planId}/roots`, environmentId),
    "Failed to fetch plan roots",
  );
}

/**
 * The plan's members and instance edges.
 *
 * **Assumed route**: the registry does not serve `GET /plans/{id}/graph`
 * yet. Returns `null` on 404 so the build view can fall back to what the
 * frontier and the plan's roots carry, and says so on screen.
 */
export async function fetchPlanGraph(
  planId: string,
  environmentId: string,
): Promise<PlanGraph | null> {
  const response = await fetchWithAuth(url(`/plans/${planId}/graph`, environmentId));
  if (response.status === 404) return null;
  if (!response.ok) throw await toError(response, "Failed to fetch plan graph");
  return response.json() as Promise<PlanGraph>;
}

/**
 * Cancel one member's task — only by the build whose plan holds the claim
 * (409 `not_claim_holder` otherwise).
 */
export function cancelMember(
  planId: string,
  taskId: string,
  environmentId: string,
): Promise<TransitionResponse> {
  return postJson(
    url(`/plans/${planId}/members/${taskId}/cancel`, environmentId),
    "Failed to cancel task",
  );
}

/** Reset a failed / cancelled / skipped / suspended / interrupted task. */
export function retryMember(
  planId: string,
  taskId: string,
  environmentId: string,
): Promise<TransitionResponse> {
  return postJson(
    url(`/plans/${planId}/members/${taskId}/retry`, environmentId),
    "Failed to retry task",
  );
}

// ---- Tasks ----

/** A completion with its instances, newest first. */
export function fetchTask(taskId: string, environmentId: string): Promise<Task> {
  return getJson(url(`/tasks/${taskId}`, environmentId), "Failed to fetch task");
}

export function fetchTaskArtifacts(
  taskId: string,
  environmentId: string,
): Promise<TaskArtifactListResponse> {
  return getJson(
    url(`/tasks/${taskId}/artifacts`, environmentId),
    "Failed to fetch task artifacts",
  );
}

// ---- Deployments and settings ----

export interface DeploymentFilters {
  kind?: DeploymentKind;
  appName?: string;
  currentOnly?: boolean;
  limit?: number;
}

export async function fetchDeployments(
  environmentId: string,
  filters: DeploymentFilters = {},
): Promise<Deployment[]> {
  const query: Record<string, string> = {};
  if (filters.kind) query.kind = filters.kind;
  if (filters.appName) query.app_name = filters.appName;
  if (filters.currentOnly) query.current = "true";
  if (filters.limit) query.limit = String(filters.limit);
  const data = await getJson<DeploymentListResponse>(
    url("/deployments", environmentId, query),
    "Failed to fetch deployments",
  );
  return data.deployments;
}

export function fetchSettings(
  settingsHash: string,
  environmentId: string,
): Promise<Settings> {
  return getJson(
    url(`/settings/${settingsHash}`, environmentId),
    "Failed to fetch settings",
  );
}
