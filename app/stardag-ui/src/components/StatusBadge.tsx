import type { TaskStatus } from "../types/task";
import { shortBuildId } from "../utils/ids";

interface StatusBadgeProps {
  status: TaskStatus;
  muted?: boolean;
  // The build whose plan holds the task's claim (`claim_build_id`), and
  // the build on screen. When they differ and `onOpenBuild` is given, the
  // badge links to the holder (v1's cross-build status link).
  holderBuildId?: string | null;
  currentBuildId?: string | null;
  onOpenBuild?: (buildId: string) => void;
}

// Skipped uses amber (warmer than pending's yellow) to be visible against
// dark-blue table rows, while staying distinct from pending.
const statusStyles: Record<TaskStatus, string> = {
  pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  suspended: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300",
  // Orange: an interruption is in the pending/skipped family (nothing is
  // wrong, nothing is done) rather than in failed's red.
  interrupted:
    "bg-orange-100 text-orange-800 dark:bg-orange-900/30 dark:text-orange-300",
  completed: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
  skipped: "bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300",
  cancelled: "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-300",
};

const statusStylesMuted: Record<TaskStatus, string> = {
  pending:
    "bg-yellow-100/50 text-yellow-800/60 dark:bg-yellow-900/20 dark:text-yellow-400/50",
  running: "bg-blue-100/50 text-blue-800/60 dark:bg-blue-900/20 dark:text-blue-400/50",
  suspended:
    "bg-purple-100/50 text-purple-800/60 dark:bg-purple-900/20 dark:text-purple-400/50",
  interrupted:
    "bg-orange-100/50 text-orange-800/60 dark:bg-orange-900/20 dark:text-orange-400/50",
  completed:
    "bg-green-100/50 text-green-800/60 dark:bg-green-900/20 dark:text-green-400/50",
  failed: "bg-red-100/50 text-red-800/60 dark:bg-red-900/20 dark:text-red-400/50",
  skipped:
    "bg-amber-100/50 text-amber-800/60 dark:bg-amber-900/20 dark:text-amber-400/50",
  cancelled:
    "bg-gray-100/50 text-gray-800/60 dark:bg-gray-900/20 dark:text-gray-400/50",
};

function ExternalLinkIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
      />
    </svg>
  );
}

/**
 * A task's global status. The status is the completion's, environment
 * wide: it is the same in every build whose plan holds the task. What can
 * belong to another build is the **claim**: a running task is held by one
 * build (`claim_build_id`), and when that is not the build on screen the
 * badge says so and — given `onOpenBuild` — jumps there (role=button,
 * Enter/Space), as v1's did.
 */
export function StatusBadge({
  status,
  muted = false,
  holderBuildId,
  currentBuildId,
  onOpenBuild,
}: StatusBadgeProps) {
  const styles = muted ? statusStylesMuted : statusStyles;
  const otherHolder =
    status === "running" && holderBuildId && holderBuildId !== currentBuildId
      ? holderBuildId
      : null;
  const clickable = Boolean(otherHolder && onOpenBuild);
  const open = (e: React.SyntheticEvent) => {
    // Never also select the row or node the badge sits in.
    e.stopPropagation();
    if (otherHolder && onOpenBuild) onOpenBuild(otherHolder);
  };
  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${
        styles[status] ?? statusStyles.pending
      } ${
        clickable
          ? "cursor-pointer hover:ring-2 hover:ring-blue-400 hover:ring-offset-1 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:hover:ring-offset-gray-800"
          : ""
      }`}
      title={
        otherHolder
          ? `${status} under build ${shortBuildId(otherHolder)}${
              clickable ? " (click to view)" : ""
            }`
          : undefined
      }
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onClick={clickable ? open : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                open(e);
              }
            }
          : undefined
      }
    >
      {otherHolder && <ExternalLinkIcon className="h-3 w-3" />}
      {status}
    </span>
  );
}
