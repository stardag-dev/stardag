import type { BuildFrontier, BuildStatus, TaskStatus } from "../types/task";

/**
 * The two ways a stuck task gets unstuck.
 *
 * - `release` cancels it, freeing the execution claim and any
 *   concurrency-limit slot it holds. The remedy for a task left RUNNING.
 * - `retry` resets it to pending so it can be scheduled again. The remedy
 *   for a task left failed / cancelled / skipped / suspended. It does
 *   nothing to a RUNNING task by design — that would invite a second,
 *   concurrent execution of the same task.
 */
export type ClaimAction = "release" | "retry";

export const CLAIM_ACTION_LABELS: Record<ClaimAction, string> = {
  release: "Release claim",
  retry: "Reset to pending",
};

/**
 * Which remedies the server will actually honour for a given status.
 *
 * Offering a button the server would treat as a no-op is worse than
 * offering nothing, so this mirrors the API's own rules: COMPLETED is
 * sticky, RUNNING is cancel-only, and PENDING/UNREGISTERED have nothing
 * to release and nothing a reset would change.
 */
export function availableClaimActions(status: TaskStatus): ClaimAction[] {
  switch (status) {
    case "running":
      return ["release"];
    case "suspended":
      return ["release", "retry"];
    case "failed":
    case "cancelled":
    case "skipped":
      return ["retry"];
    default:
      return [];
  }
}

/** A task whose global status is one of these is holding an execution claim. */
export const CLAIM_HOLDING_STATUSES: TaskStatus[] = ["running", "suspended"];

export type SchedulingPanelForm = "hidden" | "collapsed" | "stalled";

/**
 * When — and how loudly — the build scheduling panel has something to say.
 *
 * `"stalled"` (full, prominent) requires the build to have tasks, nothing
 * actionable and nothing running. That is not an arbitrary heuristic: it
 * is *exactly* the condition under which the server computes
 * `blocked_by_external`, and exactly the condition the SDK reads as "this
 * build cannot progress" — the read that fails a build with "No runnable
 * or running tasks left but roots are not complete" even when an upstream
 * owned by another build is legitimately holding it up.
 *
 * Terminal states are qualified rather than blanket-included. Note this
 * whole paragraph is about the **`"stalled"`** form only — a terminal
 * reactive build still gets the quiet `"collapsed"` strip, which is the
 * one route to its tick trail and says nothing about blockers:
 *
 * - `completed` / `cancelled`: never *stalled*. The build is not "not
 *   progressing", it is done, or a user stopped it on purpose.
 * - `failed`: only when the failure has the stuck-build shape — an
 *   external blocker was reported, or the build failed while *nothing in
 *   it failed*. An ordinary DAG failure (a task raised) explains itself
 *   in the task table and does not need a scheduler post-mortem.
 *
 * `"collapsed"` is a single quiet line for a reactive build the rule does
 * not flag: the live counts, plus access to the tick trail, which exists
 * nowhere else in the UI. `"hidden"` is everything else — a healthy
 * non-reactive build has no scheduler to reason about, and a permanently
 * empty box is worse than no box.
 */
export function schedulingPanelForm(
  frontier: BuildFrontier,
  buildStatus: BuildStatus,
): SchedulingPanelForm {
  const totalTasks = Object.values(frontier.status_counts).reduce((a, b) => a + b, 0);
  const stalledShape =
    totalTasks > 0 && frontier.actionable.length === 0 && frontier.running.length === 0;

  let stalled = false;
  if (stalledShape) {
    if (buildStatus === "completed" || buildStatus === "cancelled") {
      stalled = false;
    } else if (buildStatus === "failed") {
      stalled =
        frontier.blocked_by_external.length > 0 ||
        (frontier.status_counts.failed ?? 0) === 0;
    } else {
      stalled = true;
    }
  }

  if (stalled) return "stalled";
  return frontier.reactive_app_name ? "collapsed" : "hidden";
}
