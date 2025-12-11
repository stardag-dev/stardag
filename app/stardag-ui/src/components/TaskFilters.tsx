import type { TaskStatus } from "../types/task";

interface TaskFiltersProps {
  familyFilter: string;
  onFamilyFilterChange: (value: string) => void;
  statusFilter: TaskStatus | "";
  onStatusFilterChange: (value: TaskStatus | "") => void;
}

export function TaskFilters({
  familyFilter,
  onFamilyFilterChange,
  statusFilter,
  onStatusFilterChange,
}: TaskFiltersProps) {
  return (
    <div className="flex gap-4 p-3 border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800">
      <input
        type="text"
        placeholder="Filter by task family..."
        value={familyFilter}
        onChange={(e) => onFamilyFilterChange(e.target.value)}
        className="flex-1 rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 placeholder-gray-500 dark:placeholder-gray-400 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      />
      <select
        value={statusFilter}
        onChange={(e) => onStatusFilterChange(e.target.value as TaskStatus | "")}
        className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-2 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
      >
        <option value="">All statuses</option>
        <option value="pending">Pending</option>
        <option value="running">Running</option>
        <option value="completed">Completed</option>
        <option value="failed">Failed</option>
      </select>
    </div>
  );
}
