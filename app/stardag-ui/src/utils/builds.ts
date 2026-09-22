import type { BuildStatus } from "../types/task";

/**
 * Statuses whose recorded outcome may still be overridden.
 *
 * A finished record is a decision already taken, and re-deciding one
 * from a toolbar is not something to do by accident. Note this is about
 * the *record* only: a failed or cancelled build may well still have
 * containers running, which is a separate question with a separate
 * control — see `BuildControlsDialog`.
 */
export function canOverrideStatus(status: BuildStatus): boolean {
  return status === "running" || status === "pending" || status === "exit_early";
}
