const STATUS_STYLES: Record<string, string> = {
  completed: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400",
  cancelled: "bg-gray-100 text-gray-700 dark:bg-gray-900/50 dark:text-gray-400",
  exit_early:
    "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-400",
  pending: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400",
};

export function BuildStatusBadge({ status }: { status: string }) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.pending;
  const label = status === "exit_early" ? "exited early" : status;

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${style}`}
    >
      {label}
    </span>
  );
}
