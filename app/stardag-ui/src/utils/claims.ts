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
    // Retryable, and deliberately NOT releasable: an interrupted task
    // holds no claim to release — the platform ended its execution and
    // the server cleared the claim with it.
    case "interrupted":
      return ["retry"];
    default:
      return [];
  }
}

/** A task whose global status is one of these is holding an execution claim. */
export const CLAIM_HOLDING_STATUSES: TaskStatus[] = ["running", "suspended"];

export type SchedulingPanelForm = "hidden" | "collapsed" | "stalled" | "satisfied";

/**
 * Whether every root this build asked for is now complete.
 *
 * A build is a *request for a set of root tasks to be materialised*. If the
 * roots are complete then so is everything they needed, and the request has
 * been satisfied — whatever the build's own status says, and whichever build
 * actually ran the tasks. Tasks are content-addressed and shared, so a build
 * can fail waiting on a task that another build completes an hour later. Its
 * status stays `failed` (an honest record of what its own driver decided) while
 * this becomes true.
 *
 * **Three clauses, and each excludes a real state:**
 *
 * - a non-empty root list — enforced in `rootsSatisfiedFrom` below, since that
 *   is where `every()` is called and where the vacuous-truth trap lives. A build
 *   with no roots has nothing to satisfy. This is not a corner case:
 *   `POST /builds` defaults `root_task_ids` to `[]`, so *every* build passes
 *   through rootless between being minted and having its roots registered,
 *   which is exactly what `build_trigger` does. Without this clause a build
 *   mid-creation would report its request satisfied.
 * - `roots.length === root_task_ids.length` — the server reports `roots` by
 *   looking the ids up, so a shorter list means some root is not resolvable
 *   (deleted, or in another environment). Unknown is not complete. Mirrors the
 *   SDK's own `roots_known` in `_handle_terminal`.
 * - `every(completed)` — the actual question.
 */
export function rootsSatisfied(frontier: BuildFrontier): boolean {
  const statusById = new Map(frontier.roots.map((r) => [r.task_id, r.latest_status]));
  return (
    frontier.roots.length === frontier.root_task_ids.length &&
    rootsSatisfiedFrom(frontier.root_task_ids, (id) => statusById.get(id))
  );
}

/**
 * The same rule against any source of task statuses.
 *
 * Exists so the two places that need it cannot drift: the scheduling panel asks
 * it of a `BuildFrontier`, while the build view asks it of the task list it has
 * already fetched — and a build being told "your work is done" by one component
 * and "you need intervention" by the other would be worse than either message
 * alone.
 *
 * An unresolvable root answers `undefined`, which is not `"completed"`, so
 * unknown counts as not satisfied without a special case.
 */
export function rootsSatisfiedFrom(
  rootTaskIds: string[],
  statusOf: (taskId: string) => TaskStatus | undefined,
): boolean {
  if (rootTaskIds.length === 0) return false;
  return rootTaskIds.every((id) => statusOf(id) === "completed");
}

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
 * `"satisfied"` short-circuits all of that. A build whose roots are complete
 * is not stalled and never needs intervention, whatever its status — so it must
 * not be told it does. This is the shape that motivated the form: a build fails
 * waiting on a shared task, another build completes the task, and the panel goes
 * on insisting "nothing is going to happen without intervention" over work that
 * is finished. It applies to `running` too, and there the copy differs: nothing
 * is wrong, the build simply has not been ticked since the roots landed.
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

  // Before any stalled reasoning: if the request is satisfied there is nothing
  // to diagnose. Checked ahead of `stalledShape` on purpose — a satisfied build
  // has no actionable and no running tasks by definition, so it always *looks*
  // stalled.
  //
  // `completed` and `cancelled` are excluded, and for the same reason the
  // stalled rule below excludes them: neither is a build anyone needs told
  // anything. `completed` already says it. `cancelled` was stopped on purpose —
  // announcing that its roots finished anyway is noise, and the form's advice to
  // re-trigger and reconcile the record actively contradicts the user's
  // intent. Both simply fall through to the quiet strip, which is what they got
  // before this form existed.
  const deliberatelyTerminal =
    buildStatus === "completed" || buildStatus === "cancelled";
  if (!deliberatelyTerminal && rootsSatisfied(frontier)) {
    return "satisfied";
  }

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
