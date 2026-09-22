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
 *
 * No ranking and no event-log reconstruction — the reason the list is read
 * *before* the build is cancelled. A cancel releases the build's claims,
 * and from that moment a neighbour may take a task over, so the row would
 * name somebody else's call. While the claims are held, the build id on
 * the row settles it.
 *
 * The executor ref decides what can be *stopped*, not what is listed. It
 * used to be a third condition above, and that was a bug (STA-88): a task
 * is claimed before its container exists. The tick starts it twice — a
 * claim, which sets RUNNING with no ref because nothing has been spawned
 * yet, then a ref-bearing start once the spawn returns a call id — so
 * between the two the row is RUNNING, held by this build, and names no
 * call. Dropping it there made the panel silently short by however many
 * tasks were mid-spawn, worst during a fan-out, which is exactly when
 * somebody opens this panel.
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

/**
 * Why a listed execution cannot be stopped. None of the reasons drop the
 * row.
 *
 * Kept identical to `_stop.py`'s `NO_REF_YET`, because the panel's job is
 * to explain the command's own output.
 */
export const NO_REF_YET =
  "no call id recorded yet — it was claimed but not yet spawned";

/**
 * A non-detached execution: the build ran it in its own process, thread
 * or subprocess, so the row carries no executor, no ref and no metadata.
 * Permanent — there is no remote thing to cancel — and it must be caught
 * before the Modal comparison or it inherits Modal's branch.
 */
export const NO_EXECUTOR = "no executor recorded — there is nothing here to reach";

/** Statuses whose row may still have a container behind it. */
export const STOPPABLE_STATUSES: TaskStatus[] = ["running", "interrupted"];

/** One live execution of a build, as its task row describes it. */
export interface StoppableExecution {
  taskId: string;
  qualifiedName: string;
  namespace: string;
  status: TaskStatus;
  executor: string;
  /** Null while the task is claimed but its spawn has not reported yet. */
  executorRef: string | null;
  metadata: ExecutorMetadata | null;
  /** The worker name the app declares (Modal's `worker_` prefix stripped). */
  worker: string | null;
  /** When the task entered this status — "running since". */
  statusAt: string | null;
  /** A preemption was reported and its restart has not landed yet. */
  restartDue: boolean;
  /** Whether `stardag builds stop` can cancel it, or only list it. */
  stoppable: boolean;
  /**
   * Why it can only be listed, or null when it can be stopped.
   *
   * Three ways to be unstoppable, and only one is temporary — which is
   * the distinction the operator is making. No executor at all
   * (`NO_EXECUTOR`, a non-detached execution) and another executor are
   * both permanent. `NO_REF_YET` is the one moment: the spawn will
   * report a ref, and the panel shows it stoppable on its next refresh.
   */
  notStoppableReason: string | null;
}

/** What the panel's controls narrow the list to. All optional. */
export interface StopFilters {
  worker?: string;
  executor?: string;
  namespace?: string;
  olderThanSeconds?: number;
  /**
   * Exact task ids, from ticking individual rows. When set it is the whole
   * selection — the CLI's `--task-id` is exact and repeatable, so a list of
   * ids names the set on its own and the other flags would only restate it.
   *
   * **Must be non-empty when present.** An empty list is not "select
   * nothing": `stopCommand` would emit no `--task-id` flags at all, and a
   * command with no filters means *every* execution the build holds — so
   * an empty selection would silently widen into the broadest possible
   * action. There is no command string that means "stop nothing", so the
   * caller must not ask for one; see `BuildStopPanel`, which renders the
   * reason instead of a command.
   */
  taskIds?: string[];
}

/** One page of `GET /tasks`, as the panel's fetcher returns it. */
export interface ClaimHolderPage {
  tasks: Task[];
  total: number;
}

/** Rows per page — the server's maximum. */
export const CLAIM_PAGE_SIZE = 100;

/**
 * How many pages the panel will walk before it gives up and says so.
 *
 * Far lower than the CLI's cap, and deliberately: this runs in a browser
 * on every refresh, where two thousand claim holders is already an absurd
 * amount of sequential requests to make on someone's behalf. Reaching it
 * is reported rather than hidden — see `collectExecutions`.
 */
export const MAX_CLAIM_PAGES = 20;

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
  if (task.latest_status_build_id !== buildId) return null;
  const metadata = task.latest_executor_metadata ?? null;
  const executorRef = task.latest_executor_ref ?? null;
  // No executor named on the row is three different things, and only a
  // ref tells them apart. With a ref: data from before `latest_executor`
  // existed, and Modal is the only executor that has ever recorded one,
  // so the legacy guess is safe. Without one: either a claim written
  // before its spawn, whose metadata declares its `kind`; or a
  // non-detached execution, which records none of the three. That last
  // row must not inherit the Modal guess — see `NO_EXECUTOR`.
  const declared = task.latest_executor || metadata?.kind || "";
  const executor = declared || (executorRef ? MODAL_EXECUTOR : "");
  const notStoppableReason = !executor
    ? NO_EXECUTOR
    : executor !== MODAL_EXECUTOR
      ? `stardag cannot stop a '${executor}' execution`
      : !executorRef
        ? NO_REF_YET
        : null;
  return {
    taskId: task.task_id,
    qualifiedName: qualify(task.task_namespace, task.task_name),
    namespace: task.task_namespace,
    status,
    executor,
    executorRef,
    metadata,
    worker: workerOf(task.latest_executor_metadata),
    statusAt: task.latest_status_at ?? null,
    restartDue: restartDue(task),
    stoppable: notStoppableReason === null,
    notStoppableReason,
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
 * Page through the environment's claim holders and keep this build's.
 *
 * `GET /tasks` has no build filter, so the scan is environment-wide and
 * narrowed here — the same shape as the CLI's collector, and for the same
 * reason: the population is "tasks holding a claim", not "tasks".
 *
 * **One page is not enough, and the failure is silent.** A build's
 * executions can sit entirely on later pages, in which case a single-page
 * read finds none — and "none" is exactly what this panel renders as
 * *absent*. So the truncation flag is not a nicety: without it, "we
 * stopped looking" and "there is nothing running" are the same screen.
 */
export async function collectExecutions(
  fetchPage: (page: number) => Promise<ClaimHolderPage>,
  buildId: string,
): Promise<{ executions: StoppableExecution[]; total: number; truncated: boolean }> {
  const executions: StoppableExecution[] = [];
  let total = 0;
  let page = 1;
  for (;;) {
    const result = await fetchPage(page);
    total = result.total;
    executions.push(...executionsForBuild(result.tasks, buildId));
    const seen = (page - 1) * CLAIM_PAGE_SIZE + result.tasks.length;
    if (result.tasks.length === 0 || seen >= result.total) {
      return { executions, total, truncated: false };
    }
    if (page >= MAX_CLAIM_PAGES) {
      return { executions, total, truncated: true };
    }
    page += 1;
  }
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
  if (filters.taskIds && !filters.taskIds.includes(execution.taskId)) return false;
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

/**
 * The executors present in a list, for the filter dropdown.
 *
 * Unattributed rows are skipped: their executor is the empty string, and
 * there is no `--executor` value that names them, so offering a blank
 * option would produce a command that matches nothing.
 */
export function executorsIn(executions: StoppableExecution[]): string[] {
  const names = new Set<string>();
  for (const execution of executions) {
    if (execution.executor) names.add(execution.executor);
  }
  return [...names].sort();
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
  if (filters.taskIds) {
    if (filters.taskIds.length === 0) {
      // Unreachable through the panel, which never builds this. Loud
      // rather than quiet because the quiet version is a command that
      // stops everything — see the field's own note.
      throw new Error(
        "stopCommand: taskIds is present but empty. There is no command " +
          "that means 'stop nothing'; do not offer one.",
      );
    }
    // Exact ids name the set on their own, so they replace the narrowing
    // flags rather than joining them — the CLI's filters are conjunctive,
    // and restating them would only invite the two to drift apart.
    for (const taskId of filters.taskIds) parts.push(`--task-id ${taskId}`);
    return parts.join(" ");
  }
  if (filters.worker) parts.push(`--worker ${filters.worker}`);
  if (filters.executor) parts.push(`--executor ${filters.executor}`);
  if (filters.namespace) parts.push(`--namespace ${filters.namespace}`);
  if (filters.olderThanSeconds) {
    parts.push(`--older-than ${formatDurationFlag(filters.olderThanSeconds)}`);
  }
  return parts.join(" ");
}
