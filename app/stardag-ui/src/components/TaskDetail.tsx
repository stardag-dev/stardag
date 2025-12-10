import { useCallback, useEffect, useState } from "react";
import { fetchTask, fetchTasks } from "../api/tasks";
import type { Task } from "../types/task";
import { DagGraph } from "./DagGraph";
import { StatusBadge } from "./StatusBadge";

interface TaskDetailProps {
  task: Task;
  onClose: () => void;
  onTaskSelect: (task: Task) => void;
}

export function TaskDetail({ task, onClose, onTaskSelect }: TaskDetailProps) {
  const [relatedTasks, setRelatedTasks] = useState<Task[]>([task]);
  const [loadingDag, setLoadingDag] = useState(false);

  // Load all related tasks (dependencies and dependents) recursively
  useEffect(() => {
    const loadRelatedTasks = async () => {
      setLoadingDag(true);
      const taskMap = new Map<string, Task>();
      taskMap.set(task.task_id, task);

      const toLoad = new Set<string>(task.dependency_ids);
      const loaded = new Set<string>([task.task_id]);

      // Also find tasks that depend on this one (load all tasks and filter)
      try {
        const allTasksResponse = await fetchTasks({ page_size: 100 });
        const allTasks = allTasksResponse.tasks;

        // Add all tasks to the map
        allTasks.forEach((t) => taskMap.set(t.task_id, t));

        // Find dependents (tasks that have this task in their dependency_ids)
        allTasks.forEach((t) => {
          if (t.dependency_ids.includes(task.task_id)) {
            toLoad.add(t.task_id);
          }
        });

        // Load any dependencies not yet in allTasks
        while (toLoad.size > 0) {
          const taskId = toLoad.values().next().value as string;
          toLoad.delete(taskId);

          if (loaded.has(taskId)) continue;
          loaded.add(taskId);

          if (!taskMap.has(taskId)) {
            try {
              const loadedTask = await fetchTask(taskId);
              taskMap.set(taskId, loadedTask);
            } catch {
              // Task might not exist in DB, skip
              continue;
            }
          }

          const loadedTask = taskMap.get(taskId);
          if (loadedTask) {
            loadedTask.dependency_ids.forEach((depId) => {
              if (!loaded.has(depId)) toLoad.add(depId);
            });
          }
        }

        setRelatedTasks(Array.from(taskMap.values()));
      } catch (err) {
        console.error("Failed to load related tasks:", err);
        setRelatedTasks([task]);
      } finally {
        setLoadingDag(false);
      }
    };

    loadRelatedTasks();
  }, [task]);

  const handleDagTaskClick = useCallback(
    (taskId: string) => {
      const clickedTask = relatedTasks.find((t) => t.task_id === taskId);
      if (clickedTask) {
        onTaskSelect(clickedTask);
      }
    },
    [relatedTasks, onTaskSelect],
  );

  return (
    <div className="space-y-4">
      {/* DAG Graph */}
      <div className="rounded-lg border border-gray-200 bg-white p-4">
        <h3 className="mb-3 text-sm font-medium text-gray-700">DAG View</h3>
        {loadingDag ? (
          <div className="flex h-64 items-center justify-center text-gray-500">
            Loading DAG...
          </div>
        ) : (
          <DagGraph
            tasks={relatedTasks}
            selectedTaskId={task.task_id}
            onTaskClick={handleDagTaskClick}
          />
        )}
      </div>

      {/* Task Details */}
      <div className="rounded-lg border border-gray-200 bg-white p-6">
        <div className="mb-4 flex items-start justify-between">
          <div>
            <h2 className="text-lg font-semibold text-gray-900">{task.task_family}</h2>
            <p className="font-mono text-sm text-gray-500">{task.task_id}</p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-500">
            <span className="sr-only">Close</span>
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              strokeWidth="1.5"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        <div className="space-y-4">
          {/* Status */}
          <div>
            <label className="block text-sm font-medium text-gray-500">Status</label>
            <div className="mt-1">
              <StatusBadge status={task.status} />
            </div>
          </div>

          {/* Metadata */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-500">User</label>
              <p className="mt-1 text-sm text-gray-900">{task.user}</p>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-500">Commit</label>
              <p className="mt-1 font-mono text-sm text-gray-900">{task.commit_hash}</p>
            </div>
          </div>

          {/* Timestamps */}
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-500">Created</label>
              <p className="mt-1 text-sm text-gray-900">
                {new Date(task.created_at).toLocaleString()}
              </p>
            </div>
            {task.started_at && (
              <div>
                <label className="block text-sm font-medium text-gray-500">
                  Started
                </label>
                <p className="mt-1 text-sm text-gray-900">
                  {new Date(task.started_at).toLocaleString()}
                </p>
              </div>
            )}
            {task.completed_at && (
              <div>
                <label className="block text-sm font-medium text-gray-500">
                  Completed
                </label>
                <p className="mt-1 text-sm text-gray-900">
                  {new Date(task.completed_at).toLocaleString()}
                </p>
              </div>
            )}
          </div>

          {/* Error message */}
          {task.error_message && (
            <div>
              <label className="block text-sm font-medium text-gray-500">Error</label>
              <pre className="mt-1 overflow-auto rounded-md bg-red-50 p-3 text-sm text-red-700">
                {task.error_message}
              </pre>
            </div>
          )}

          {/* Dependencies */}
          {task.dependency_ids.length > 0 && (
            <div>
              <label className="block text-sm font-medium text-gray-500">
                Dependencies ({task.dependency_ids.length})
              </label>
              <ul className="mt-1 space-y-1">
                {task.dependency_ids.map((depId) => (
                  <li key={depId} className="font-mono text-sm text-gray-600">
                    {depId.slice(0, 12)}...
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* Task data */}
          <div>
            <label className="block text-sm font-medium text-gray-500">
              Task Parameters
            </label>
            <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-gray-50 p-3 text-sm">
              {JSON.stringify(task.task_data, null, 2)}
            </pre>
          </div>
        </div>
      </div>
    </div>
  );
}
