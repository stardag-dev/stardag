import type { TaskStatus } from "../types/task";

/**
 * A task's claim, read off the task (design.md, `task`).
 *
 * The claim is **live** when the task is RUNNING and its expiry is in the
 * future; RUNNING with a past expiry is a **lapsed** claim, which the next
 * claiming start takes over. Every other status holds no claim — a
 * SUSPENDED or INTERRUPTED task included.
 */
export type ClaimState = "none" | "live" | "lapsed";

export function claimState(
  task: { status: TaskStatus; claim_expires_at: string | null },
  now: number = Date.now(),
): ClaimState {
  if (task.status !== "running") return "none";
  if (!task.claim_expires_at) return "lapsed";
  const expires = Date.parse(task.claim_expires_at);
  if (Number.isNaN(expires)) return "lapsed";
  return expires > now ? "live" : "lapsed";
}

/**
 * The two operator remedies on one task, each through a plan
 * (`/plans/{plan_id}/members/{task_id}/...`).
 *
 * - `release` cancels the task: its claim is released with outcome
 *   `cancelled`. Addressed to the plan holding the claim
 *   (`task.claim_plan_id`) — the server refuses any other with 409
 *   `not_claim_holder` — and open to any workspace member, as with the
 *   CLI. It stops nothing: the worker finds out at its next checkpoint.
 * - `retry` resets the task to PENDING. Refused on COMPLETED and on a live
 *   claim; a lapsed claim is closed first.
 */
export type ClaimAction = "release" | "retry";

export const CLAIM_ACTION_LABELS: Record<ClaimAction, string> = {
  release: "Release claim and retry",
  retry: "Reset to pending",
};

/** The remedies the server honours for a task in this state. */
export function availableClaimActions(
  status: TaskStatus,
  claim: ClaimState,
): ClaimAction[] {
  switch (status) {
    case "running":
      return claim === "live" ? ["release"] : ["release", "retry"];
    case "suspended":
    case "interrupted":
    case "failed":
    case "cancelled":
    case "skipped":
      return ["retry"];
    default:
      return [];
  }
}
