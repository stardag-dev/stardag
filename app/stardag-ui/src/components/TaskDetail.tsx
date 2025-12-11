import type { Task } from "../types/task";
import { StatusBadge } from "./StatusBadge";

interface TaskDetailProps {
  task: Task;
  onClose: () => void;
}

export function TaskDetail({ task, onClose }: TaskDetailProps) {
  return (
    <div className="h-full overflow-auto bg-white dark:bg-gray-800 p-4">
      {/* Header */}
      <div className="mb-4 flex items-start justify-between">
        <div className="min-w-0 flex-1">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100 truncate">
            {task.task_family}
          </h2>
          <p className="font-mono text-sm text-gray-500 dark:text-gray-400 truncate">
            {task.task_id}
          </p>
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

        {/* Metadata */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              User
            </label>
            <p className="mt-1 text-sm text-gray-900 dark:text-gray-100">{task.user}</p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Commit
            </label>
            <p className="mt-1 font-mono text-sm text-gray-900 dark:text-gray-100">
              {task.commit_hash}
            </p>
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

        {/* Dependencies */}
        {task.dependency_ids.length > 0 && (
          <div>
            <label className="block text-sm font-medium text-gray-500 dark:text-gray-400">
              Dependencies ({task.dependency_ids.length})
            </label>
            <ul className="mt-1 space-y-1">
              {task.dependency_ids.map((depId) => (
                <li
                  key={depId}
                  className="font-mono text-sm text-gray-600 dark:text-gray-300"
                >
                  {depId.slice(0, 16)}...
                </li>
              ))}
            </ul>
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
      </div>
    </div>
  );
}
