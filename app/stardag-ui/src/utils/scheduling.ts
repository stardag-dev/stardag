import type { BuildFrontier, BuildStatus } from "../types/task";

export type SchedulingState =
  | "unknown"
  | "complete"
  | "progressing"
  | "stalled"
  | "settled";

/**
 * What the frontier says about progress.
 *
 * `stalled`: a running build whose active plan is not complete, with
 * nothing runnable, nothing running and no discovery job — the state the
 * tick reads as "cannot progress". `complete`: the plan's members are all
 * satisfied. Anything with work in it is `progressing`; a build that is
 * no longer running with nothing left to do is `settled`.
 */
export function schedulingState(
  frontier: BuildFrontier | null,
  buildStatus: BuildStatus,
): SchedulingState {
  if (!frontier) return "unknown";
  if (frontier.plan_complete) return "complete";
  const idle =
    frontier.runnable.length === 0 &&
    frontier.running.length === 0 &&
    frontier.discovery_jobs.length === 0;
  if (!idle) return "progressing";
  return buildStatus === "running" ? "stalled" : "settled";
}
