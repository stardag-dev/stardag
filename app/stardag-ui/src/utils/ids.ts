/**
 * Abbreviating identifiers for display.
 *
 * Which end you keep depends on which end carries the difference, and the
 * two kinds of id in this UI disagree about that.
 */

/** How many hex characters an abbreviated id shows. */
export const SHORT_ID_LENGTH = 8;

/**
 * A build id, abbreviated — **from the end**.
 *
 * Build ids are UUIDv7: the leading bytes are a millisecond timestamp, so
 * every build created in the same moment shares them. A list of builds
 * seeded together showed `01a0c93c` on every single row, which is not an
 * abbreviation of anything — it identified nothing and looked like a bug.
 * The trailing bytes are the random half, so the tail is what tells two
 * builds apart.
 *
 * The leading ellipsis is load-bearing: without it this reads as a whole
 * short id rather than the end of a long one.
 */
export function shortBuildId(id: string): string {
  const compact = id.replace(/-/g, "");
  if (compact.length <= SHORT_ID_LENGTH) return id;
  return `…${compact.slice(-SHORT_ID_LENGTH)}`;
}

/**
 * A task id, abbreviated — from the start.
 *
 * Task ids are content hashes (UUID5 over the task's parameters), so
 * every character is equally discriminating and the front is as good as
 * the back. Kept leading because that is how the rest of the UI, the CLI
 * and the logs all render them, and an id you cannot match against a log
 * line by eye is worse than one abbreviated at the less useful end.
 */
export function shortTaskId(id: string): string {
  return id.slice(0, SHORT_ID_LENGTH);
}
