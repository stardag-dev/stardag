import { useEffect, useState, useCallback } from "react";
import { fetchTasks, type TaskFilters } from "../api/tasks";
import type { Task, TaskStatus } from "../types/task";

interface UseTasksReturn {
  tasks: Task[];
  total: number;
  page: number;
  setPage: (page: number) => void;
  loading: boolean;
  error: string | null;
  familyFilter: string;
  setFamilyFilter: (filter: string) => void;
  statusFilter: TaskStatus | "";
  setStatusFilter: (status: TaskStatus | "") => void;
  pageSize: number;
  totalPages: number;
  refresh: () => void;
}

export function useTasks(pageSize = 20): UseTasksReturn {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [familyFilter, setFamilyFilter] = useState("");
  const [statusFilter, setStatusFilter] = useState<TaskStatus | "">("");
  const [refreshKey, setRefreshKey] = useState(0);

  const loadTasks = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const filters: TaskFilters = {
        page,
        page_size: pageSize,
      };
      if (familyFilter) filters.task_family = familyFilter;
      if (statusFilter) filters.status = statusFilter;

      const response = await fetchTasks(filters);
      setTasks(response.tasks);
      setTotal(response.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setLoading(false);
    }
  }, [page, pageSize, familyFilter, statusFilter, refreshKey]);

  useEffect(() => {
    loadTasks();
  }, [loadTasks]);

  const handleSetFamilyFilter = useCallback((filter: string) => {
    setFamilyFilter(filter);
    setPage(1);
  }, []);

  const handleSetStatusFilter = useCallback((status: TaskStatus | "") => {
    setStatusFilter(status);
    setPage(1);
  }, []);

  const refresh = useCallback(() => {
    setRefreshKey((k) => k + 1);
  }, []);

  return {
    tasks,
    total,
    page,
    setPage,
    loading,
    error,
    familyFilter,
    setFamilyFilter: handleSetFamilyFilter,
    statusFilter,
    setStatusFilter: handleSetStatusFilter,
    pageSize,
    totalPages: Math.ceil(total / pageSize),
    refresh,
  };
}
