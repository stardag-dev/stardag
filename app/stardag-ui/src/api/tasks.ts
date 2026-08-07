import type {
  Build,
  BuildCancelResult,
  BuildFrontier,
  BuildListResponse,
  BuildStatus,
  BuildTickSummaryListResponse,
  BulkCancelBuildsRequest,
  BulkCancelBuildsResponse,
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

/**
 * Best-effort error message for a failed response.
 *
 * FastAPI puts the useful part in a JSON ``detail`` (the 422 "provide
 * build_ids and/or idle_for_seconds" and the 403 admin gate both say
 * exactly what went wrong), while ``statusText`` says only
 * "Unprocessable Entity". Falls back to the status line when the body is
 * missing or not JSON.
 */
async function errorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail) return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first?.msg === "string") return first.msg;
    }
  } catch {
    // Not JSON (or already consumed) — fall through to the status line.
  }
  return `${fallback}: ${response.statusText}`;
}

// Build API

export interface BuildFilters {
  page?: number;
  page_size?: number;
  environment_id?: string;
  // Derived build status, filtered server-side.
  status?: BuildStatus;
  // Only builds reactively scheduled by the named app.
  reactive_app_name?: string;
  // Only builds with no activity for at least this long, measured on
  // `last_activity_at`. Same definition and same 60s floor as
  // POST /builds/bulk-cancel — deliberately the one predicate, so what a
  // list shows and what a cleanup would act on cannot disagree. Ordering
  // flips to stalest-first when it is set.
  //
  // Only `status=running` may accompany it (that pair is the
  // abandoned-build query and stays exact); any other status is a 422.
  idle_for_seconds?: number;
}

export async function fetchBuilds(
  filters: BuildFilters = {},
): Promise<BuildListResponse> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.environment_id) params.set("environment_id", filters.environment_id);
  if (filters.status) params.set("status", filters.status);
  if (filters.reactive_app_name)
    params.set("reactive_app_name", filters.reactive_app_name);
  if (filters.idle_for_seconds)
    params.set("idle_for_seconds", String(filters.idle_for_seconds));

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

/**
 * The build's scheduling frontier — what a reactive scheduler tick sees.
 *
 * This is the endpoint that answers "why is this build not progressing?":
 * it reports what is actionable, what is running, and — when neither is —
 * which upstreams outside the build are holding it back. See
 * ``BuildFrontier.blocked_by_external`` for the one ambiguity that matters:
 * an empty blocker list means "not blocked, OR not stalled".
 */
export async function fetchBuildFrontier(
  buildId: string,
  environmentId: string,
): Promise<BuildFrontier> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);

  const url = `${API_BASE}/builds/${buildId}/frontier?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(await errorMessage(response, "Failed to fetch build frontier"));
  }
  return response.json();
}

/**
 * The build's recent reactive-scheduler tick summaries, newest first.
 *
 * Returns ``null`` when the server does not have the endpoint (404) — it
 * postdates the frontier, so a UI pointed at an older API must degrade to
 * "no tick history" rather than to an error. A missing *build* would have
 * failed the frontier fetch first, so treating 404 as "unavailable" does
 * not hide a real problem.
 */
export async function fetchBuildTickSummaries(
  buildId: string,
  environmentId: string,
  limit?: number,
): Promise<BuildTickSummaryListResponse | null> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);
  if (limit) params.set("limit", String(limit));

  const url = `${API_BASE}/builds/${buildId}/tick-summaries?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (response.status === 404) return null;
  if (!response.ok) {
    throw new Error(await errorMessage(response, "Failed to fetch tick summaries"));
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

  const url = `${API_BASE}/tasks/graph?${params.toString()}`;
  const response = await fetchWithAuth(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      task_ids: taskIds,
      upstream_depth: options?.upstream_depth ?? 0,
      downstream_depth: options?.downstream_depth ?? 0,
      max_per_type_per_level: options?.max_per_type_per_level ?? 5,
      max_total_nodes: options?.max_total_nodes ?? 500,
    }),
  });
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
  /**
   * Filter by the task's *global* (environment-wide) status. Repeatable —
   * `["running", "suspended"]` matches either. This is how "which tasks are
   * holding an execution claim?" is asked.
   */
  status?: TaskStatus[];
  /**
   * Only tasks whose current status was recorded strictly before this
   * ISO-8601 instant. An absolute timestamp rather than a duration so a
   * paged scan cannot drift between requests. Tasks with no recorded
   * status timestamp never match.
   */
  status_older_than?: string;
}

/**
 * List tasks in an environment.
 *
 * With a status or staleness filter the server orders **oldest claim
 * first** (the task RUNNING longest is the most likely to be abandoned and
 * the most expensive to leave holding a claim); otherwise newest-first by
 * registration time.
 */
export async function fetchTasks(
  filters: GlobalTaskFilters = {},
): Promise<{ tasks: Task[]; total: number; page: number; page_size: number }> {
  const params = new URLSearchParams();
  if (filters.page) params.set("page", String(filters.page));
  if (filters.page_size) params.set("page_size", String(filters.page_size));
  if (filters.task_name) params.set("task_name", filters.task_name);
  if (filters.environment_id) params.set("environment_id", filters.environment_id);
  for (const status of filters.status ?? []) params.append("status", status);
  if (filters.status_older_than)
    params.set("status_older_than", filters.status_older_than);

  const url = `${API_BASE}/tasks?${params.toString()}`;
  const response = await fetchWithAuth(url);
  if (!response.ok) {
    throw new Error(`Failed to fetch tasks: ${response.statusText}`);
  }
  return response.json();
}

export async function fetchTask(taskId: string, environmentId?: string): Promise<Task> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);

  const response = await fetchWithAuth(
    `${API_BASE}/tasks/${taskId}?${params.toString()}`,
  );
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

/**
 * Cancel a single build.
 *
 * ``cascade`` additionally emits TASK_CANCELLED for the build's
 * RUNNING/SUSPENDED tasks, releasing the execution claims and
 * concurrency-limit slots they hold. It defaults to false server-side, so
 * an omitted argument keeps the historical behaviour (a build-level event
 * and nothing else).
 */
export async function cancelBuild(
  buildId: string,
  environmentId?: string,
  triggeredByUserId?: string,
  cascade = false,
): Promise<BuildCancelResult> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);
  if (triggeredByUserId) params.set("triggered_by_user_id", triggeredByUserId);
  if (cascade) params.set("cascade", "true");

  const url = `${API_BASE}/builds/${buildId}/cancel?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorMessage(response, "Failed to cancel build"));
  }
  return response.json();
}

/**
 * Cancel every RUNNING build matching an explicit id set and/or an
 * idleness threshold, optionally releasing the claims their tasks hold.
 *
 * Pass ``dry_run: true`` first: the response then reports the exact same
 * selection (builds, cascaded task ids, per-build skip reasons,
 * truncation) that a real call would act on, and writes nothing.
 *
 * On the JWT path this requires the workspace admin role.
 */
export async function bulkCancelBuilds(
  request: BulkCancelBuildsRequest,
  environmentId: string,
): Promise<BulkCancelBuildsResponse> {
  const params = new URLSearchParams();
  params.set("environment_id", environmentId);

  const url = `${API_BASE}/builds/bulk-cancel?${params.toString()}`;
  const response = await fetchWithAuth(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(request),
  });
  if (!response.ok) {
    throw new Error(await errorMessage(response, "Failed to cancel builds"));
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

/**
 * Cancel a task under a build, releasing the execution claim and any
 * concurrency-limit slot it holds.
 *
 * ``buildId`` addresses the *owning* build — the one whose event produced
 * the task's current status (``latest_status_build_id``) — which is not
 * necessarily the build the user is looking at. A task's status is
 * environment-global, so releasing a claim is inherently a cross-build act.
 */
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
    throw new Error(await errorMessage(response, "Failed to cancel task"));
  }
}

/**
 * Reset a failed / cancelled / skipped / **suspended** task to pending.
 *
 * Suspended is included: a task suspended for dynamic dependencies is not
 * executing, so one whose orchestrator died has no path forward except
 * running again from scratch. RUNNING tasks are deliberately unaffected —
 * they hold a live claim, and releasing that is `cancelTask`, not a retry.
 * COMPLETED is sticky. Same ``buildId`` rule as `cancelTask`.
 */
export async function retryTask(
  buildId: string,
  taskId: string,
  environmentId?: string,
): Promise<void> {
  const params = new URLSearchParams();
  if (environmentId) params.set("environment_id", environmentId);

  const url = `${API_BASE}/builds/${buildId}/tasks/${taskId}/retry?${params.toString()}`;
  const response = await fetchWithAuth(url, { method: "POST" });
  if (!response.ok) {
    throw new Error(await errorMessage(response, "Failed to retry task"));
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
