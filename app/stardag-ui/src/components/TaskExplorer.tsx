import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { fetchWithAuth } from "../api/client";
import { API_V1 } from "../api/config";
import { fetchBuildGraph } from "../api/tasks";
import { useWorkspace } from "../context/WorkspaceContext";
import type { Task, TaskGraphResponse, TaskStatus } from "../types/task";
import { DagGraph } from "./DagGraph";
import { TaskDetail } from "./TaskDetail";

// Filter types
export interface FilterCondition {
  id: string;
  key: string;
  operator: "=" | "!=" | ">" | "<" | ">=" | "<=" | "~";
  value: string;
}

interface TaskSearchResult extends Task {
  build_id?: string;
  build_name?: string;
}

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

// Column configuration
interface ColumnConfig {
  key: string;
  label: string;
  visible: boolean;
  width?: number;
}

const DEFAULT_COLUMNS: ColumnConfig[] = [
  { key: "task_name", label: "Name", visible: true },
  { key: "task_namespace", label: "Namespace", visible: true },
  { key: "status", label: "Status", visible: true },
  { key: "build_name", label: "Build", visible: true },
  { key: "created_at", label: "Created", visible: true },
];

function getStatusColor(status: TaskStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400";
    case "failed":
      return "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400";
    case "running":
      return "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400";
    default:
      return "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400";
  }
}

// Available operators for filter autocomplete
const FILTER_OPERATORS: { op: FilterCondition["operator"]; label: string }[] = [
  { op: "=", label: "equals" },
  { op: "!=", label: "not equals" },
  { op: ">", label: "greater than" },
  { op: "<", label: "less than" },
  { op: ">=", label: "greater or equal" },
  { op: "<=", label: "less or equal" },
  { op: "~", label: "contains" },
];

// Helper: Quote a value if it contains spaces or quotes
function quoteValue(value: string): string {
  if (value.includes(" ") || value.includes('"')) {
    // Escape internal quotes and wrap in quotes
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
// Formats: "key op value", "key op", "key"
// Value can be quoted: key = "value with spaces"
function parseFilterInput(text: string): {
  key?: string;
  op?: FilterCondition["operator"];
  value?: string;
} {
  const trimmed = text.trim();
  if (!trimmed) return {};

  // Regex to match: key (optional: op (optional: value))
  // Key cannot contain spaces, op is one of the operators, value can be quoted
  const opPattern = "=|!=|>=|<=|>|<|~";

  // Try to match: key op value (value may be quoted)
  const fullMatch = trimmed.match(new RegExp(`^(\\S+)\\s+(${opPattern})\\s+(.+)$`));
  if (fullMatch) {
    const [, key, op, rawValue] = fullMatch;
    return {
      key,
      op: op as FilterCondition["operator"],
      value: unquoteValue(rawValue),
    };
  }

  // Try to match: key op (no value yet)
  const keyOpMatch = trimmed.match(new RegExp(`^(\\S+)\\s+(${opPattern})\\s*$`));
  if (keyOpMatch) {
    const [, key, op] = keyOpMatch;
    return { key, op: op as FilterCondition["operator"] };
  }

  // Try to match: key followed by space (ready for operator)
  const keySpaceMatch = trimmed.match(/^(\S+)\s+$/);
  if (keySpaceMatch) {
    return { key: keySpaceMatch[1] };
  }

  // Just a key (no space yet)
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
  const { activeWorkspace } = useWorkspace();

  // Search state
  const [filters, setFilters] = useState<FilterCondition[]>([]);
  const [searchText, setSearchText] = useState("");
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

  // DAG view state
  const [showDag, setShowDag] = useState(false);
  const [dagGraph, setDagGraph] = useState<TaskGraphResponse | null>(null);
  const [dagLoading, setDagLoading] = useState(false);

  // Column state
  const [columns, setColumns] = useState<ColumnConfig[]>(DEFAULT_COLUMNS);
  const [showColumnPicker, setShowColumnPicker] = useState(false);

  // Autocomplete state
  const [availableKeys, setAvailableKeys] = useState<string[]>([]);
  const [showAutocomplete, setShowAutocomplete] = useState(false);
  const [autocompleteOptions, setAutocompleteOptions] = useState<string[]>([]);
  const [selectedIndex, setSelectedIndex] = useState(0);

  // Track if user is composing a filter (don't apply partial query to search)
  const [isComposingFilter, setIsComposingFilter] = useState(false);

  // Reference to input for focus management
  const inputRef = useRef<HTMLInputElement>(null);

  // Load available keys for autocomplete
  useEffect(() => {
    if (!activeWorkspace?.id) return;

    const loadKeys = async () => {
      try {
        const params = new URLSearchParams({
          workspace_id: activeWorkspace.id,
        });
        const response = await fetchWithAuth(`${API_V1}/tasks/search/keys?${params}`);
        if (response.ok) {
          const data = await response.json();
          // API returns objects like {key: "task_name", type: "string"}, extract just the key strings
          const keys = (data.keys || []).map((k: { key: string }) => k.key);
          setAvailableKeys(keys);
        }
      } catch {
        // Silently fail - autocomplete is optional
      }
    };
    loadKeys();
  }, [activeWorkspace?.id]);

  // Search tasks
  const searchTasks = useCallback(async () => {
    if (!activeWorkspace?.id) {
      setTasks([]);
      return;
    }

    setLoading(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        workspace_id: activeWorkspace.id,
        page: String(page),
        page_size: String(pageSize),
        sort: `${sortBy}:${sortDir}`,
      });

      // Add filters (convert to backend format: key:op:value)
      if (filters.length > 0) {
        const filterStr = filters
          .map((f) => `${f.key}:${f.operator}:${f.value}`)
          .join(",");
        params.set("filter", filterStr);
      }

      // Add text search - but NOT if user is composing a filter
      // This prevents empty results while typing a structured query
      if (searchText.trim() && !isComposingFilter) {
        params.set("q", searchText.trim());
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
    activeWorkspace?.id,
    filters,
    searchText,
    isComposingFilter,
    page,
    sortBy,
    sortDir,
  ]);

  useEffect(() => {
    searchTasks();
  }, [searchTasks]);

  // Compute unique builds in search results (for DAG view)
  const uniqueBuildIds = useMemo(() => {
    const buildIds = new Set<string>();
    for (const task of tasks) {
      if (task.build_id) buildIds.add(task.build_id);
    }
    return Array.from(buildIds);
  }, [tasks]);

  const canShowDag =
    uniqueBuildIds.length === 1 && tasks.length > 0 && tasks.length <= 100;
  const dagBuildId = canShowDag ? uniqueBuildIds[0] : null;

  // Load DAG graph when expanded and there's a single build
  useEffect(() => {
    if (!showDag || !dagBuildId || !activeWorkspace?.id) {
      setDagGraph(null);
      return;
    }

    let cancelled = false;
    setDagLoading(true);

    fetchBuildGraph(dagBuildId, activeWorkspace.id)
      .then((graph) => {
        if (!cancelled) {
          setDagGraph(graph);
        }
      })
      .catch((err) => {
        console.error("Failed to load DAG:", err);
        if (!cancelled) {
          setDagGraph(null);
        }
      })
      .finally(() => {
        if (!cancelled) {
          setDagLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [showDag, dagBuildId, activeWorkspace?.id]);

  // Tasks with filter context for DAG view
  const tasksWithContext = useMemo(() => {
    if (!dagGraph) return [];
    const searchTaskIds = new Set(tasks.map((t) => t.task_id));
    return dagGraph.nodes.map((node) => {
      const searchTask = tasks.find((t) => t.task_id === node.task_id);
      return {
        id: node.id,
        task_id: node.task_id,
        workspace_id: activeWorkspace?.id ?? "",
        task_namespace: node.task_namespace,
        task_name: node.task_name,
        task_data: searchTask?.task_data ?? {},
        version: searchTask?.version ?? null,
        created_at: searchTask?.created_at ?? "",
        status: node.status,
        started_at: searchTask?.started_at ?? null,
        completed_at: searchTask?.completed_at ?? null,
        error_message: searchTask?.error_message ?? null,
        asset_count: node.asset_count,
        isFilterMatch: searchTaskIds.has(node.task_id),
      };
    });
  }, [dagGraph, tasks, activeWorkspace?.id]);

  // Handle filter operations
  const addFilter = useCallback(
    (key: string, operator: FilterCondition["operator"], value: string) => {
      setFilters((prev) => {
        // For exact equality (=), only one filter per key is allowed - replace existing
        // For other operators, prevent duplicate (same key + same operator)
        const existingIndex = prev.findIndex((f) =>
          operator === "="
            ? f.key === key && f.operator === "="
            : f.key === key && f.operator === operator,
        );

        if (existingIndex >= 0) {
          // Replace existing filter with new value
          const updated = [...prev];
          updated[existingIndex] = { ...updated[existingIndex], value };
          return updated;
        }

        // Add new filter with unique ID
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

  // Edit filter - populate search bar and remove filter
  const editFilter = useCallback((filter: FilterCondition) => {
    setSearchText(formatFilterForInput(filter));
    setFilters((prev) => prev.filter((f) => f.id !== filter.id));
    setIsComposingFilter(true);
  }, []);

  // Handle DAG task click
  const handleDagTaskClick = useCallback(
    (taskId: string) => {
      const task = tasks.find((t) => t.task_id === taskId);
      if (task) setSelectedTask(task);
    },
    [tasks],
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

  // Autocomplete mode: "key" | "operator" | "value"
  const [autocompleteMode, setAutocompleteMode] = useState<
    "key" | "operator" | "value"
  >("key");
  const [autocompleteKey, setAutocompleteKey] = useState<string>("");

  // Fetch value suggestions from backend
  const fetchValueSuggestions = useCallback(
    async (key: string, prefix: string) => {
      if (!activeWorkspace?.id) return;
      try {
        const params = new URLSearchParams({
          workspace_id: activeWorkspace.id,
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
        // Silently fail - autocomplete is optional
      }
    },
    [activeWorkspace?.id],
  );

  // Handle autocomplete input - determines which stage we're in
  // Uses space-based syntax: "key op value" or "key op" or "key"
  const handleSearchInput = useCallback(
    (value: string) => {
      setSearchText(value);
      setSelectedIndex(0); // Reset selection when input changes

      // Parse the input to determine autocomplete mode
      const parsed = parseFilterInput(value);

      // Determine if we're composing a structured filter
      // (has a recognized key that looks like a filter key)
      const looksLikeFilter =
        !!parsed.key &&
        (availableKeys.includes(parsed.key) ||
          parsed.key.startsWith("param.") ||
          value.includes(" "));
      setIsComposingFilter(looksLikeFilter);

      // Stage 1: Just typing a key (no space yet)
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

      // Stage 2: Key followed by space - suggest operators
      // Pattern: "key " or "key =", "key !", etc (partial operator)
      if (parsed.key && !parsed.value) {
        const afterKey = value.slice(parsed.key.length).trimStart();

        if (afterKey === "" || afterKey.match(/^[=!<>~]*$/)) {
          // Show operators (filter by partial if any)
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

      // Stage 3: Key and operator present - suggest values
      if (parsed.key && parsed.op) {
        setAutocompleteMode("value");
        setAutocompleteKey(parsed.key);
        fetchValueSuggestions(parsed.key, parsed.value ?? "");
        return;
      }

      setShowAutocomplete(false);
      setIsComposingFilter(false);
    },
    [availableKeys, fetchValueSuggestions],
  );

  // Handle autocomplete option selection
  const handleAutocompleteSelect = useCallback(
    (option: string) => {
      if (autocompleteMode === "key") {
        // After key selection, add space and auto-show operator suggestions
        const newText = `${option} `;
        setSearchText(newText);
        setAutocompleteKey(option);
        // Auto-trigger operator suggestions
        setAutocompleteMode("operator");
        setAutocompleteOptions(FILTER_OPERATORS.map((o) => o.op));
        setShowAutocomplete(true);
        setSelectedIndex(0);
      } else if (autocompleteMode === "operator") {
        // After operator selection, add space and auto-show value suggestions
        const newText = `${autocompleteKey} ${option} `;
        setSearchText(newText);
        // Auto-trigger value suggestions
        setAutocompleteMode("value");
        fetchValueSuggestions(autocompleteKey, "");
        setSelectedIndex(0);
      } else {
        // After value selection, add the filter directly
        const parsed = parseFilterInput(searchText);
        const op = parsed.op ?? "=";
        addFilter(autocompleteKey, op, option);
        setSearchText("");
        setShowAutocomplete(false);
        setIsComposingFilter(false);
      }
      // Return focus to input
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

      // Try to parse as filter using space-based syntax: "key op value"
      const parsed = parseFilterInput(text);

      if (parsed.key && parsed.op && parsed.value !== undefined) {
        // Complete filter - add it
        addFilter(parsed.key, parsed.op, parsed.value);
        setSearchText("");
        setIsComposingFilter(false);
        return;
      }

      // Otherwise treat as text search - trigger search
      setIsComposingFilter(false);
      searchTasks();
    },
    [searchText, addFilter, searchTasks],
  );

  const totalPages = Math.ceil(total / pageSize);
  const visibleColumns = columns.filter((c) => c.visible);

  if (!activeWorkspace) {
    return (
      <div className="flex h-full items-center justify-center text-gray-500 dark:text-gray-400">
        <p>Select a workspace to explore tasks</p>
      </div>
    );
  }

  return (
    <div className="flex h-full flex-col bg-gray-50 dark:bg-gray-900">
      {/* Header */}
      <div className="border-b border-gray-200 bg-white px-6 py-4 dark:border-gray-700 dark:bg-gray-800">
        <h1 className="text-xl font-semibold text-gray-900 dark:text-gray-100">
          Task Explorer
        </h1>
        <p className="mt-1 text-sm text-gray-500 dark:text-gray-400">
          Search and filter tasks across all builds
        </p>
      </div>

      {/* Main content area with horizontal panels */}
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          {/* Left panel - search, filters, DAG, results */}
          <Panel defaultSize={selectedTask ? 70 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              {/* Search bar */}
              <div className="border-b border-gray-200 bg-white px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
                <form onSubmit={handleSearchSubmit} className="relative">
                  <div className="flex items-center gap-2">
                    <div className="relative flex-1">
                      <input
                        ref={inputRef}
                        type="text"
                        value={searchText}
                        onChange={(e) => handleSearchInput(e.target.value)}
                        onKeyDown={handleKeyDown}
                        onFocus={() => searchText && handleSearchInput(searchText)}
                        onBlur={() => setTimeout(() => setShowAutocomplete(false), 150)}
                        placeholder="Search tasks... (e.g., task_name = MyTask, param.lr > 0.01)"
                        className="w-full rounded-md border border-gray-300 bg-white px-4 py-2 pl-10 text-sm text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
                      />
                      <svg
                        className="absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400"
                        fill="none"
                        stroke="currentColor"
                        viewBox="0 0 24 24"
                      >
                        <path
                          strokeLinecap="round"
                          strokeLinejoin="round"
                          strokeWidth={2}
                          d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
                        />
                      </svg>

                      {/* Autocomplete dropdown */}
                      {showAutocomplete && autocompleteOptions.length > 0 && (
                        <div className="absolute left-0 right-0 top-full z-10 mt-1 rounded-md border border-gray-200 bg-white shadow-lg dark:border-gray-600 dark:bg-gray-700">
                          {/* Header based on mode */}
                          <div className="border-b border-gray-100 px-3 py-1.5 text-xs font-medium text-gray-500 dark:border-gray-600 dark:text-gray-400">
                            {autocompleteMode === "key" && "Keys"}
                            {autocompleteMode === "operator" && "Operators"}
                            {autocompleteMode === "value" &&
                              `Values for ${autocompleteKey}`}
                          </div>
                          {autocompleteOptions.map((option, index) => {
                            const isSelected = index === selectedIndex;
                            // Get operator description if in operator mode
                            const opInfo =
                              autocompleteMode === "operator"
                                ? FILTER_OPERATORS.find((o) => o.op === option)
                                : null;

                            return (
                              <button
                                key={option}
                                type="button"
                                onMouseDown={(e) => {
                                  e.preventDefault();
                                  handleAutocompleteSelect(option);
                                }}
                                onMouseEnter={() => setSelectedIndex(index)}
                                className={`block w-full px-4 py-2 text-left text-sm ${
                                  isSelected
                                    ? "bg-blue-50 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                                    : "text-gray-700 dark:text-gray-300"
                                }`}
                              >
                                {autocompleteMode === "operator" ? (
                                  <span className="flex items-center justify-between">
                                    <span className="font-mono">{option}</span>
                                    {opInfo && (
                                      <span className="text-xs text-gray-400 dark:text-gray-500">
                                        {opInfo.label}
                                      </span>
                                    )}
                                  </span>
                                ) : (
                                  option
                                )}
                              </button>
                            );
                          })}
                          {/* Keyboard hint */}
                          <div className="border-t border-gray-100 px-3 py-1.5 text-xs text-gray-400 dark:border-gray-600 dark:text-gray-500">
                            <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">
                              ↑↓
                            </kbd>{" "}
                            navigate{" "}
                            <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">
                              Enter
                            </kbd>{" "}
                            select{" "}
                            <kbd className="rounded bg-gray-100 px-1 dark:bg-gray-600">
                              Esc
                            </kbd>{" "}
                            close
                          </div>
                        </div>
                      )}
                    </div>

                    <button
                      type="submit"
                      className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
                    >
                      Search
                    </button>

                    {/* Column picker */}
                    <div className="relative">
                      <button
                        type="button"
                        onClick={() => setShowColumnPicker(!showColumnPicker)}
                        className="rounded-md border border-gray-300 p-2 text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                        title="Configure columns"
                      >
                        <svg
                          className="h-4 w-4"
                          fill="none"
                          stroke="currentColor"
                          viewBox="0 0 24 24"
                        >
                          <path
                            strokeLinecap="round"
                            strokeLinejoin="round"
                            strokeWidth={2}
                            d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"
                          />
                        </svg>
                      </button>

                      {showColumnPicker && (
                        <div className="absolute right-0 top-full z-10 mt-1 w-48 rounded-md border border-gray-200 bg-white p-2 shadow-lg dark:border-gray-600 dark:bg-gray-700">
                          {columns.map((col) => (
                            <label
                              key={col.key}
                              className="flex items-center gap-2 rounded px-2 py-1 hover:bg-gray-100 dark:hover:bg-gray-600"
                            >
                              <input
                                type="checkbox"
                                checked={col.visible}
                                onChange={() =>
                                  setColumns((prev) =>
                                    prev.map((c) =>
                                      c.key === col.key
                                        ? { ...c, visible: !c.visible }
                                        : c,
                                    ),
                                  )
                                }
                                className="rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                              />
                              <span className="text-sm text-gray-700 dark:text-gray-300">
                                {col.label}
                              </span>
                            </label>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                </form>

                {/* Active filters */}
                {filters.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {filters.map((filter) => (
                      <span
                        key={filter.id}
                        className="inline-flex items-center gap-1 rounded-full bg-blue-100 px-3 py-1 text-sm text-blue-700 dark:bg-blue-900/50 dark:text-blue-300"
                      >
                        <button
                          onClick={() => editFilter(filter)}
                          className="flex items-center gap-1 hover:underline"
                          title="Click to edit this filter"
                        >
                          <span className="font-medium">{filter.key}</span>
                          <span className="text-blue-500 dark:text-blue-400">
                            {filter.operator}
                          </span>
                          <span>{filter.value}</span>
                        </button>
                        <button
                          onClick={() => removeFilter(filter.id)}
                          className="ml-1 rounded-full p-0.5 hover:bg-blue-200 dark:hover:bg-blue-800"
                          title="Remove filter"
                        >
                          <svg
                            className="h-3 w-3"
                            fill="none"
                            stroke="currentColor"
                            viewBox="0 0 24 24"
                          >
                            <path
                              strokeLinecap="round"
                              strokeLinejoin="round"
                              strokeWidth={2}
                              d="M6 18L18 6M6 6l12 12"
                            />
                          </svg>
                        </button>
                      </span>
                    ))}
                    <button
                      onClick={() => setFilters([])}
                      className="text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
                    >
                      Clear all
                    </button>
                  </div>
                )}
              </div>

              {/* Collapsible DAG section */}
              {tasks.length > 0 && (
                <div className="border-b border-gray-200 dark:border-gray-700">
                  <button
                    onClick={() => canShowDag && setShowDag(!showDag)}
                    disabled={!canShowDag}
                    className={`flex w-full items-center justify-between px-6 py-2 text-sm ${
                      canShowDag
                        ? "cursor-pointer text-gray-700 hover:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-800"
                        : "cursor-not-allowed text-gray-400 dark:text-gray-500"
                    }`}
                  >
                    <div className="flex items-center gap-2">
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
                          {tasks.length > 100
                            ? "(Limit: 100 tasks)"
                            : uniqueBuildIds.length > 1
                              ? `(${uniqueBuildIds.length} builds - select a single build)`
                              : "(No build associated)"}
                        </span>
                      )}
                    </div>
                    {canShowDag && (
                      <span className="text-xs text-gray-500 dark:text-gray-400">
                        {showDag ? "Click to collapse" : "Click to expand"}
                      </span>
                    )}
                  </button>

                  {/* DAG content when expanded */}
                  {showDag && canShowDag && (
                    <div className="h-64 border-t border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900">
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
                </div>
              )}

              {/* Results table area */}
              <div className="flex-1 overflow-auto">
                {loading ? (
                  <div className="flex h-full items-center justify-center">
                    <div className="h-8 w-8 animate-spin rounded-full border-2 border-blue-500 border-t-transparent" />
                  </div>
                ) : error ? (
                  <div className="flex h-full items-center justify-center text-red-500">
                    <p>{error}</p>
                  </div>
                ) : tasks.length === 0 ? (
                  <div className="flex h-full flex-col items-center justify-center text-gray-500 dark:text-gray-400">
                    <svg
                      className="mb-4 h-16 w-16"
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        strokeWidth={1.5}
                        d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"
                      />
                    </svg>
                    <p className="text-lg font-medium">No tasks found</p>
                    <p className="mt-1 text-sm">Try adjusting your filters</p>
                  </div>
                ) : (
                  <table className="w-full">
                    <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
                      <tr>
                        {visibleColumns.map((col) => (
                          <th
                            key={col.key}
                            onClick={() => handleSort(col.key)}
                            className="cursor-pointer border-b border-gray-200 px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 hover:bg-gray-100 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-700"
                          >
                            <div className="flex items-center gap-1">
                              {col.label}
                              {sortBy === col.key && (
                                <svg
                                  className={`h-4 w-4 ${
                                    sortDir === "desc" ? "rotate-180" : ""
                                  }`}
                                  fill="none"
                                  stroke="currentColor"
                                  viewBox="0 0 24 24"
                                >
                                  <path
                                    strokeLinecap="round"
                                    strokeLinejoin="round"
                                    strokeWidth={2}
                                    d="M5 15l7-7 7 7"
                                  />
                                </svg>
                              )}
                            </div>
                          </th>
                        ))}
                        {/* Actions column header */}
                        <th className="border-b border-gray-200 px-4 py-3 text-left text-xs font-medium uppercase tracking-wider text-gray-500 dark:border-gray-700 dark:text-gray-400">
                          <span className="sr-only">Actions</span>
                        </th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-800">
                      {tasks.map((task) => (
                        <tr
                          key={`${task.task_id}-${task.build_id}`}
                          className={`hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                            selectedTask?.task_id === task.task_id
                              ? "bg-blue-50 dark:bg-blue-900/20"
                              : ""
                          }`}
                        >
                          {visibleColumns.map((col) => (
                            <td
                              key={col.key}
                              onClick={(e) => {
                                const value = getCellValue(task, col.key);
                                if (value) {
                                  handleCellClick(col.key, String(value), e.shiftKey);
                                }
                              }}
                              className="cursor-pointer whitespace-nowrap px-4 py-3 text-sm text-gray-900 hover:bg-blue-50 dark:text-gray-100 dark:hover:bg-blue-900/20"
                              title="Click to filter, Shift+click to exclude"
                            >
                              {renderCell(task, col.key, onNavigateToBuild)}
                            </td>
                          ))}
                          {/* Actions column */}
                          <td className="whitespace-nowrap px-4 py-3 text-sm">
                            <button
                              onClick={() => setSelectedTask(task)}
                              className="rounded p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
                              title="View task details"
                            >
                              <svg
                                className="h-5 w-5"
                                fill="none"
                                stroke="currentColor"
                                viewBox="0 0 24 24"
                              >
                                <path
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  strokeWidth={2}
                                  d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
                                />
                                <path
                                  strokeLinecap="round"
                                  strokeLinejoin="round"
                                  strokeWidth={2}
                                  d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z"
                                />
                              </svg>
                            </button>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>

              {/* Pagination */}
              {totalPages > 0 && (
                <div className="flex items-center justify-between border-t border-gray-200 bg-white px-6 py-3 dark:border-gray-700 dark:bg-gray-800">
                  <span className="text-sm text-gray-500 dark:text-gray-400">
                    Showing {(page - 1) * pageSize + 1}-
                    {Math.min(page * pageSize, total)} of {total}
                  </span>
                  <div className="flex items-center gap-2">
                    <button
                      onClick={() => setPage((p) => Math.max(1, p - 1))}
                      disabled={page === 1}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      Previous
                    </button>
                    <span className="text-sm text-gray-500 dark:text-gray-400">
                      Page {page} of {totalPages}
                    </span>
                    <button
                      onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                      disabled={page === totalPages}
                      className="rounded-md border border-gray-300 px-3 py-1.5 text-sm text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
                    >
                      Next
                    </button>
                  </div>
                </div>
              )}
            </div>
          </Panel>

          {/* Task detail panel (only when task selected) */}
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
    </div>
  );
}

// Helper functions
function getCellValue(task: TaskSearchResult, key: string): unknown {
  if (key.startsWith("param.")) {
    const paramPath = key.slice(6).split(".");
    let value: unknown = task.task_data;
    for (const p of paramPath) {
      if (value && typeof value === "object") {
        value = (value as Record<string, unknown>)[p];
      } else {
        return undefined;
      }
    }
    return value;
  }

  switch (key) {
    case "task_name":
      return task.task_name;
    case "task_namespace":
      return task.task_namespace;
    case "status":
      return task.status;
    case "build_name":
      return task.build_name;
    case "build_id":
      return task.build_id;
    case "created_at":
      return task.created_at;
    case "task_id":
      return task.task_id;
    default:
      return undefined;
  }
}

function renderCell(
  task: TaskSearchResult,
  key: string,
  onNavigateToBuild?: (buildId: string) => void,
): React.ReactNode {
  const value = getCellValue(task, key);

  if (key === "status" && typeof value === "string") {
    return (
      <span
        className={`rounded-full px-2 py-0.5 text-xs font-medium ${getStatusColor(
          value as TaskStatus,
        )}`}
      >
        {value}
      </span>
    );
  }

  if (key === "build_name" && task.build_id && onNavigateToBuild) {
    return (
      <button
        onClick={(e) => {
          e.stopPropagation();
          onNavigateToBuild(task.build_id!);
        }}
        className="text-blue-600 hover:underline dark:text-blue-400"
      >
        {value as string}
      </button>
    );
  }

  if (key === "created_at" && typeof value === "string") {
    return new Date(value).toLocaleString();
  }

  if (value === undefined || value === null) {
    return <span className="text-gray-400">-</span>;
  }

  if (typeof value === "object") {
    return <span className="font-mono text-xs">{JSON.stringify(value)}</span>;
  }

  return String(value);
}
