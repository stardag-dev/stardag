import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  type ImperativePanelHandle,
  Panel,
  PanelGroup,
  PanelResizeHandle,
} from "react-resizable-panels";
import { fetchWithAuth } from "../api/client";
import { API_V1 } from "../api/config";
import {
  fetchAvailableColumns,
  fetchTask,
  fetchTaskGraph,
  type AvailableColumnsResponse,
} from "../api/tasks";
import { useBreadcrumb } from "../context/BreadcrumbContext";
import { useEnvironment } from "../context/EnvironmentContext";
import type {
  Task,
  TaskGraphExtendedResponse,
  TaskGraphResponse,
  TaskWithContext,
} from "../types/task";
import { isExtendedResponse } from "../types/task";
import {
  ColumnManagerModal,
  type AvailableColumn,
  type ColumnConfig,
} from "./ColumnManagerModal";
import { DagControls, type DagControlsState } from "./DagControls";
import { DagGraph } from "./DagGraph";
import { TaskDetail } from "./TaskDetail";
import {
  TaskExplorerSearch,
  type FilterCondition,
  FILTER_OPERATORS,
} from "./TaskExplorerSearch";
import {
  TaskExplorerTable,
  type TaskSearchResult,
  keyToLabel,
} from "./TaskExplorerTable";

// Re-export for any external consumers
export type { FilterCondition } from "./TaskExplorerSearch";

interface TaskSearchResponse {
  tasks: TaskSearchResult[];
  total: number;
  page: number;
  page_size: number;
  available_columns: string[];
}

interface TaskExplorerProps {
  onNavigateToBuild?: (buildId: string) => void;
}

const DEFAULT_COLUMNS: ColumnConfig[] = [
  { key: "task_name", label: "Name", visible: true, width: 200 },
  { key: "task_namespace", label: "Namespace", visible: true, width: 150 },
  { key: "status", label: "Status", visible: true, width: 100 },
  { key: "build_name", label: "Build", visible: true, width: 150 },
  { key: "created_at", label: "Created", visible: true, width: 180 },
];

// Helper: Quote a value if it contains spaces or quotes
function quoteValue(value: string): string {
  if (value.includes(" ") || value.includes('"')) {
    return `"${value.replace(/"/g, '\\"')}"`;
  }
  return value;
}

// Helper: Unquote a value (remove surrounding quotes and unescape)
function unquoteValue(value: string): string {
  if (value.startsWith('"') && value.endsWith('"')) {
    return value.slice(1, -1).replace(/\\"/g, '"');
  }
  return value;
}

// Helper: Parse space-based filter syntax
function parseFilterInput(text: string): {
  key?: string;
  op?: FilterCondition["operator"];
  value?: string;
} {
  const trimmed = text.trim();
  if (!trimmed) return {};

  const opPattern = "=|!=|>=|<=|>|<|~";

  const fullMatch = trimmed.match(new RegExp(`^(\\S+)\\s+(${opPattern})\\s+(.+)$`));
  if (fullMatch) {
    const [, key, op, rawValue] = fullMatch;
    return {
      key,
      op: op as FilterCondition["operator"],
      value: unquoteValue(rawValue),
    };
  }

  const keyOpMatch = trimmed.match(new RegExp(`^(\\S+)\\s+(${opPattern})\\s*$`));
  if (keyOpMatch) {
    const [, key, op] = keyOpMatch;
    return { key, op: op as FilterCondition["operator"] };
  }

  const keySpaceMatch = trimmed.match(/^(\S+)\s+$/);
  if (keySpaceMatch) {
    return { key: keySpaceMatch[1] };
  }

  if (!trimmed.includes(" ")) {
    return { key: trimmed };
  }

  return {};
}

// Helper: Format filter for display in search bar
function formatFilterForInput(filter: FilterCondition): string {
  return `${filter.key} ${filter.operator} ${quoteValue(filter.value)}`;
}

export function TaskExplorer({ onNavigateToBuild }: TaskExplorerProps) {
  const { activeEnvironment } = useEnvironment();
  const { setItems: setBreadcrumb } = useBreadcrumb();

  // Search state
  const [filters, setFilters] = useState<FilterCondition[]>([]);
  const [searchText, setSearchText] = useState("");
  const [committedQuery, setCommittedQuery] = useState("");
  const [sortBy, setSortBy] = useState<string>("created_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");

  // Results state
  const [tasks, setTasks] = useState<TaskSearchResult[]>([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pageSize = 50;

  // Selected task for detail view
  const [selectedTask, setSelectedTask] = useState<TaskSearchResult | null>(null);

  // Update breadcrumb navigation
  useEffect(() => {
    const items: { label: string; onClick?: () => void }[] = [
      { label: "Task Explorer" },
    ];
    if (selectedTask) {
      items.push({ label: selectedTask.task_id });
    }
    setBreadcrumb(items);
    return () => setBreadcrumb([]);
  }, [selectedTask, setBreadcrumb]);

  // DAG view state
  const [userPrefersShowDag, setUserPrefersShowDag] = useState(true);
  const [showDag, setShowDag] = useState(true);
  const [dagGraph, setDagGraph] = useState<
    TaskGraphResponse | TaskGraphExtendedResponse | null
  >(null);
  const [dagLoading, setDagLoading] = useState(false);
  const [dagFullscreen, setDagFullscreen] = useState(false);
  const dagPanelRef = useRef<ImperativePanelHandle>(null);

  // Upstream traversal controls
  const [dagControls, setDagControls] = useState<DagControlsState>({
    upstreamDepth: 0,
    downstreamDepth: 0,
    maxPerType: 5,
  });

  // Handle DAG toggle with panel resize
  const handleToggleDag = useCallback(() => {
    const panel = dagPanelRef.current;
    const newState = !showDag;
    if (panel) {
      if (newState) {
        panel.expand();
      } else {
        panel.collapse();
      }
    }
    setShowDag(newState);
    setUserPrefersShowDag(newState);
  }, [showDag]);

  // Column state
  const [columns, setColumns] = useState<ColumnConfig[]>(DEFAULT_COLUMNS);
  const [showColumnManager, setShowColumnManager] = useState(false);
  const [availableColumns, setAvailableColumns] = useState<AvailableColumn[]>([]);

  // Column resize state
  const [resizingColumn, setResizingColumn] = useState<string | null>(null);
  const resizeStartX = useRef(0);
  const resizeStartWidth = useRef(0);

  // Autocomplete state
  const [availableKeys, setAvailableKeys] = useState<string[]>([]);
  const [showAutocomplete, setShowAutocomplete] = useState(false);
  const [autocompleteOptions, setAutocompleteOptions] = useState<string[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);
  const [autocompleteMode, setAutocompleteMode] = useState<
    "key" | "operator" | "value"
  >("key");
  const [autocompleteKey, setAutocompleteKey] = useState<string>("");

  const inputRef = useRef<HTMLInputElement>(null);

  // Build filter string from current filters
  const currentFilterStr = useMemo(() => {
    if (filters.length === 0) return undefined;
    return filters.map((f) => `${f.key}:${f.operator}:${f.value}`).join(",");
  }, [filters]);
  const currentQ = committedQuery.trim() || undefined;

  // Load available keys for autocomplete
  useEffect(() => {
    if (!activeEnvironment?.id) return;

    const loadKeys = async () => {
      try {
        const params = new URLSearchParams({
          environment_id: activeEnvironment.id,
        });
        if (currentFilterStr) params.set("filter", currentFilterStr);
        if (currentQ) params.set("q", currentQ);
        const response = await fetchWithAuth(`${API_V1}/tasks/search/keys?${params}`);
        if (response.ok) {
          const data = await response.json();
          const keys = (data.keys || []).map((k: { key: string }) => k.key);
          setAvailableKeys(keys);
        }
      } catch {
        // Silently fail - autocomplete is optional
      }
    };
    loadKeys();
  }, [activeEnvironment?.id, currentFilterStr, currentQ]);

  // Re-compute autocomplete when keys load and there's already search text
  useEffect(() => {
    if (availableKeys.length === 0 || !searchText || searchText.includes(" ")) return;

    const suggestions = availableKeys.filter((k) =>
      k.toLowerCase().includes(searchText.toLowerCase()),
    );
    setAutocompleteOptions(suggestions.slice(0, 10));
    setShowAutocomplete(suggestions.length > 0);
    setAutocompleteMode("key");
  }, [availableKeys, searchText]);

  // Load available columns for column manager
  useEffect(() => {
    if (!activeEnvironment?.id) return;

    const loadColumns = async () => {
      try {
        const data: AvailableColumnsResponse = await fetchAvailableColumns(
          activeEnvironment.id,
          currentFilterStr,
          currentQ,
        );

        const cols: AvailableColumn[] = [
          ...data.core.map((key) => ({
            key,
            label: keyToLabel(key),
            type: "core" as const,
          })),
          ...data.params.map((key) => ({
            key,
            label: key,
            type: "param" as const,
          })),
          ...data.artifacts.map((key) => ({
            key,
            label: key,
            type: "artifact" as const,
          })),
        ];
        setAvailableColumns(cols);

        if (data.artifacts.length > 0) {
          setAvailableKeys((prev) => {
            const existingSet = new Set(prev);
            const newKeys = data.artifacts.filter((k) => !existingSet.has(k));
            return newKeys.length > 0 ? [...prev, ...newKeys] : prev;
          });
        }
      } catch {
        // Silently fail
      }
    };
    loadColumns();
  }, [activeEnvironment?.id, currentFilterStr, currentQ]);

  // Column resize handlers
  const handleResizeStart = useCallback(
    (e: React.MouseEvent, columnKey: string) => {
      e.preventDefault();
      e.stopPropagation();
      const column = columns.find((c) => c.key === columnKey);
      if (column) {
        setResizingColumn(columnKey);
        resizeStartX.current = e.clientX;
        resizeStartWidth.current = column.width;
      }
    },
    [columns],
  );

  const handleResizeMove = useCallback(
    (e: MouseEvent) => {
      if (!resizingColumn) return;
      const diff = e.clientX - resizeStartX.current;
      const newWidth = Math.max(80, Math.min(500, resizeStartWidth.current + diff));
      setColumns((prev) =>
        prev.map((col) =>
          col.key === resizingColumn ? { ...col, width: newWidth } : col,
        ),
      );
    },
    [resizingColumn],
  );

  const handleResizeEnd = useCallback(() => {
    setResizingColumn(null);
  }, []);

  useEffect(() => {
    if (resizingColumn) {
      document.addEventListener("mousemove", handleResizeMove);
      document.addEventListener("mouseup", handleResizeEnd);
      return () => {
        document.removeEventListener("mousemove", handleResizeMove);
        document.removeEventListener("mouseup", handleResizeEnd);
      };
    }
  }, [resizingColumn, handleResizeMove, handleResizeEnd]);

  // Compute visible artifact names for search dependency
  const visibleArtifactNames = useMemo(() => {
    const artifactNames = new Set<string>();
    for (const col of columns) {
      if (col.visible && col.key.startsWith("artifact.")) {
        const parts = col.key.slice(9).split(".");
        if (parts.length > 0) {
          artifactNames.add(parts[0]);
        }
      }
    }
    return Array.from(artifactNames).sort().join(",");
  }, [columns]);

  // Search tasks
  const searchTasks = useCallback(async () => {
    if (!activeEnvironment?.id) {
      setTasks([]);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        environment_id: activeEnvironment.id,
        page: String(page),
        page_size: String(pageSize),
        sort: `${sortBy}:${sortDir}`,
      });

      if (filters.length > 0) {
        const filterStr = filters
          .map((f) => `${f.key}:${f.operator}:${f.value}`)
          .join(",");
        params.set("filter", filterStr);
      }

      if (committedQuery.trim()) {
        params.set("q", committedQuery.trim());
      }

      if (visibleArtifactNames) {
        params.set("include_artifacts", visibleArtifactNames);
      }

      const response = await fetchWithAuth(`${API_V1}/tasks/search?${params}`);
      if (!response.ok) {
        throw new Error(`Search failed: ${response.statusText}`);
      }

      const data: TaskSearchResponse = await response.json();
      setTasks(data.tasks);
      setTotal(data.total);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Search failed");
    } finally {
      setLoading(false);
    }
  }, [
    activeEnvironment?.id,
    filters,
    committedQuery,
    page,
    sortBy,
    sortDir,
    visibleArtifactNames,
  ]);

  useEffect(() => {
    searchTasks();
  }, [searchTasks]);

  // DAG availability
  const canShowDag = tasks.length > 0 && tasks.length <= 500;

  // Auto-collapse/expand DAG based on availability
  useEffect(() => {
    const panel = dagPanelRef.current;
    if (!canShowDag) {
      if (showDag && panel) {
        panel.collapse();
      }
      setShowDag(false);
    } else {
      if (userPrefersShowDag && !showDag) {
        if (panel) {
          panel.expand();
        }
        setShowDag(true);
      } else if (!userPrefersShowDag && showDag) {
        if (panel) {
          panel.collapse();
        }
        setShowDag(false);
      }
    }
  }, [canShowDag, userPrefersShowDag]); // eslint-disable-line react-hooks/exhaustive-deps

  // ESC to exit DAG fullscreen
  useEffect(() => {
    if (!dagFullscreen) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape") setDagFullscreen(false);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [dagFullscreen]);

  // Load DAG graph
  useEffect(() => {
    if (!showDag || !canShowDag || !activeEnvironment?.id) {
      setDagGraph(null);
      return;
    }

    const taskIds = tasks.map((t) => t.task_id);
    if (taskIds.length === 0) {
      setDagGraph(null);
      return;
    }

    let cancelled = false;
    setDagLoading(true);

    fetchTaskGraph(taskIds, activeEnvironment.id, {
      upstream_depth: dagControls.upstreamDepth,
      downstream_depth: dagControls.downstreamDepth,
      max_per_type_per_level: dagControls.maxPerType,
    })
      .then((graph) => {
        if (!cancelled) setDagGraph(graph);
      })
      .catch((err) => {
        console.error("Failed to load DAG:", err);
        if (!cancelled) setDagGraph(null);
      })
      .finally(() => {
        if (!cancelled) setDagLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [
    showDag,
    canShowDag,
    tasks,
    activeEnvironment?.id,
    dagControls.upstreamDepth,
    dagControls.downstreamDepth,
    dagControls.maxPerType,
  ]);

  // Extended graph metadata
  const extendedDagGraph = dagGraph && isExtendedResponse(dagGraph) ? dagGraph : null;

  // Tasks with filter context for DAG view
  const tasksWithContext: TaskWithContext[] = useMemo(() => {
    if (!dagGraph) return [];
    const searchTaskIds = new Set(tasks.map((t) => t.task_id));
    return dagGraph.nodes.map((node) => {
      const searchTask = tasks.find((t) => t.task_id === node.task_id);
      const isPrimary =
        "is_primary" in node ? (node as { is_primary: boolean }).is_primary : true;
      return {
        id: node.id,
        task_id: node.task_id,
        environment_id: activeEnvironment?.id ?? "",
        task_namespace: node.task_namespace,
        task_name: node.task_name,
        task_data: searchTask?.task_data ?? {},
        version: searchTask?.version ?? null,
        output_uri: searchTask?.output_uri ?? null,
        created_at: searchTask?.created_at ?? "",
        status: node.status,
        started_at: searchTask?.started_at ?? null,
        completed_at: searchTask?.completed_at ?? null,
        error_message: searchTask?.error_message ?? null,
        artifact_count: node.artifact_count,
        isFilterMatch: isPrimary && searchTaskIds.has(node.task_id),
      };
    });
  }, [dagGraph, tasks, activeEnvironment?.id]);

  // Handle filter operations
  const addFilter = useCallback(
    (key: string, operator: FilterCondition["operator"], value: string) => {
      setFilters((prev) => {
        const existingIndex = prev.findIndex((f) =>
          operator === "="
            ? f.key === key && f.operator === "="
            : f.key === key && f.operator === operator,
        );

        if (existingIndex >= 0) {
          const updated = [...prev];
          updated[existingIndex] = { ...updated[existingIndex], value };
          return updated;
        }

        const id = crypto.randomUUID();
        return [...prev, { id, key, operator, value }];
      });
      setPage(1);
    },
    [],
  );

  const removeFilter = useCallback((id: string) => {
    setFilters((prev) => prev.filter((f) => f.id !== id));
    setPage(1);
  }, []);

  const editFilter = useCallback((filter: FilterCondition) => {
    setSearchText(formatFilterForInput(filter));
    setFilters((prev) => prev.filter((f) => f.id !== filter.id));
  }, []);

  // Handle DAG task click - fetch full task for dependency nodes
  const handleDagTaskClick = useCallback(
    async (taskId: string) => {
      // First check search results (these are complete)
      const searchResult = tasks.find((t) => t.task_id === taskId);
      if (searchResult) {
        setSelectedTask(searchResult);
        return;
      }

      // For dependency nodes not in search results, fetch the full task
      try {
        const fullTask = await fetchTask(taskId);
        setSelectedTask(fullTask as TaskSearchResult);
      } catch (err) {
        console.error("Failed to fetch task:", err);
        // Fallback: use graph node data (limited but better than nothing)
        const graphNode = tasksWithContext.find((t) => t.task_id === taskId);
        if (graphNode) {
          setSelectedTask(graphNode as unknown as TaskSearchResult);
        }
      }
    },
    [tasks, tasksWithContext],
  );

  // Handle click-to-filter on cell
  const handleCellClick = useCallback(
    (key: string, value: string, isShiftKey: boolean) => {
      const operator = isShiftKey ? "!=" : "=";
      addFilter(key, operator, value);
    },
    [addFilter],
  );

  // Handle column sort
  const handleSort = useCallback((column: string) => {
    setSortBy((prev) => {
      if (prev === column) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortDir("desc");
      return column;
    });
    setPage(1);
  }, []);

  // Fetch value suggestions from backend
  const fetchValueSuggestions = useCallback(
    async (key: string, prefix: string) => {
      if (!activeEnvironment?.id) return;
      try {
        const params = new URLSearchParams({
          environment_id: activeEnvironment.id,
          key,
          prefix,
          limit: "10",
        });
        const response = await fetchWithAuth(`${API_V1}/tasks/search/values?${params}`);
        if (response.ok) {
          const data = await response.json();
          const values = (data.values || []).map((v: { value: string }) => v.value);
          setAutocompleteOptions(values);
          setShowAutocomplete(values.length > 0);
        }
      } catch {
        // Silently fail
      }
    },
    [activeEnvironment?.id],
  );

  // Handle autocomplete input
  const handleSearchInput = useCallback(
    (value: string) => {
      setSearchText(value);
      setSelectedIndex(0);

      const parsed = parseFilterInput(value);

      if (parsed.key && !value.includes(" ")) {
        setAutocompleteMode("key");
        if (value.length > 0) {
          const suggestions = availableKeys.filter((k) =>
            k.toLowerCase().includes(value.toLowerCase()),
          );
          setAutocompleteOptions(suggestions.slice(0, 10));
          setShowAutocomplete(suggestions.length > 0);
        } else {
          setShowAutocomplete(false);
        }
        return;
      }

      if (parsed.key && !parsed.value) {
        const afterKey = value.slice(parsed.key.length).trimStart();
        if (afterKey === "" || afterKey.match(/^[=!<>~]*$/)) {
          setAutocompleteMode("operator");
          setAutocompleteKey(parsed.key);
          if (afterKey === "") {
            setAutocompleteOptions(FILTER_OPERATORS.map((o) => o.op));
          } else {
            const filtered = FILTER_OPERATORS.filter((o) => o.op.startsWith(afterKey));
            setAutocompleteOptions(filtered.map((o) => o.op));
          }
          setShowAutocomplete(true);
          return;
        }
      }

      if (parsed.key && parsed.op) {
        setAutocompleteMode("value");
        setAutocompleteKey(parsed.key);
        fetchValueSuggestions(parsed.key, parsed.value ?? "");
        return;
      }

      setShowAutocomplete(false);
    },
    [availableKeys, fetchValueSuggestions],
  );

  // Handle autocomplete option selection
  const handleAutocompleteSelect = useCallback(
    (option: string) => {
      if (autocompleteMode === "key") {
        const newText = `${option} `;
        setSearchText(newText);
        setAutocompleteKey(option);
        setAutocompleteMode("operator");
        setAutocompleteOptions(FILTER_OPERATORS.map((o) => o.op));
        setShowAutocomplete(true);
        setSelectedIndex(0);
      } else if (autocompleteMode === "operator") {
        const newText = `${autocompleteKey} ${option} `;
        setSearchText(newText);
        setAutocompleteMode("value");
        fetchValueSuggestions(autocompleteKey, "");
        setSelectedIndex(0);
      } else {
        const parsed = parseFilterInput(searchText);
        const op = parsed.op ?? "=";
        addFilter(autocompleteKey, op, option);
        setSearchText("");
        setCommittedQuery("");
        setShowAutocomplete(false);
      }
      inputRef.current?.focus();
    },
    [autocompleteMode, autocompleteKey, searchText, fetchValueSuggestions, addFilter],
  );

  // Handle keyboard navigation in autocomplete
  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (!showAutocomplete || autocompleteOptions.length === 0) {
        return;
      }

      switch (e.key) {
        case "ArrowDown":
          e.preventDefault();
          setSelectedIndex((prev) =>
            prev < autocompleteOptions.length - 1 ? prev + 1 : 0,
          );
          break;
        case "ArrowUp":
          e.preventDefault();
          setSelectedIndex((prev) =>
            prev > 0 ? prev - 1 : autocompleteOptions.length - 1,
          );
          break;
        case "Enter":
          if (showAutocomplete && autocompleteOptions.length > 0) {
            e.preventDefault();
            handleAutocompleteSelect(autocompleteOptions[selectedIndex]);
          }
          break;
        case "Escape":
          e.preventDefault();
          setShowAutocomplete(false);
          break;
        case "Tab":
          if (showAutocomplete && autocompleteOptions.length > 0) {
            e.preventDefault();
            handleAutocompleteSelect(autocompleteOptions[selectedIndex]);
          }
          break;
      }
    },
    [showAutocomplete, autocompleteOptions, selectedIndex, handleAutocompleteSelect],
  );

  // Parse and add filter from search text
  const handleSearchSubmit = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const text = searchText.trim();

      const parsed = parseFilterInput(text);
      if (parsed.key && parsed.op && parsed.value !== undefined) {
        addFilter(parsed.key, parsed.op, parsed.value);
        setSearchText("");
        setCommittedQuery("");
        return;
      }

      setCommittedQuery(text);
    },
    [searchText, addFilter],
  );

  const totalPages = Math.ceil(total / pageSize);
  const visibleColumns = columns.filter((c) => c.visible);

  if (!activeEnvironment) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Select an environment to explore tasks</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-900">
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          {/* Left panel - search, filters, DAG, results */}
          <Panel defaultSize={selectedTask ? 70 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              {/* Search bar */}
              <TaskExplorerSearch
                searchText={searchText}
                onSearchInput={handleSearchInput}
                onSearchSubmit={handleSearchSubmit}
                onKeyDown={handleKeyDown}
                onFocus={() => searchText && handleSearchInput(searchText)}
                onBlur={() => setTimeout(() => setShowAutocomplete(false), 150)}
                filters={filters}
                onEditFilter={editFilter}
                onRemoveFilter={removeFilter}
                onClearAllFilters={() => setFilters([])}
                onShowColumnManager={() => setShowColumnManager(true)}
                showAutocomplete={showAutocomplete}
                autocompleteOptions={autocompleteOptions}
                autocompleteMode={autocompleteMode}
                autocompleteKey={autocompleteKey}
                selectedIndex={selectedIndex}
                onAutocompleteSelect={handleAutocompleteSelect}
                onSelectedIndexChange={setSelectedIndex}
                inputRef={inputRef}
              />

              {/* DAG header - always visible */}
              <div className="flex items-center justify-between border-b border-gray-200 px-4 py-1.5 dark:border-gray-700">
                <button
                  onClick={() => canShowDag && handleToggleDag()}
                  disabled={!canShowDag}
                  className={`flex items-center gap-2 text-sm ${
                    canShowDag
                      ? "cursor-pointer text-gray-700 hover:text-gray-900 dark:text-gray-300 dark:hover:text-gray-100"
                      : "cursor-not-allowed text-gray-400 dark:text-gray-500"
                  }`}
                >
                  <svg
                    className={`h-4 w-4 transition-transform ${
                      showDag ? "rotate-90" : ""
                    }`}
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M9 5l7 7-7 7"
                    />
                  </svg>
                  <span className="font-medium">DAG View</span>
                  {!canShowDag && (
                    <span className="text-xs text-gray-400 dark:text-gray-500">
                      {tasks.length === 0 ? "(No tasks)" : "(Limit: 500 tasks)"}
                    </span>
                  )}
                </button>
                {showDag && canShowDag && (
                  <div className="flex items-center gap-2">
                    <DagControls
                      value={dagControls}
                      onChange={setDagControls}
                      primaryCount={tasks.length}
                      upstreamCount={extendedDagGraph?.total_upstream_count ?? 0}
                      downstreamCount={extendedDagGraph?.total_downstream_count ?? 0}
                      groupCount={extendedDagGraph?.groups.length ?? 0}
                      truncated={extendedDagGraph?.truncated ?? false}
                    />
                    <button
                      onClick={() => setDagFullscreen(true)}
                      className="rounded p-1 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      title="Fullscreen DAG"
                    >
                      <svg
                        className="h-4 w-4"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                        strokeWidth={2}
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          d="M4 8V4m0 0h4M4 4l5 5m11-1V4m0 0h-4m4 0l-5 5M4 16v4m0 0h4m-4 0l5-5m11 5v-4m0 4h-4m4 0l-5-5"
                        />
                      </svg>
                    </button>
                  </div>
                )}
              </div>

              {/* DAG + Results with resizable split */}
              <PanelGroup direction="vertical" className="flex-1">
                {/* Collapsible DAG section */}
                <Panel
                  ref={dagPanelRef}
                  defaultSize={50}
                  minSize={0}
                  collapsible
                  onCollapse={() => setShowDag(false)}
                  onExpand={() => setShowDag(true)}
                >
                  {showDag && canShowDag && (
                    <div className="h-full bg-gray-50 dark:bg-gray-900">
                      {dagLoading ? (
                        <div className="flex h-full items-center justify-center">
                          <div className="h-6 w-6 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
                        </div>
                      ) : dagGraph ? (
                        <DagGraph
                          tasks={tasksWithContext}
                          graph={dagGraph}
                          selectedTaskId={selectedTask?.task_id ?? null}
                          onTaskClick={handleDagTaskClick}
                        />
                      ) : (
                        <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
                          <p>Failed to load DAG</p>
                        </div>
                      )}
                    </div>
                  )}
                </Panel>

                <PanelResizeHandle className="h-1 cursor-row-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />

                {/* Results table */}
                <Panel defaultSize={50} minSize={20}>
                  <TaskExplorerTable
                    tasks={tasks}
                    loading={loading}
                    error={error}
                    visibleColumns={visibleColumns}
                    selectedTaskId={selectedTask?.task_id ?? null}
                    sortBy={sortBy}
                    sortDir={sortDir}
                    onSort={handleSort}
                    onCellClick={handleCellClick}
                    onSelectTask={setSelectedTask}
                    onNavigateToBuild={onNavigateToBuild}
                    onResizeStart={handleResizeStart}
                    page={page}
                    pageSize={pageSize}
                    total={total}
                    totalPages={totalPages}
                    onPageChange={setPage}
                  />
                </Panel>
              </PanelGroup>
            </div>
          </Panel>

          {/* Task detail panel */}
          {selectedTask && (
            <>
              <PanelResizeHandle className="w-1 cursor-col-resize bg-gray-200 transition-colors hover:bg-blue-400 dark:bg-gray-700 dark:hover:bg-blue-500" />
              <Panel defaultSize={30} minSize={20} maxSize={50}>
                <div className="h-full border-l border-gray-200 dark:border-gray-700">
                  <TaskDetail
                    task={selectedTask as Task}
                    onClose={() => setSelectedTask(null)}
                  />
                </div>
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>

      {/* Column Manager Modal */}
      <ColumnManagerModal
        isOpen={showColumnManager}
        onClose={() => setShowColumnManager(false)}
        columns={columns}
        availableColumns={availableColumns}
        onColumnsChange={setColumns}
      />

      {/* DAG fullscreen overlay */}
      {dagFullscreen && dagGraph && (
        <div className="fixed inset-0 z-50 flex flex-col bg-white dark:bg-gray-900">
          <div className="flex items-center justify-between border-b border-gray-200 px-4 py-2 dark:border-gray-700">
            <div className="flex items-center gap-3">
              <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
                DAG View
              </span>
              <DagControls
                value={dagControls}
                onChange={setDagControls}
                primaryCount={tasks.length}
                upstreamCount={extendedDagGraph?.total_upstream_count ?? 0}
                downstreamCount={extendedDagGraph?.total_downstream_count ?? 0}
                groupCount={extendedDagGraph?.groups.length ?? 0}
                truncated={extendedDagGraph?.truncated ?? false}
              />
            </div>
            <button
              onClick={() => setDagFullscreen(false)}
              className="rounded p-1.5 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
              title="Exit fullscreen (Esc)"
            >
              <svg
                className="h-5 w-5"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
                strokeWidth={2}
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  d="M6 18L18 6M6 6l12 12"
                />
              </svg>
            </button>
          </div>
          <div className="flex-1">
            <DagGraph
              tasks={tasksWithContext}
              graph={dagGraph}
              selectedTaskId={selectedTask?.task_id ?? null}
              onTaskClick={handleDagTaskClick}
            />
          </div>
        </div>
      )}
    </div>
  );
}
