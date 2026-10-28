import { Tooltip } from "./ui/Tooltip";

const STATUS_STYLES: Record<string, string> = {
  completed: "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400",
  failed: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400",
  running: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400",
  cancelled: "bg-gray-100 text-gray-700 dark:bg-gray-900/50 dark:text-gray-400",
  exit_early:
    "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-400",
  pending: "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400",
  registered: "bg-teal-100 text-teal-700 dark:bg-teal-900/50 dark:text-teal-400",
};

export function BuildStatusBadge({
  status,
  isResumed = false,
  planOnly = false,
}: {
  status: string;
  // True when the build's RUNNING state was triggered by a BUILD_RESUMED
  // event (sd.build resume_build_id flow). Used to render
  // "running (resumed)" in place of plain "running".
  isResumed?: boolean;
  // A plan-only build (sd.register): registers and runs nothing. Created
  // pending and never running; one still pending did not finish
  // registering (its driver died, or the build was never marked).
  planOnly?: boolean;
}) {
  const style = STATUS_STYLES[status] ?? STATUS_STYLES.pending;
  let label: string;
  let title: string | undefined;
  if (status === "exit_early") {
    label = "exited early";
  } else if (status === "running" && isResumed) {
    label = "running (resumed)";
    title = "Build was resumed via sd.build(resume_build_id=...)";
  } else if (status === "registered") {
    label = "registered";
    title = "Plan-only build: its tasks were registered, nothing ran";
  } else if (status === "pending" && planOnly) {
    label = "registration incomplete";
    title =
      "Plan-only build that never finished registering. Nothing runs from it; " +
      "fail or cancel it, and register again.";
  } else {
    label = status;
  }

  return (
    <Tooltip content={title}>
      <span
        className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${style}`}
      >
        {label}
      </span>
    </Tooltip>
  );
}
