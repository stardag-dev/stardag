import type { TaskStatus } from "../types/task";

interface StatusBadgeProps {
  status: TaskStatus;
  muted?: boolean;
  waitingForLock?: boolean;
  lockHolderBuildId?: string;
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

export function StatusBadge({
  status,
  muted = false,
  waitingForLock = false,
  lockHolderBuildId,
}: StatusBadgeProps) {
  const styles = muted ? statusStylesMuted : statusStyles;

  const lockTooltip = lockHolderBuildId
    ? `Waiting for lock held by build ${lockHolderBuildId.slice(0, 8)}...`
    : "Waiting for global lock";

  return (
    <span
      className={`inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[status]}`}
      title={waitingForLock ? lockTooltip : undefined}
    >
      {waitingForLock && <LockIcon className="h-3 w-3" />}
      {status}
    </span>
  );
}
