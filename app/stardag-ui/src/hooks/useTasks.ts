import { useEffect, useState, useCallback } from "react";
import { fetchTask, fetchTasks, type TaskFilters } from "../api/tasks";
import type { Task, TaskStatus } from "../types/task";

export interface TaskWithContext extends Task {
  isFilterMatch: boolean;
}

interface UseTasksReturn {
  tasks: Task[];
  tasksWithContext: TaskWithContext[];
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

const MAX_DEPTH = 3;

async function fetchAllTasks(): Promise<Task[]> {
  const allTasks: Task[] = [];
  let page = 1;
  const pageSize = 100;

  while (true) {
    try {
      const response = await fetchTasks({ page, page_size: pageSize });
      allTasks.push(...response.tasks);
      if (allTasks.length >= response.total || response.tasks.length === 0) {
        break;
      }
      page++;
    } catch {
      break;
    }
  }

  return allTasks;
}

async function loadRelatedTasks(matchingTasks: Task[]): Promise<Map<string, Task>> {
  const taskMap = new Map<string, Task>();
  matchingTasks.forEach((t) => taskMap.set(t.task_id, t));

  const matchingIds = new Set(matchingTasks.map((t) => t.task_id));

  // Load all tasks to find dependents
  const allTasks = await fetchAllTasks();
  allTasks.forEach((t) => taskMap.set(t.task_id, t));

  // Build reverse dependency map (task -> tasks that depend on it)
  const dependentsMap = new Map<string, Set<string>>();
  allTasks.forEach((task) => {
    task.dependency_ids.forEach((depId) => {
      if (!dependentsMap.has(depId)) {
        dependentsMap.set(depId, new Set());
      }
      dependentsMap.get(depId)!.add(task.task_id);
    });
  });

  // BFS to find tasks within MAX_DEPTH connections
  const toVisit: Array<{ taskId: string; depth: number }> = [];
  const visited = new Set<string>();

  // Start from matching tasks
  matchingIds.forEach((id) => {
    toVisit.push({ taskId: id, depth: 0 });
    visited.add(id);
  });

  while (toVisit.length > 0) {
    const { taskId, depth } = toVisit.shift()!;
    if (depth >= MAX_DEPTH) continue;

    const task = taskMap.get(taskId);
    if (!task) continue;

    // Upstream (dependencies)
    for (const depId of task.dependency_ids) {
      if (!visited.has(depId)) {
        visited.add(depId);
        if (!taskMap.has(depId)) {
          try {
            const depTask = await fetchTask(depId);
            taskMap.set(depId, depTask);
          } catch {
            continue;
          }
        }
        toVisit.push({ taskId: depId, depth: depth + 1 });
      }
    }

    // Downstream (dependents)
    const dependents = dependentsMap.get(taskId) || new Set();
    for (const depId of dependents) {
      if (!visited.has(depId)) {
        visited.add(depId);
        toVisit.push({ taskId: depId, depth: depth + 1 });
      }
    }
  }

  // Return only visited tasks
  const result = new Map<string, Task>();
  visited.forEach((id) => {
    const task = taskMap.get(id);
    if (task) result.set(id, task);
  });

  return result;
}

export function useTasks(pageSize = 20): UseTasksReturn {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [tasksWithContext, setTasksWithContext] = useState<TaskWithContext[]>([]);
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
      const matchingTasks = response.tasks;
      setTasks(matchingTasks);
      setTotal(response.total);

      // Load related tasks for DAG view
      const matchingIds = new Set(matchingTasks.map((t) => t.task_id));
      const relatedMap = await loadRelatedTasks(matchingTasks);

      const withContext: TaskWithContext[] = Array.from(relatedMap.values()).map(
        (task) => ({
          ...task,
          isFilterMatch: matchingIds.has(task.task_id),
        }),
      );

      setTasksWithContext(withContext);
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
    tasksWithContext,
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
