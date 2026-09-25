import type { BuildFrontier, BuildStatus, TaskStatus } from "../types/task";

export type SchedulingState =
  | "unknown"
  | "complete"
  | "progressing"
  | "waking"
  | "stalled"
  | "settled";

/** Build statuses the scheduler still acts on. */
function isLive(buildStatus: BuildStatus): boolean {
  return buildStatus === "running" || buildStatus === "pending";
}

/**
 * What the frontier says about progress.
 *
 * `stalled`: a running build whose active plan is not complete, with
 * nothing runnable, nothing running and no discovery job — the state the
 * tick reads as "cannot progress". `waking`: the same, but a wake-up is
 * queued for the build (`GET /builds/{id}/notify` reports `needs_tick`),
 * so the next tick re-evaluates it and nothing needs doing yet.
 * `complete`: a live build whose plan's members are all satisfied — the
 * next tick completes it. Anything with work in it is `progressing`; a
 * build that is no longer running is `settled`, whatever its plan says.
 */
export function schedulingState(
  frontier: BuildFrontier | null,
  buildStatus: BuildStatus,
  wakeUpPending = false,
): SchedulingState {
  if (!frontier) return "unknown";
  if (!isLive(buildStatus)) return "settled";
  if (frontier.plan_complete) return "complete";
  const idle =
    frontier.runnable.length === 0 &&
    frontier.running.length === 0 &&
    frontier.discovery_jobs.length === 0;
  if (!idle) return "progressing";
  return wakeUpPending ? "waking" : "stalled";
}

/** The order v1 listed status counts in: what needs attention first. */
export const STATUS_ORDER: TaskStatus[] = [
  "running",
  "suspended",
  "interrupted",
  "pending",
  "failed",
  "cancelled",
  "skipped",
  "completed",
];

/** Non-zero counts, in `STATUS_ORDER` (unknown statuses last). */
export function orderedCounts(
  counts: Partial<Record<string, number>>,
): [string, number][] {
  const rank = (status: string) => {
    const i = STATUS_ORDER.indexOf(status as TaskStatus);
    return i < 0 ? STATUS_ORDER.length : i;
  };
  return Object.entries(counts)
    .filter((entry): entry is [string, number] => (entry[1] ?? 0) > 0)
    .sort((a, b) => rank(a[0]) - rank(b[0]));
}
