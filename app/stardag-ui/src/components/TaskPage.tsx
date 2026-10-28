import { useEffect } from "react";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import { shortTaskId } from "../utils/ids";
import type { CompareTarget } from "../utils/taskDiff";
import { EMPTY_SEARCH, formatSearch, withTag } from "../utils/taskSearch";
import { TaskDetail } from "./TaskDetail";
import { TaskSearch } from "./TaskSearch";

interface TaskPageProps {
  // Null on `/tasks`: the search.
  taskId: string | null;
  onOpenTask: (taskId: string) => void;
  onOpenBuild?: (buildId: string) => void;
  // Open the task search at a query string (`?tag=...`).
  onSearchTasks?: (query: string) => void;
  // Open `/task-diff` with a task's instance on the left.
  onCompareTask?: (target: CompareTarget) => void;
}

/**
 * A task page keyed by `task_id`, or — without one — the task search
 * (name, namespace, tags, id prefix, free text). Search over task
 * parameters is the explorer's, a later route.
 */
export function TaskPage({
  taskId,
  onOpenTask,
  onOpenBuild,
  onSearchTasks,
  onCompareTask,
}: TaskPageProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();

  useEffect(() => {
    setBreadcrumb(
      taskId
        ? [
            { label: "Tasks", onClick: () => onOpenTask("") },
            { label: shortTaskId(taskId), title: taskId },
          ]
        : [{ label: "Tasks" }],
    );
    return () => setBreadcrumb([]);
  }, [taskId, onOpenTask, setBreadcrumb]);

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        Select an environment to view tasks
      </div>
    );
  }
  if (taskId) {
    return (
      <div className="mx-auto h-full max-w-4xl">
        <TaskDetail
          taskId={taskId}
          environmentId={activeEnvironment.id}
          onOpenBuild={onOpenBuild}
          onCompare={onCompareTask}
          onSearchTag={
            onSearchTasks
              ? (tag) => onSearchTasks(formatSearch(withTag(EMPTY_SEARCH, tag)))
              : undefined
          }
        />
      </div>
    );
  }
  // Keyed: a late read of the previous environment (the autocomplete seed
  // included) can never land in the new one's search.
  return (
    <TaskSearch
      key={activeEnvironment.id}
      environmentId={activeEnvironment.id}
      onOpenTask={onOpenTask}
    />
  );
}
