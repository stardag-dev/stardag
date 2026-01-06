import { useEffect, useState } from "react";
import { fetchTaskAssets } from "../api/tasks";
import type { Task, TaskAsset } from "../types/task";
import { AssetList } from "./AssetViewer";
import { StatusBadge } from "./StatusBadge";

interface TaskDetailProps {
  task: Task;
  onClose: () => void;
}

export function TaskDetail({ task, onClose }: TaskDetailProps) {
  const [assets, setAssets] = useState<TaskAsset[]>([]);
  const [assetsLoading, setAssetsLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;

    async function loadAssets() {
      setAssetsLoading(true);
      try {
        const response = await fetchTaskAssets(task.task_id);
        if (!cancelled) {
          setAssets(response.assets);
        }
      } catch (error) {
        console.error("Failed to load task assets:", error);
        if (!cancelled) {
          setAssets([]);
        }
      } finally {
        if (!cancelled) {
          setAssetsLoading(false);
        }
      }
    }

    loadAssets();

    return () => {
      cancelled = true;
    };
  }, [task.task_id]);

  return (
    <div className="h-full overflow-auto bg-white dark:bg-gray-800 p-4">
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
            {task.task_name}
          </h2>
          <p className="font-mono text-sm text-gray-500 dark:text-gray-400 truncate">
            {task.task_id}
          </p>
          {task.task_namespace && (
            <p className="text-sm text-gray-500 dark:text-gray-400">
              Namespace: {task.task_namespace}
            </p>
          )}
        </div>
        <button
          onClick={onClose}
          className="ml-2 text-gray-400 hover:text-gray-500 dark:hover:text-gray-300"
        >
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
          <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
            Status
          </label>
          <div className="mt-1">
            <StatusBadge status={task.status} />
          </div>
        </div>

        {/* Timestamps */}
        <div className="space-y-2">
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Created
            </label>
            <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
              {new Date(task.created_at).toLocaleString()}
            </p>
          </div>
          {task.started_at && (
            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
                Started
              </label>
              <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
                {new Date(task.started_at).toLocaleString()}
              </p>
            </div>
          )}
          {task.completed_at && (
            <div>
              <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
                Completed
              </label>
              <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">
                {new Date(task.completed_at).toLocaleString()}
              </p>
            </div>
          )}
        </div>

        {/* Error message */}
        {task.error_message && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Error
            </label>
            <pre className="mt-1 overflow-auto rounded-md bg-red-50 dark:bg-red-900/20 p-3 text-sm text-red-700 dark:text-red-400">
              {task.error_message}
            </pre>
          </div>
        )}

        {/* Task data */}
        <div>
          <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
            Task Parameters
          </label>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-gray-50 dark:bg-gray-900 p-3 text-sm text-gray-800 dark:text-gray-200">
            {JSON.stringify(task.task_data, null, 2)}
          </pre>
        </div>

        {/* Assets */}
        {assetsLoading ? (
          <div className="text-sm text-gray-500 dark:text-gray-400">
            Loading assets...
          </div>
        ) : (
          <AssetList assets={assets} />
        )}
      </div>
    </div>
  );
}
