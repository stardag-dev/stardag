import { useEffect, useState } from "react";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import { shortTaskId } from "../utils/ids";
import { TaskDetail } from "./TaskDetail";

interface TaskPageProps {
  // Null on `/tasks`: the lookup form.
  taskId: string | null;
  onOpenTask: (taskId: string) => void;
}

/**
 * A task page keyed by `task_id`, or — without one — a lookup by id. The
 * registry serves no task list or search route, so tasks are reached from
 * a build's plan, a pasted id, or a link.
 */
export function TaskPage({ taskId, onOpenTask }: TaskPageProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();
  const [input, setInput] = useState("");

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
        <TaskDetail taskId={taskId} environmentId={activeEnvironment.id} />
      </div>
    );
  }
  return (
    <div className="mx-auto max-w-xl space-y-3 p-6">
      <form
        onSubmit={(e) => {
          e.preventDefault();
          const id = input.trim();
          if (id) onOpenTask(id);
        }}
        className="flex gap-2"
      >
        <input
          aria-label="Task id"
          placeholder="Task id…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          className="min-w-0 flex-1 rounded-md border border-gray-300 px-3 py-1.5 font-mono text-sm dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
        />
        <button
          type="submit"
          className="rounded-md bg-blue-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-blue-700"
        >
          Open
        </button>
      </form>
      <p className="text-sm text-gray-500 dark:text-gray-400">
        Open a task by its id. Tasks are listed per build, in its plan; an
        environment-wide task search is not served by this registry.
      </p>
    </div>
  );
}
