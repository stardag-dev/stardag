/**
 * Timestamp formatting shared by list views.
 *
 * Relative time is what an operator scans ("3d ago"); the absolute time
 * belongs in a `title` next to it, because "3d ago" is useless the moment
 * anyone needs to correlate with a log.
 */

const MISSING = "—";

/** "just now" / "12m ago" / "5h ago" / "3d ago" / a locale date beyond a week. */
export function formatRelativeTime(dateString: string | null | undefined): string {
  if (!dateString) return MISSING;
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return MISSING;

  const diffSecs = Math.floor((Date.now() - date.getTime()) / 1000);
  if (diffSecs < 0) return "just now";
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffSecs < 60) return "just now";
  if (diffMins < 60) return `${diffMins}m ago`;
  if (diffHours < 24) return `${diffHours}h ago`;
  if (diffDays < 7) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

/** Full local timestamp for a `title` attribute; undefined when unknown. */
export function formatAbsoluteTime(
  dateString: string | null | undefined,
): string | undefined {
  if (!dateString) return undefined;
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return undefined;
  return date.toLocaleString();
}

/** Seconds since `dateString`, or null when it is missing/unparseable. */
export function secondsSince(dateString: string | null | undefined): number | null {
  if (!dateString) return null;
  const ms = new Date(dateString).getTime();
  if (Number.isNaN(ms)) return null;
  return Math.floor((Date.now() - ms) / 1000);
}

/** "45s" / "12m 3s" / "2h 07m" — elapsed time between two instants. */
export function formatDuration(
  startedAt: string | null | undefined,
  completedAt: string | null | undefined,
): string {
  if (!startedAt) return MISSING;
  const start = new Date(startedAt);
  if (Number.isNaN(start.getTime())) return MISSING;
  const end = completedAt ? new Date(completedAt) : new Date();
  const diffSecs = Math.max(0, Math.floor((end.getTime() - start.getTime()) / 1000));
  const diffMins = Math.floor(diffSecs / 60);
  const diffHours = Math.floor(diffMins / 60);

  if (diffSecs < 60) return `${diffSecs}s`;
  if (diffMins < 60) return `${diffMins}m ${diffSecs % 60}s`;
  return `${diffHours}h ${String(diffMins % 60).padStart(2, "0")}m`;
}

/**
 * Humanise an idleness threshold in seconds: "1h", "24h", "7d".
 * Used in filter labels and in the bulk-cleanup confirmation copy.
 */
export function formatIdleThreshold(seconds: number): string {
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}
