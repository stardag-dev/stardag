import type { TaskStatus } from "../types/task";

interface StatusBadgeProps {
  status: TaskStatus;
  muted?: boolean;
  waitingForLock?: boolean;
  // Build where the status-determining event occurred
  statusBuildId?: string;
  // Current build being viewed (to detect cross-build status)
  currentBuildId?: string;
  // Callback when clicking on a cross-build status badge
  onStatusBuildClick?: (buildId: string) => void;
}

const statusStyles: Record<TaskStatus, string> = {
  pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  suspended: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300",
  completed: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
  cancelled: "bg-gray-100 text-gray-800 dark:bg-gray-900/30 dark:text-gray-300",
};

const statusStylesMuted: Record<TaskStatus, string> = {
  pending:
    "bg-yellow-100/50 text-yellow-800/60 dark:bg-yellow-900/20 dark:text-yellow-400/50",
  running: "bg-blue-100/50 text-blue-800/60 dark:bg-blue-900/20 dark:text-blue-400/50",
  suspended:
    "bg-purple-100/50 text-purple-800/60 dark:bg-purple-900/20 dark:text-purple-400/50",
  completed:
    "bg-green-100/50 text-green-800/60 dark:bg-green-900/20 dark:text-green-400/50",
  failed: "bg-red-100/50 text-red-800/60 dark:bg-red-900/20 dark:text-red-400/50",
  cancelled:
    "bg-gray-100/50 text-gray-800/60 dark:bg-gray-900/20 dark:text-gray-400/50",
};

function LockIcon({ className }: { className?: string }) {
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
        d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z"
      />
    </svg>
  );
}

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

export function StatusBadge({
  status,
  muted = false,
  waitingForLock = false,
  statusBuildId,
  currentBuildId,
  onStatusBuildClick,
}: StatusBadgeProps) {
  const styles = muted ? statusStylesMuted : statusStyles;

  // Check if status is from a different build
  const isFromOtherBuild =
    statusBuildId && currentBuildId && statusBuildId !== currentBuildId;

  const isClickable = isFromOtherBuild && onStatusBuildClick;

  let tooltip: string | undefined;
  if (waitingForLock) {
    tooltip = "Waiting for global lock";
  } else if (isFromOtherBuild) {
    tooltip = `${status} in build ${statusBuildId.slice(0, 8)}...${
      isClickable ? " (click to view)" : ""
    }`;
  }

  const handleClick = (e: React.MouseEvent) => {
    // Stop propagation to prevent node click in DAG view
    e.stopPropagation();
    if (isClickable && statusBuildId) {
      onStatusBuildClick(statusBuildId);
    }
  };

  const baseClasses = `inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[status]}`;
  const clickableClasses = isClickable
    ? "cursor-pointer hover:ring-2 hover:ring-offset-1 hover:ring-blue-400 dark:hover:ring-offset-gray-800"
    : "";

  return (
    <span
      className={`${baseClasses} ${clickableClasses}`}
      title={tooltip}
      onClick={isClickable ? handleClick : undefined}
      role={isClickable ? "button" : undefined}
      tabIndex={isClickable ? 0 : undefined}
      onKeyDown={
        isClickable
          ? (e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.stopPropagation();
                if (statusBuildId) {
                  onStatusBuildClick(statusBuildId);
                }
              }
            }
          : undefined
      }
    >
      {waitingForLock && <LockIcon className="h-3 w-3" />}
      {isFromOtherBuild && !waitingForLock && <ExternalLinkIcon className="h-3 w-3" />}
      {status}
    </span>
  );
}
