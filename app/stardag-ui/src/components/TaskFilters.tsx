import type { TaskStatus } from "../types/task";

interface TaskFiltersProps {
  nameFilter: string;
  onNameFilterChange: (value: string) => void;
  statusFilter: TaskStatus | "";
  onStatusFilterChange: (value: TaskStatus | "") => void;
}

export function TaskFilters({
  nameFilter,
  onNameFilterChange,
  statusFilter,
  onStatusFilterChange,
}: TaskFiltersProps) {
  return (
    <>
      <input
        type="text"
        placeholder="Filter by task name..."
        value={nameFilter}
        onChange={(e) => onNameFilterChange(e.target.value)}
        className="min-w-0 flex-1 rounded-md border border-gray-300 px-3 py-1 text-xs text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
      />
      <select
        value={statusFilter}
        onChange={(e) => onStatusFilterChange(e.target.value as TaskStatus | "")}
        className="rounded-md border border-gray-300 px-3 py-1 text-xs text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100"
      >
        <option value="">All statuses</option>
        <option value="pending">Pending</option>
        <option value="running">Running</option>
        <option value="suspended">Suspended</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
      </select>
    </>
  );
}
