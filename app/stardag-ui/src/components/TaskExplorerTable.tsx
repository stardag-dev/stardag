import React, { useEffect, useMemo, useRef, useState } from "react";
import type { Task, TaskStatus } from "../types/task";
import { truncateNestedKeyToWidth } from "../utils/truncateKey";
import type { ColumnConfig } from "./ColumnManagerModal";

export interface TaskSearchResult extends Task {
  build_id?: string;
  build_name?: string;
}

export interface TaskExplorerTableProps {
  tasks: TaskSearchResult[];
  loading: boolean;
  error: string | null;
  visibleColumns: ColumnConfig[];
  selectedTaskId: string | null;
  sortBy: string;
  sortDir: "asc" | "desc";
  onSort: (column: string) => void;
  onCellClick: (key: string, value: string, isShiftKey: boolean) => void;
  onSelectTask: (task: TaskSearchResult) => void;
  onNavigateToBuild?: (buildId: string) => void;
  onResizeStart: (e: React.MouseEvent, columnKey: string) => void;
  // Pagination
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  onPageChange: (page: number) => void;
}

// Component that applies smart truncation to column headers when needed
// Uses a ref to measure the actual container width for accurate truncation
function TruncatedColumnHeader({
  label,
  isNested,
}: {
  label: string;
  isNested: boolean;
}) {
  const containerRef = useRef<HTMLSpanElement>(null);
  const [containerWidth, setContainerWidth] = useState(0);

  // Measure container width using ResizeObserver
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const observer = new ResizeObserver((entries) => {
      for (const entry of entries) {
        setContainerWidth(entry.contentRect.width);
      }
    });

    observer.observe(container);
    // Initial measurement
    setContainerWidth(container.getBoundingClientRect().width);

    return () => observer.disconnect();
  }, []);

  // For non-nested keys, just show the label
  // For nested keys, apply smart truncation based on measured container width
  const truncatedLabel = useMemo(() => {
    if (!isNested || containerWidth <= 0) {
      return label;
    }

    // Match the header CSS: text-[11px], font-medium (500), uppercase, tracking-wider (0.05em = 0.55px)
    return truncateNestedKeyToWidth(label, containerWidth, {
      font: "500 11px Inter, system-ui, sans-serif",
      letterSpacing: 0.55, // tracking-wider = 0.05em at 11px
      uppercase: true, // CSS text-transform: uppercase
    });
  }, [label, isNested, containerWidth]);

  return (
    <span
      ref={containerRef}
      className="block min-w-0 flex-1 whitespace-nowrap overflow-hidden"
      title={label}
    >
      {truncatedLabel}
    </span>
  );
}

function getStatusColor(status: TaskStatus): string {
  switch (status) {
    case "completed":
      return "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-400";
    case "failed":
      return "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-400";
    case "running":
      return "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-400";
    case "suspended":
      return "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-400";
    case "skipped":
      return "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-400";
    case "cancelled":
      return "bg-gray-100 text-gray-700 dark:bg-gray-900/50 dark:text-gray-400";
    case "unregistered":
      return "bg-gray-50 text-gray-400 dark:bg-gray-900/30 dark:text-gray-500";
    case "pending":
    default:
      return "bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-400";
  }
}

// Convert a column key to a human-readable label
// eslint-disable-next-line react-refresh/only-export-components
export function keyToLabel(key: string): string {
  // For param.* keys, keep the full key as label
  if (key.startsWith("param.")) {
    return key;
  }
  // For artifact.* keys, keep the full key as label
  if (key.startsWith("artifact.")) {
    return key;
  }
  // For core keys, format nicely (task_name -> Task Name)
  return key
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
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

  // Handle artifact.{name}.{path} format
  if (key.startsWith("artifact.")) {
    const parts = key.slice(9).split(".", 1); // Remove 'artifact.' prefix, split at first dot
    if (parts.length < 1) return undefined;

    const artifactName = parts[0];
    const restOfKey = key.slice(9 + artifactName.length + 1); // Get path after artifact.{name}.
    const artifactPath = restOfKey ? restOfKey.split(".") : [];

    // Get artifact data from task
    const artifactData = task.artifact_data?.[artifactName];
    if (!artifactData) return undefined;

    // Navigate the path
    let value: unknown = artifactData;
    for (const p of artifactPath) {
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

// Small link icon component
function LinkIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className ?? "h-3.5 w-3.5"}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14"
      />
    </svg>
  );
}

function renderCell(
  task: TaskSearchResult,
  key: string,
  onNavigateToBuild?: (buildId: string) => void,
  onSelectTask?: (task: TaskSearchResult) => void,
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

  // Task name with link icon to open details
  if (key === "task_name" && onSelectTask) {
    return (
      <span className="inline-flex items-center gap-1.5 overflow-hidden">
        <button
          onClick={(e) => {
            e.stopPropagation();
            onSelectTask(task);
          }}
          className="flex-shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
          title="View task details"
        >
          <LinkIcon />
        </button>
        <span className="truncate">{value as string}</span>
      </span>
    );
  }

  // Build name with link icon to navigate to build
  if (key === "build_name" && task.build_id && onNavigateToBuild) {
    return (
      <span className="inline-flex items-center gap-1.5 overflow-hidden">
        <button
          onClick={(e) => {
            e.stopPropagation();
            onNavigateToBuild(task.build_id!);
          }}
          className="flex-shrink-0 rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
          title="Go to build"
        >
          <LinkIcon />
        </button>
        <span className="truncate">{value as string}</span>
      </span>
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

export function TaskExplorerTable({
  tasks,
  loading,
  error,
  visibleColumns,
  selectedTaskId,
  sortBy,
  sortDir,
  onSort,
  onCellClick,
  onSelectTask,
  onNavigateToBuild,
  onResizeStart,
  page,
  pageSize,
  total,
  totalPages,
  onPageChange,
}: TaskExplorerTableProps) {
  return (
    <div className="flex h-full flex-col">
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
          <table className="w-full table-fixed">
            <thead className="sticky top-0 bg-gray-50 dark:bg-gray-800">
              <tr>
                {visibleColumns.map((col) => (
                  <th
                    key={col.key}
                    style={{ width: col.width }}
                    className="group relative cursor-pointer border-b border-gray-200 px-3 py-1.5 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500 hover:bg-gray-100 dark:border-gray-700 dark:text-gray-400 dark:hover:bg-gray-700"
                  >
                    <div
                      className="flex items-center gap-1 overflow-hidden"
                      onClick={() => onSort(col.key)}
                    >
                      <TruncatedColumnHeader
                        label={col.label}
                        isNested={
                          col.key.startsWith("param.") ||
                          col.key.startsWith("artifact.")
                        }
                      />
                      {sortBy === col.key && (
                        <svg
                          className={`h-4 w-4 flex-shrink-0 ${
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
                    {/* Resize handle */}
                    <div
                      className="absolute right-0 top-0 h-full w-1 cursor-col-resize bg-transparent opacity-0 transition-opacity group-hover:opacity-100 hover:bg-blue-400"
                      onMouseDown={(e) => onResizeStart(e, col.key)}
                    />
                  </th>
                ))}
                {/* Actions column header */}
                <th className="w-12 border-b border-gray-200 px-3 py-1.5 text-left text-[11px] font-medium uppercase tracking-wider text-gray-500 dark:border-gray-700 dark:text-gray-400">
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-200 bg-white dark:divide-gray-700 dark:bg-gray-800">
              {tasks.map((task) => (
                <tr
                  key={`${task.task_id}-${task.build_id}`}
                  className={`hover:bg-gray-50 dark:hover:bg-gray-700/50 ${
                    selectedTaskId === task.task_id
                      ? "bg-blue-50 dark:bg-blue-900/20"
                      : ""
                  }`}
                >
                  {visibleColumns.map((col) => (
                    <td
                      key={col.key}
                      style={{ width: col.width }}
                      onClick={(e) => {
                        const value = getCellValue(task, col.key);
                        if (value !== null && value !== undefined) {
                          onCellClick(col.key, String(value), e.shiftKey);
                        }
                      }}
                      className="cursor-pointer overflow-hidden text-ellipsis whitespace-nowrap px-3 py-1.5 text-xs text-gray-900 hover:bg-blue-50 dark:text-gray-100 dark:hover:bg-blue-900/20"
                      title="Click to filter, Shift+click to exclude"
                    >
                      {renderCell(task, col.key, onNavigateToBuild, onSelectTask)}
                    </td>
                  ))}
                  {/* Actions column */}
                  <td className="whitespace-nowrap px-3 py-1.5 text-xs">
                    <button
                      onClick={() => onSelectTask(task)}
                      className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-700 dark:hover:text-gray-300"
                      title="View task details"
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
        <div className="flex items-center justify-between border-t border-gray-200 bg-white px-4 py-1.5 dark:border-gray-700 dark:bg-gray-800">
          <span className="text-xs text-gray-500 dark:text-gray-400">
            {(page - 1) * pageSize + 1}-{Math.min(page * pageSize, total)} of {total}
          </span>
          <div className="flex items-center gap-2">
            <button
              onClick={() => onPageChange(Math.max(1, page - 1))}
              disabled={page === 1}
              className="rounded border border-gray-300 px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Prev
            </button>
            <span className="text-xs text-gray-500 dark:text-gray-400">
              {page}/{totalPages}
            </span>
            <button
              onClick={() => onPageChange(Math.min(totalPages, page + 1))}
              disabled={page === totalPages}
              className="rounded border border-gray-300 px-2 py-0.5 text-xs text-gray-700 hover:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:text-gray-300 dark:hover:bg-gray-700"
            >
              Next
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
