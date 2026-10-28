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
  BuildNotify,
  BuildStatus,
  BuildTickSummaryListResponse,
  ConcurrencyLimit,
  ConcurrencyLimitListResponse,
  Deployment,
  DeploymentKind,
  DeploymentListResponse,
  Execution,
  EventListResponse,
  ExecutionListResponse,
  PlanDetail,
  PlanGraph,
  PlanListResponse,
  Settings,
  Task,
  TaskAnnotations,
  TaskArtifactListResponse,
  TaskEvent,
  TaskExecutionListResponse,
  TaskListResponse,
  TaskStatus,
  TaskSummary,
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

async function getJson<T>(
  requestUrl: string,
  what: string,
  signal?: AbortSignal,
): Promise<T> {
  const response = await fetchWithAuth(requestUrl, signal ? { signal } : undefined);
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
  // Running builds with no lifecycle change for at least this long (>= 60).
  // The server implies RUNNING and refuses any other status alongside it.
  idleForSeconds?: number;
  // Page size, 1..500 on the server.
  limit?: number;
  // The previous page's `next_cursor`; absent for the first page.
  cursor?: string;
}

/**
 * One page of the environment's builds, most recently active first
 * (`GET /builds`). `total` counts every build the filters match; pass
 * `next_cursor` back as `cursor` for the next page (keyset paging, so
 * there is no page number to jump to).
 */
export function fetchBuilds(
  environmentId: string,
  filters: BuildFilters = {},
): Promise<BuildListResponse> {
  const query: Record<string, string> = {};
  if (filters.status) query.status = filters.status;
  if (filters.reactiveAppName) query.reactive_app_name = filters.reactiveAppName;
  if (filters.idleForSeconds) query.idle_for_seconds = String(filters.idleForSeconds);
  if (filters.limit) query.limit = String(filters.limit);
  if (filters.cursor) query.cursor = filters.cursor;
  return getJson<BuildListResponse>(
    url("/builds", environmentId, query),
    "Failed to fetch builds",
  );
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

/** Whether a wake-up is queued for the build (`needs_tick`). */
export function fetchBuildNotify(
  buildId: string,
  environmentId: string,
): Promise<BuildNotify> {
  return getJson(
    url(`/builds/${buildId}/notify`, environmentId),
    "Failed to read the build's wake-up flag",
  );
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

/** Every plan of the build, newest generation first, the active one marked. */
export async function fetchBuildPlans(
  buildId: string,
  environmentId: string,
): Promise<PlanDetail[]> {
  const data = await getJson<PlanListResponse>(
    url(`/builds/${buildId}/plans`, environmentId),
    "Failed to fetch plans",
  );
  return data.plans;
}

/**
 * The plan's members (each joined to its task's identity and global
 * status) and the instance edges between member instances. A 404 is a
 * plan that does not exist in this environment.
 */
export function fetchPlanGraph(
  planId: string,
  environmentId: string,
): Promise<PlanGraph> {
  return getJson(
    url(`/plans/${planId}/graph`, environmentId),
    "Failed to fetch plan graph",
  );
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

/**
 * `GET /tasks` filters. They AND together; an absent one does not filter.
 * `namespace: ""` is the root namespace (a filter, not an absent one).
 */
export interface TaskFilters {
  status?: TaskStatus;
  // Exact matches.
  namespace?: string;
  name?: string;
  // A task must carry all of them.
  tags?: string[];
  // The start of the task id.
  idPrefix?: string;
  // Case-insensitive substring of the name, the namespace or the description.
  q?: string;
  // Page size, 1..500 on the server.
  limit?: number;
  // The previous page's `next_cursor`, read with the same filters.
  cursor?: string;
}

/** The query string `GET /tasks` is read with, `environment_id` included. */
export function taskListQuery(
  environmentId: string,
  filters: TaskFilters = {},
): URLSearchParams {
  const params = new URLSearchParams({ environment_id: environmentId });
  if (filters.status) params.set("status", filters.status);
  if (filters.namespace !== undefined) params.set("namespace", filters.namespace);
  if (filters.name) params.set("name", filters.name);
  for (const tag of filters.tags ?? []) params.append("tag", tag);
  if (filters.idPrefix) params.set("id_prefix", filters.idPrefix);
  if (filters.q) params.set("q", filters.q);
  if (filters.limit) params.set("limit", String(filters.limit));
  if (filters.cursor) params.set("cursor", filters.cursor);
  return params;
}

/** The refusal code of a search an older registry cannot answer. */
export const SEARCH_UNSUPPORTED = "search_unsupported";

/** The `GET /tasks` filters a registry older than task search ignores. */
function searchFiltersIn(filters: TaskFilters): string[] {
  const names: string[] = [];
  if (filters.namespace !== undefined) names.push("namespace");
  if (filters.name) names.push("name");
  if (filters.tags?.length) names.push("tag");
  if (filters.idPrefix) names.push("task id");
  if (filters.q) names.push("text");
  return names;
}

/**
 * One page of the environment's completions, most recent status change
 * first (`GET /tasks`). Keyset paging: pass `next_cursor` back as `cursor`
 * with the same filters.
 *
 * A registry older than task search ignores the search filters as unknown
 * query parameters and answers unfiltered; it also serves no `tags` on its
 * rows, which is how that is told apart (as the SDK does). Such a page is
 * refused with {@link SEARCH_UNSUPPORTED} rather than shown as results.
 * Unfiltered and status-only reads work against it as before.
 */
export async function fetchTasks(
  environmentId: string,
  filters: TaskFilters = {},
  signal?: AbortSignal,
): Promise<TaskListResponse> {
  const page = await getJson<TaskListResponse>(
    `${API_V2}/tasks?${taskListQuery(environmentId, filters).toString()}`,
    "Failed to search tasks",
    signal,
  );
  const searched = searchFiltersIn(filters);
  if (searched.length && page.tasks.length && !("tags" in page.tasks[0])) {
    throw new RegistryError(
      `This registry does not support task search (${searched.join(", ")} ` +
        "filters) and would have answered unfiltered. Upgrade the registry " +
        "server; listing without filters, or by status, still works.",
      200,
      SEARCH_UNSUPPORTED,
    );
  }
  return page;
}

/**
 * Set a completion's tags and/or description (`PATCH /tasks/{id}`); an
 * omitted field is left as it is. Returns the task as stored, tags
 * normalised (trimmed, deduplicated, sorted).
 */
export async function annotateTask(
  taskId: string,
  environmentId: string,
  changes: TaskAnnotations,
): Promise<TaskSummary> {
  const response = await fetchWithAuth(
    url(`/tasks/${encodeURIComponent(taskId)}`, environmentId),
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(changes),
    },
  );
  if (!response.ok) throw await toError(response, "Failed to save task annotations");
  return response.json() as Promise<TaskSummary>;
}

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

/** The server's default page of a task's executions (its cap is 500). */
export const TASK_EXECUTION_LIMIT = 100;

/**
 * Every execution of the task, **across builds**, newest first — ended
 * ones included unless `includeEnded` is false. At most
 * `TASK_EXECUTION_LIMIT` rows (the route has no cursor).
 */
export async function fetchTaskExecutions(
  taskId: string,
  environmentId: string,
  includeEnded = true,
): Promise<Execution[]> {
  const data = await getJson<TaskExecutionListResponse>(
    url(`/tasks/${taskId}/executions`, environmentId, {
      include_ended: String(includeEnded),
      limit: String(TASK_EXECUTION_LIMIT),
    }),
    "Failed to fetch task executions",
  );
  return data.executions;
}

/** The server's cap on one event read. */
export const EVENT_LIST_LIMIT = 500;

/**
 * The task's event log across every build, **oldest first**, at most
 * `EVENT_LIST_LIMIT` rows (the oldest ones: the route has no cursor).
 */
export async function fetchTaskEvents(
  taskId: string,
  environmentId: string,
): Promise<TaskEvent[]> {
  const data = await getJson<EventListResponse>(
    url(`/tasks/${taskId}/events`, environmentId, {
      limit: String(EVENT_LIST_LIMIT),
    }),
    "Failed to fetch task events",
  );
  return data.events;
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

/** One deployment by id — for one the list's first page does not hold. */
export function fetchDeployment(
  deploymentId: string,
  environmentId: string,
): Promise<Deployment> {
  return getJson(
    url(`/deployments/${deploymentId}`, environmentId),
    "Failed to fetch deployment",
  );
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

// ---- Concurrency limits ----

/**
 * The routes take the key as a `/{key}` path segment, where a `/` is a
 * separator even percent-encoded (the server decodes it before routing),
 * so such a key is refused here rather than sent to 404.
 */
function limitUrl(key: string, environmentId: string) {
  if (key.includes("/")) {
    throw new RegistryError(
      `A concurrency-limit key cannot contain "/": ${key}`,
      400,
      "invalid_limit_key",
    );
  }
  return url(`/concurrency-limits/${encodeURIComponent(key)}`, environmentId);
}

/**
 * The environment's named limits, each with how many slots live claims
 * occupy — and, with `includeHolders`, which tasks occupy them.
 */
export async function fetchConcurrencyLimits(
  environmentId: string,
  includeHolders = false,
): Promise<ConcurrencyLimit[]> {
  const data = await getJson<ConcurrencyLimitListResponse>(
    url(
      "/concurrency-limits",
      environmentId,
      includeHolders ? { include_holders: "true" } : {},
    ),
    "Failed to fetch concurrency limits",
  );
  return data.limits;
}

/** Create or replace the cap on `key`. */
export async function setConcurrencyLimit(
  key: string,
  maxConcurrent: number,
  environmentId: string,
): Promise<{ key: string; max_concurrent: number }> {
  const response = await fetchWithAuth(limitUrl(key, environmentId), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ max_concurrent: maxConcurrent }),
  });
  if (!response.ok) throw await toError(response, "Failed to save concurrency limit");
  return response.json() as Promise<{ key: string; max_concurrent: number }>;
}

/** Remove the cap on `key`: the key becomes unlimited. */
export async function deleteConcurrencyLimit(
  key: string,
  environmentId: string,
): Promise<void> {
  const response = await fetchWithAuth(limitUrl(key, environmentId), {
    method: "DELETE",
  });
  if (!response.ok) {
    throw await toError(response, "Failed to delete concurrency limit");
  }
}
