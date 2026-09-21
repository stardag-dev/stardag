/**
 * Which of a build's task rows name an execution that may still be running.
 *
 * The UI half of `stardag builds stop`. The rules here mirror
 * `stardag/_cli/_stop.py` exactly, and they have to: the panel's whole job
 * is to show the operator the list the command will act on and hand them
 * the command. A list that disagreed with the command would be worse than
 * no list.
 *
 * Both sides read the same endpoint (`GET /tasks`, status-filtered) rather
 * than agreeing about a computation, which is what keeps them identical
 * without a shared implementation. This module holds only the selection,
 * the filters and the command string.
 *
 * The selection is the task row and nothing else:
 *
 *     latest_status_build_id === buildId
 *     latest_status is running | interrupted
 *     latest_executor_ref is set
 *
 * No ranking and no event-log reconstruction — the reason the list is read
 * *before* the build is cancelled. A cancel releases the build's claims,
 * and from that moment a neighbour may take a task over, so the row would
 * name somebody else's call. While the claims are held, the build id on
 * the row settles it.
 *
 * Why those two statuses: RUNNING is the claim, and it covers preemption
 * too (a preemption keeps the status and the ref, and only pulls the
 * claim's expiry forward). INTERRUPTED has released its claim but the
 * server deliberately keeps the executor ref, because a backend with its
 * own retries may be restarting the very same call. SUSPENDED keeps a ref
 * as well, but that execution yielded and *returned* — nothing to stop.
 */

import type { ExecutorMetadata, Task, TaskStatus } from "../types/task";

/** The only executor the CLI can stop. Others are listed, never acted on. */
export const MODAL_EXECUTOR = "modal";

/** Statuses whose row may still have a container behind it. */
export const STOPPABLE_STATUSES: TaskStatus[] = ["running", "interrupted"];

/** One live execution of a build, as its task row describes it. */
export interface StoppableExecution {
  taskId: string;
  qualifiedName: string;
  namespace: string;
  status: TaskStatus;
  executor: string;
  executorRef: string;
  metadata: ExecutorMetadata | null;
  /** The worker name the app declares (Modal's `worker_` prefix stripped). */
  worker: string | null;
  /** When the task entered this status — "running since". */
  statusAt: string | null;
  /** A preemption was reported and its restart has not landed yet. */
  restartDue: boolean;
  /** Whether `stardag builds stop` can cancel it, or only list it. */
  stoppable: boolean;
}

/** What the panel's controls narrow the list to. All optional. */
export interface StopFilters {
  worker?: string;
  executor?: string;
  namespace?: string;
  olderThanSeconds?: number;
}

function qualify(namespace: string, name: string): string {
  return namespace ? `${namespace}.${name}` : name;
}

/**
 * The worker name, with Modal's function-name prefix removed.
 *
 * Modal registers a worker as `worker_<name>`; `<name>` is what the app
 * declares and what `--worker` takes, so the prefix is stripped once here
 * rather than at every comparison.
 */
function workerOf(metadata: ExecutorMetadata | null | undefined): string | null {
  const functionName = metadata?.function_name;
  if (typeof functionName !== "string" || !functionName) return null;
  return functionName.startsWith("worker_")
    ? functionName.slice("worker_".length)
    : functionName;
}

/**
 * Whether a preemption is still outstanding.
 *
 * Derived, never stored: the restart records its own start, which moves
 * `latest_status_at` past `latest_preempted_at`, so this goes false with
 * nothing to clear. (`utils/claims.restartExpected` asks the same question
 * of a RUNNING task; this one also covers INTERRUPTED, which is in this
 * list and not in that one.)
 */
function restartDue(task: Task): boolean {
  if (!task.latest_preempted_at) return false;
  if (!task.latest_status_at) return true;
  return Date.parse(task.latest_preempted_at) > Date.parse(task.latest_status_at);
}

/**
 * Turn one task row into an execution, or null if it holds none.
 *
 * `buildId` is the build being viewed: a row whose status was produced by
 * another build is that build's execution, not this one's, and acting on
 * it would be somebody else's worker killed.
 */
export function executionFromTask(
  task: Task,
  buildId: string,
): StoppableExecution | null {
  const status = task.latest_status;
  if (!status || !STOPPABLE_STATUSES.includes(status)) return null;
  if (!task.latest_executor_ref) return null;
  if (task.latest_status_build_id !== buildId) return null;
  return {
    taskId: task.task_id,
    qualifiedName: qualify(task.task_namespace, task.task_name),
    namespace: task.task_namespace,
    status,
    // A ref with no executor named is data from before `latest_executor`
    // existed. Modal is the only executor that has ever recorded a ref,
    // and dropping the row would hide a live container from the one list
    // that is meant to be exact.
    executor: task.latest_executor || MODAL_EXECUTOR,
    executorRef: task.latest_executor_ref,
    metadata: task.latest_executor_metadata ?? null,
    worker: workerOf(task.latest_executor_metadata),
    statusAt: task.latest_status_at ?? null,
    restartDue: restartDue(task),
    stoppable: (task.latest_executor || MODAL_EXECUTOR) === MODAL_EXECUTOR,
  };
}

/** Every live execution this build holds, from a page of task rows. */
export function executionsForBuild(
  tasks: Task[],
  buildId: string,
): StoppableExecution[] {
  return tasks
    .map((task) => executionFromTask(task, buildId))
    .filter((execution): execution is StoppableExecution => execution !== null);
}

/**
 * Whether an execution survives every filter that is set.
 *
 * Conjunctive, and `namespace` is a *prefix* match — `acme` covers
 * `acme.features`. Same semantics as the CLI's flags, including the rule
 * that a row with no status timestamp never matches a staleness filter: an
 * age that cannot be established is not evidence of age.
 */
export function matchesFilters(
  execution: StoppableExecution,
  filters: StopFilters,
  now: number = Date.now(),
): boolean {
  if (filters.executor && execution.executor !== filters.executor) return false;
  if (filters.namespace && !execution.namespace.startsWith(filters.namespace)) {
    return false;
  }
  if (filters.worker && execution.worker !== filters.worker) return false;
  if (filters.olderThanSeconds) {
    if (!execution.statusAt) return false;
    const parsed = Date.parse(execution.statusAt);
    if (Number.isNaN(parsed)) return false;
    if ((now - parsed) / 1000 < filters.olderThanSeconds) return false;
  }
  return true;
}

/** The worker names present in a list, for the filter dropdown. */
export function workersIn(executions: StoppableExecution[]): string[] {
  const names = new Set<string>();
  for (const execution of executions) {
    if (execution.worker) names.add(execution.worker);
  }
  return [...names].sort();
}

/** The executors present in a list, for the filter dropdown. */
export function executorsIn(executions: StoppableExecution[]): string[] {
  return [...new Set(executions.map((execution) => execution.executor))].sort();
}

/** Render a duration in seconds the way the CLI's `--older-than` takes it. */
export function formatDurationFlag(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/**
 * The exact command for what is on screen — the panel's actual output.
 *
 * The UI does not stop anything itself, and that is a design rule rather
 * than an omission: the server cannot reach Modal and must not learn to,
 * so the credentials that can stop a container are the operator's. What
 * the panel can do is make sure the command they run is the one that
 * matches the list they just read.
 */
export function stopCommand(buildId: string, filters: StopFilters): string {
  const parts = ["stardag builds stop", buildId];
  if (filters.worker) parts.push(`--worker ${filters.worker}`);
  if (filters.executor) parts.push(`--executor ${filters.executor}`);
  if (filters.namespace) parts.push(`--namespace ${filters.namespace}`);
  if (filters.olderThanSeconds) {
    parts.push(`--older-than ${formatDurationFlag(filters.olderThanSeconds)}`);
  }
  return parts.join(" ");
}
