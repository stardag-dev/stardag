import type { Task } from "../types/task";
import { StatusBadge } from "./StatusBadge";

interface TaskTableProps {
  tasks: Task[];
  loading: boolean;
  error: string | null;
  selectedTaskId: string | null;
  onSelectTask: (task: Task) => void;
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

export function TaskTable({
  tasks,
  loading,
  error,
  selectedTaskId,
  onSelectTask,
  page,
  pageSize,
  total,
  totalPages,
  onPageChange,
}: TaskTableProps) {
  return (
    <div className="flex h-full flex-col overflow-hidden">
      {/* Error state */}
      {error && (
        <div className="m-3 rounded-md bg-red-50 dark:bg-red-900/20 p-4 text-red-700 dark:text-red-400">
          {error}
        </div>
      )}

      {/* Loading state */}
      {loading && (
        <div className="flex-1 flex items-center justify-center text-gray-500 dark:text-gray-400">
          Loading tasks...
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && tasks.length === 0 && (
        <div className="flex-1 flex items-center justify-center text-gray-500 dark:text-gray-400">
          No tasks found
        </div>
      )}

      {/* Task table */}
      {!loading && tasks.length > 0 && (
        <div className="flex-1 overflow-auto">
          <table className="min-w-full divide-y divide-gray-200 dark:divide-gray-700">
            <thead className="bg-gray-50 dark:bg-gray-800 sticky top-0">
              <tr>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Task Family
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Task ID
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Status
                </th>
                <th className="px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  Created
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 dark:divide-gray-700 bg-white dark:bg-gray-900">
              {tasks.map((task) => (
                <tr
                  key={task.task_id}
                  onClick={() => onSelectTask(task)}
                  className={`cursor-pointer transition-colors ${
                    selectedTaskId === task.task_id
                      ? "bg-blue-50 dark:bg-blue-900/20"
                      : "hover:bg-gray-50 dark:hover:bg-gray-800"
                  }`}
                >
                  <td className="px-4 py-3 text-sm font-medium text-gray-900 dark:text-gray-100">
                    {task.task_family}
                  </td>
                  <td className="px-4 py-3 font-mono text-sm text-gray-500 dark:text-gray-400">
                    {task.task_id.slice(0, 12)}...
                  </td>
                  <td className="px-4 py-3">
                    <StatusBadge status={task.status} />
                  </td>
                  <td className="px-4 py-3 text-sm text-gray-500 dark:text-gray-400">
                    {new Date(task.created_at).toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Pagination */}
      {totalPages > 1 && (
        <div className="flex items-center justify-between border-t border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-2">
          <span className="text-sm text-gray-500 dark:text-gray-400">
            {(page - 1) * pageSize + 1}-{Math.min(page * pageSize, total)} of {total}
          </span>
          <div className="flex gap-2">
            <button
              onClick={() => onPageChange(Math.max(1, page - 1))}
              disabled={page === 1}
              className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1 text-sm text-gray-700 dark:text-gray-200 disabled:opacity-50 hover:bg-gray-50 dark:hover:bg-gray-600"
            >
              Prev
            </button>
            <button
              onClick={() => onPageChange(Math.min(totalPages, page + 1))}
              disabled={page === totalPages}
              className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1 text-sm text-gray-700 dark:text-gray-200 disabled:opacity-50 hover:bg-gray-50 dark:hover:bg-gray-600"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
