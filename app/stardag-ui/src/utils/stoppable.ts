/**
 * Which of a build's executions `stardag builds stop` would act on, and
 * the command that does it.
 *
 * The UI half of `builds stop`. Both sides read one list — `GET
 * /builds/{id}/executions`, the build's executions with no end reported
 * (`ended_at IS NULL`), over all of its plans — so they agree by
 * construction rather than by two selections staying in step. That list is
 * independent of where the claim has since gone: an execution ref is not a
 * claim, and an execution whose claim was taken over may still be running.
 * `--not-in-current-plan` narrows it to the **orphans**, executions under a
 * plan that is no longer the build's active one (design.md, "Rollover").
 *
 * **The UI stops nothing.** Stopping a container means reaching the
 * execution backend with the operator's credentials, then reporting it
 * (`POST /executions/{id}/stopped`); that is the CLI's job. The panel
 * shows the list and hands over the command for exactly what is on screen.
 *
 * The executor ref decides what can be *stopped*, not what is listed: an
 * execution is minted and claimed before its container exists, so a row
 * without a ref is listed and explained, never dropped.
 */

import type { Execution } from "../types/task";

/** The only executor the CLI can stop. Others are listed, never acted on. */
export const MODAL_EXECUTOR = "modal";

export const NO_REF_YET =
  "no call id recorded yet — it was claimed but not yet spawned";

export const NO_EXECUTOR =
  "no executor recorded — it runs in the build's own process, or its " +
  "spawn has not reported yet; refresh to see whether a call id appears";

/** Why an execution can only be listed, or null when it can be stopped. */
export function notStoppableReason(execution: Execution): string | null {
  const executor = execution.executor || execution.executor_metadata?.kind || "";
  if (!executor) return NO_EXECUTOR;
  if (executor !== MODAL_EXECUTOR)
    return `stardag cannot stop a '${executor}' execution`;
  if (!execution.executor_ref) return NO_REF_YET;
  return null;
}

/** The worker name the app declares (Modal's `worker_` prefix stripped). */
export function workerOf(execution: Execution): string | null {
  const functionName = execution.executor_metadata?.function_name;
  if (typeof functionName !== "string" || !functionName) return null;
  return functionName.startsWith("worker_")
    ? functionName.slice("worker_".length)
    : functionName;
}

/** What the panel's controls narrow the list to. All optional. */
export interface StopFilters {
  // `--not-in-current-plan`: orphans only. Applied by the server.
  notInCurrentPlan?: boolean;
  worker?: string;
  executor?: string;
  olderThanSeconds?: number;
  /**
   * Exact task ids, from ticking rows. **Must be non-empty when present**:
   * a command with no targets means every execution the build has, so an
   * empty selection would silently widen into the broadest action.
   */
  taskIds?: string[];
}

/** Whether an execution survives the client-side filters that are set. */
export function matchesFilters(
  execution: Execution,
  filters: StopFilters,
  now: number = Date.now(),
): boolean {
  if (filters.notInCurrentPlan && execution.in_current_plan) return false;
  if (filters.taskIds && !filters.taskIds.includes(execution.task_id)) return false;
  if (filters.executor && (execution.executor ?? "") !== filters.executor) return false;
  if (filters.worker && workerOf(execution) !== filters.worker) return false;
  if (filters.olderThanSeconds) {
    const started = Date.parse(execution.started_at);
    if (Number.isNaN(started)) return false;
    if ((now - started) / 1000 < filters.olderThanSeconds) return false;
  }
  return true;
}

export function workersIn(executions: Execution[]): string[] {
  const names = new Set<string>();
  for (const execution of executions) {
    const worker = workerOf(execution);
    if (worker) names.add(worker);
  }
  return [...names].sort();
}

export function executorsIn(executions: Execution[]): string[] {
  const names = new Set<string>();
  for (const execution of executions) {
    if (execution.executor) names.add(execution.executor);
  }
  return [...names].sort();
}

/** Render seconds the way the CLI's `--older-than` takes them. */
export function formatDurationFlag(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}

/** The exact command for what is on screen — the panel's actual output. */
export function stopCommand(buildId: string, filters: StopFilters): string {
  const parts = ["stardag builds stop", buildId];
  if (filters.notInCurrentPlan) parts.push("--not-in-current-plan");
  if (filters.taskIds) {
    if (filters.taskIds.length === 0) {
      throw new Error(
        "stopCommand: taskIds is present but empty. There is no command " +
          "that means 'stop nothing'; do not offer one.",
      );
    }
    for (const taskId of filters.taskIds) parts.push(`--task-id ${taskId}`);
    return parts.join(" ");
  }
  if (filters.worker) parts.push(`--worker ${filters.worker}`);
  if (filters.executor) parts.push(`--executor ${filters.executor}`);
  if (filters.olderThanSeconds) {
    parts.push(`--older-than ${formatDurationFlag(filters.olderThanSeconds)}`);
  }
  return parts.join(" ");
}
