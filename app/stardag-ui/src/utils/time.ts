/**
 * Timestamp formatting shared by list views.
 *
 * Relative time is what an operator scans ("3d ago"); the absolute time
 * belongs in a `title` next to it, because "3d ago" is useless the moment
 * anyone needs to correlate with a log.
 */

const MISSING = "—";

const pad = (n: number) => String(n).padStart(2, "0");

/**
 * `YYYY-MM-DD` in the **viewer's** timezone.
 *
 * Not `toISOString().slice(0, 10)`, which is UTC and would show the wrong
 * day for anyone west of Greenwich for part of their evening. Not
 * `toLocaleDateString()` either: that renders `8/9/2026` or `9/8/2026`
 * depending on the browser's locale, and a timestamp whose meaning depends
 * on who is reading it is worse than useless when two people compare
 * notes on the same incident.
 */
function isoDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

/** "just now" / "12m ago" / "5h ago" / "3d ago" / "2026-08-09" beyond a week. */
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
  return isoDate(date);
}

/** "2026-08-09 14:32:07" — full local timestamp for a `title` attribute. */
export function formatAbsoluteTime(
  dateString: string | null | undefined,
): string | undefined {
  if (!dateString) return undefined;
  const date = new Date(dateString);
  if (Number.isNaN(date.getTime())) return undefined;
  return `${isoDate(date)} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(
    date.getSeconds(),
  )}`;
}

/** Seconds since `dateString`, or null when it is missing/unparseable. */
export function secondsSince(dateString: string | null | undefined): number | null {
  if (!dateString) return null;
  const ms = new Date(dateString).getTime();
  if (Number.isNaN(ms)) return null;
  return Math.floor((Date.now() - ms) / 1000);
}

/** "45s" / "12m 3s" / "2h 07m" / "15d 11h 41m" — elapsed time. */
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
  if (diffHours < 24) return `${diffHours}h ${pad(diffMins % 60)}m`;
  // Past a day, hours alone stop being readable: a build abandoned two
  // weeks ago reads "371h 41m", which nobody converts in their head.
  return `${Math.floor(diffHours / 24)}d ${diffHours % 24}h ${pad(diffMins % 60)}m`;
}

/**
 * Humanise an idleness threshold in seconds: "1h", "24h", "7d".
 * Used in filter summaries and in the bulk-cleanup confirmation copy.
 *
 * Hours are kept up to two days so a threshold the user picked as
 * "24 hours" is not echoed back at them as "1d".
 */
export function formatIdleThreshold(seconds: number): string {
  if (seconds % 3600 === 0 && seconds < 48 * 3600) return `${seconds / 3600}h`;
  if (seconds % 86400 === 0) return `${seconds / 86400}d`;
  if (seconds % 3600 === 0) return `${seconds / 3600}h`;
  if (seconds % 60 === 0) return `${seconds / 60}m`;
  return `${seconds}s`;
}
