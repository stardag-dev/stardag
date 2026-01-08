import { useEffect, useMemo, useState } from "react";
import {
  DndContext,
  closestCenter,
  KeyboardSensor,
  PointerSensor,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  arrayMove,
  SortableContext,
  sortableKeyboardCoordinates,
  useSortable,
  verticalListSortingStrategy,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";

// Column configuration type
export interface ColumnConfig {
  key: string;
  label: string;
  visible: boolean;
  width: number;
}

// Available column with type information
export interface AvailableColumn {
  key: string;
  label: string;
  type: "core" | "param" | "asset";
}

interface ColumnManagerModalProps {
  isOpen: boolean;
  onClose: () => void;
  columns: ColumnConfig[];
  availableColumns: AvailableColumn[];
  onColumnsChange: (columns: ColumnConfig[]) => void;
}

// Icon components for column types
function CoreIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className ?? "h-4 w-4"}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
      />
    </svg>
  );
}

function ParamIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className ?? "h-4 w-4"}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
      />
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
      />
    </svg>
  );
}

function AssetIcon({ className }: { className?: string }) {
  return (
    <svg
      className={className ?? "h-4 w-4"}
      fill="none"
      stroke="currentColor"
      viewBox="0 0 24 24"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M20 7l-8-4-8 4m16 0l-8 4m8-4v10l-8 4m0-10L4 7m8 4v10M4 7v10l8 4"
      />
    </svg>
  );
}

function ColumnTypeIcon({
  type,
  className,
}: {
  type: "core" | "param" | "asset";
  className?: string;
}) {
  switch (type) {
    case "core":
      return <CoreIcon className={className} />;
    case "param":
      return <ParamIcon className={className} />;
    case "asset":
      return <AssetIcon className={className} />;
  }
}

// Sortable item component for the Visible list
function SortableColumnItem({
  column,
  availableColumn,
  onRemove,
}: {
  column: ColumnConfig;
  availableColumn: AvailableColumn | undefined;
  onRemove: () => void;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: column.key });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`flex items-center gap-2 rounded-md border border-gray-200 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-700 ${
        isDragging ? "opacity-50 shadow-lg" : ""
      }`}
    >
      {/* Drag handle */}
      <button
        {...attributes}
        {...listeners}
        className="cursor-grab touch-none text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
        title="Drag to reorder"
      >
        <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 24 24">
          <path d="M8 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm0 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm0 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm8-12a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm0 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0zm0 6a2 2 0 1 1-4 0 2 2 0 0 1 4 0z" />
        </svg>
      </button>

      {/* Type icon */}
      <ColumnTypeIcon
        type={availableColumn?.type ?? "core"}
        className="h-4 w-4 flex-shrink-0 text-gray-400"
      />

      {/* Label */}
      <span className="flex-1 truncate text-sm text-gray-700 dark:text-gray-300">
        {column.label}
      </span>

      {/* Remove button */}
      <button
        onClick={onRemove}
        className="rounded p-0.5 text-gray-400 hover:bg-gray-100 hover:text-gray-600 dark:hover:bg-gray-600 dark:hover:text-gray-300"
        title="Hide column"
      >
        <svg className="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M6 18L18 6M6 6l12 12"
          />
        </svg>
      </button>
    </div>
  );
}

// Generate label from key
function keyToLabel(key: string): string {
  // For param.* keys, use the key as-is but make it readable
  if (key.startsWith("param.")) {
    return key;
  }
  // For core keys, format nicely
  return key
    .split("_")
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(" ");
}

export function ColumnManagerModal({
  isOpen,
  onClose,
  columns,
  availableColumns,
  onColumnsChange,
}: ColumnManagerModalProps) {
  const [searchQuery, setSearchQuery] = useState("");

  // Close handler that also resets search
  const handleClose = () => {
    setSearchQuery("");
    onClose();
  };

  // Handle escape key
  useEffect(() => {
    if (!isOpen) return;

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        handleClose();
      }
    };

    document.addEventListener("keydown", handleEscape);
    return () => document.removeEventListener("keydown", handleEscape);
  }, [isOpen]); // eslint-disable-line react-hooks/exhaustive-deps

  // Prevent body scroll when modal is open
  useEffect(() => {
    if (isOpen) {
      document.body.style.overflow = "hidden";
    } else {
      document.body.style.overflow = "";
    }
    return () => {
      document.body.style.overflow = "";
    };
  }, [isOpen]);

  // Create a map of available columns for quick lookup
  const availableColumnsMap = useMemo(() => {
    const map = new Map<string, AvailableColumn>();
    for (const col of availableColumns) {
      map.set(col.key, col);
    }
    return map;
  }, [availableColumns]);

  // Get visible columns (in order) and hidden columns
  const visibleColumns = useMemo(() => {
    return columns.filter((c) => c.visible);
  }, [columns]);

  const hiddenColumns = useMemo(() => {
    // Get all available column keys that aren't visible
    const visibleKeys = new Set(columns.filter((c) => c.visible).map((c) => c.key));
    const allKeys = new Set([
      ...availableColumns.map((c) => c.key),
      ...columns.map((c) => c.key),
    ]);

    return Array.from(allKeys)
      .filter((key) => !visibleKeys.has(key))
      .map((key) => {
        const existing = columns.find((c) => c.key === key);
        const available = availableColumnsMap.get(key);
        return {
          key,
          label: existing?.label ?? available?.label ?? keyToLabel(key),
          type: available?.type ?? "core",
        };
      })
      .sort((a, b) => {
        // Sort: core first, then param, then asset
        const typeOrder = { core: 0, param: 1, asset: 2 };
        if (typeOrder[a.type] !== typeOrder[b.type]) {
          return typeOrder[a.type] - typeOrder[b.type];
        }
        return a.label.localeCompare(b.label);
      });
  }, [columns, availableColumns, availableColumnsMap]);

  // Filter columns by search query
  const filteredVisibleColumns = useMemo(() => {
    if (!searchQuery) return visibleColumns;
    const query = searchQuery.toLowerCase();
    return visibleColumns.filter(
      (c) =>
        c.key.toLowerCase().includes(query) || c.label.toLowerCase().includes(query),
    );
  }, [visibleColumns, searchQuery]);

  const filteredHiddenColumns = useMemo(() => {
    if (!searchQuery) return hiddenColumns;
    const query = searchQuery.toLowerCase();
    return hiddenColumns.filter(
      (c) =>
        c.key.toLowerCase().includes(query) || c.label.toLowerCase().includes(query),
    );
  }, [hiddenColumns, searchQuery]);

  // Drag and drop sensors
  const sensors = useSensors(
    useSensor(PointerSensor),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  // Handle drag end
  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;

    if (over && active.id !== over.id) {
      const oldIndex = columns.findIndex((c) => c.key === active.id);
      const newIndex = columns.findIndex((c) => c.key === over.id);

      if (oldIndex !== -1 && newIndex !== -1) {
        const newColumns = arrayMove(columns, oldIndex, newIndex);
        onColumnsChange(newColumns);
      }
    }
  };

  // Show a column
  const handleShowColumn = (key: string) => {
    const existing = columns.find((c) => c.key === key);
    if (existing) {
      // Toggle existing column to visible
      onColumnsChange(
        columns.map((c) => (c.key === key ? { ...c, visible: true } : c)),
      );
    } else {
      // Add new column
      const available = availableColumnsMap.get(key);
      onColumnsChange([
        ...columns,
        {
          key,
          label: available?.label ?? keyToLabel(key),
          visible: true,
          width: 150,
        },
      ]);
    }
  };

  // Hide a column
  const handleHideColumn = (key: string) => {
    onColumnsChange(columns.map((c) => (c.key === key ? { ...c, visible: false } : c)));
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      {/* Backdrop */}
      <div
        className="fixed inset-0 bg-black/50 transition-opacity"
        onClick={handleClose}
      />

      {/* Modal content */}
      <div className="relative z-10 flex max-h-[80vh] w-full max-w-3xl flex-col rounded-lg bg-white shadow-xl dark:bg-gray-800 mx-4">
        {/* Header */}
        <div className="flex items-center justify-between border-b border-gray-200 px-6 py-4 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">
            Manage Columns
          </h2>
          <button
            onClick={handleClose}
            className="rounded-md p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-300"
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
                d="M6 18L18 6M6 6l12 12"
              />
            </svg>
          </button>
        </div>

        {/* Search bar */}
        <div className="border-b border-gray-200 px-6 py-3 dark:border-gray-700">
          <div className="relative">
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search columns..."
              className="w-full rounded-md border border-gray-300 bg-white py-2 pl-10 pr-4 text-sm text-gray-900 placeholder-gray-500 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-100 dark:placeholder-gray-400"
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
          </div>
        </div>

        {/* Two-pane layout */}
        <div className="flex flex-1 gap-4 overflow-hidden p-6">
          {/* Hidden columns (left) */}
          <div className="flex flex-1 flex-col">
            <h3 className="mb-3 text-sm font-medium text-gray-700 dark:text-gray-300">
              Hidden ({filteredHiddenColumns.length})
            </h3>
            <div className="flex-1 overflow-y-auto rounded-md border border-gray-200 bg-gray-50 p-2 dark:border-gray-600 dark:bg-gray-900">
              {filteredHiddenColumns.length === 0 ? (
                <p className="py-4 text-center text-sm text-gray-400 dark:text-gray-500">
                  {searchQuery ? "No matching columns" : "All columns visible"}
                </p>
              ) : (
                <div className="space-y-1">
                  {filteredHiddenColumns.map((col) => (
                    <button
                      key={col.key}
                      onClick={() => handleShowColumn(col.key)}
                      className="flex w-full items-center gap-2 rounded-md px-3 py-2 text-left text-sm text-gray-700 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-800"
                      title="Click to show column"
                    >
                      <ColumnTypeIcon
                        type={col.type}
                        className="h-4 w-4 flex-shrink-0 text-gray-400"
                      />
                      <span className="truncate">{col.label}</span>
                    </button>
                  ))}
                </div>
              )}
            </div>
          </div>

          {/* Visible columns (right) */}
          <div className="flex flex-1 flex-col">
            <h3 className="mb-3 text-sm font-medium text-gray-700 dark:text-gray-300">
              Visible ({filteredVisibleColumns.length})
            </h3>
            <div className="flex-1 overflow-y-auto rounded-md border border-gray-200 bg-gray-50 p-2 dark:border-gray-600 dark:bg-gray-900">
              {filteredVisibleColumns.length === 0 ? (
                <p className="py-4 text-center text-sm text-gray-400 dark:text-gray-500">
                  {searchQuery ? "No matching columns" : "No columns visible"}
                </p>
              ) : (
                <DndContext
                  sensors={sensors}
                  collisionDetection={closestCenter}
                  onDragEnd={handleDragEnd}
                >
                  <SortableContext
                    items={filteredVisibleColumns.map((c) => c.key)}
                    strategy={verticalListSortingStrategy}
                  >
                    <div className="space-y-1">
                      {filteredVisibleColumns.map((col) => (
                        <SortableColumnItem
                          key={col.key}
                          column={col}
                          availableColumn={availableColumnsMap.get(col.key)}
                          onRemove={() => handleHideColumn(col.key)}
                        />
                      ))}
                    </div>
                  </SortableContext>
                </DndContext>
              )}
            </div>
          </div>
        </div>

        {/* Footer */}
        <div className="flex justify-end border-t border-gray-200 px-6 py-4 dark:border-gray-700">
          <button
            onClick={handleClose}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700"
          >
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
