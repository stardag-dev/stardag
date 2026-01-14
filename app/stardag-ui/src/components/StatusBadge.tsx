import type { TaskStatus } from "../types/task";

interface StatusBadgeProps {
  status: TaskStatus;
  muted?: boolean;
}

const statusStyles: Record<TaskStatus, string> = {
  pending: "bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300",
  running: "bg-blue-100 text-blue-800 dark:bg-blue-900/30 dark:text-blue-300",
  suspended: "bg-purple-100 text-purple-800 dark:bg-purple-900/30 dark:text-purple-300",
  completed: "bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300",
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
};

export function StatusBadge({ status, muted = false }: StatusBadgeProps) {
  const styles = muted ? statusStylesMuted : statusStyles;
  return (
    <span
      className={`inline-flex items-center rounded-full px-2.5 py-0.5 text-xs font-medium ${styles[status]}`}
    >
      {status}
    </span>
  );
}
