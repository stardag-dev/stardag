const STATUS_STYLES: Record<string, string> = {
  completed: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400",
  cancelled: "bg-gray-100 text-gray-700 dark:bg-gray-900/50 dark:text-gray-400",
  exit_early:
    "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-400",
  pending: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400",
};

export function BuildStatusBadge({
  status,
  isResumed = false,
}: {
  status: string;
  // True when the build's RUNNING state was triggered by a BUILD_RESUMED
  // event (sd.build resume_build_id flow). Used to render
  // "running (resumed)" in place of plain "running".
  isResumed?: boolean;
}) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.pending;
  let label: string;
  if (status === "exit_early") {
    label = "exited early";
  } else if (status === "running" && isResumed) {
    label = "running (resumed)";
  } else {
    label = status;
  }
  const title =
    status === "running" && isResumed
      ? "Build was resumed via sd.build(resume_build_id=...)"
      : undefined;

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${style}`}
      title={title}
    >
      {label}
    </span>
  );
}
