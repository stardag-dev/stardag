import { Handle, Position } from "@xyflow/react";
import type { TaskStatus } from "../types/task";
import { truncateLabel, type LayoutDirection } from "./dagLayout";
import { Tooltip } from "./ui/Tooltip";

/**
 * A batch of plan members drawn as one node: same type, same level, same
 * status (v1's fan-out grouping, done here on the client). Clicking it
 * expands it back into its members.
 */
export interface BatchNodeData extends Record<string, unknown> {
  label: string;
  taskType: string;
  count: number;
  status: TaskStatus;
  // Every member filtered out by the table's filters.
  isMuted: boolean;
  direction: LayoutDirection;
  // Expand the batch into its members (click, Enter or Space).
  onExpand?: () => void;
}

const STATUS_COLORS: Record<string, { bg: string; border: string; badge: string }> = {
  completed: {
    bg: "bg-green-50 dark:bg-green-900/20",
    border: "border-green-300 dark:border-green-700",
    badge: "bg-green-200 text-green-800 dark:bg-green-800 dark:text-green-200",
  },
  running: {
    bg: "bg-blue-50 dark:bg-blue-900/20",
    border: "border-blue-300 dark:border-blue-700",
    badge: "bg-blue-200 text-blue-800 dark:bg-blue-800 dark:text-blue-200",
  },
  failed: {
    bg: "bg-red-50 dark:bg-red-900/20",
    border: "border-red-300 dark:border-red-700",
    badge: "bg-red-200 text-red-800 dark:bg-red-800 dark:text-red-200",
  },
  pending: {
    bg: "bg-gray-100 dark:bg-gray-800/80",
    border: "border-gray-300 dark:border-gray-600",
    badge: "bg-gray-300 text-gray-600 dark:bg-gray-600 dark:text-gray-300",
  },
};

export function BatchNode({ data }: { data: BatchNodeData }) {
  const isHorizontal = data.direction === "LR";
  const colors = STATUS_COLORS[data.status] ?? STATUS_COLORS.pending;
  return (
    <Tooltip
      content={`${data.count} × ${data.taskType}, ${data.status} — click to expand`}
    >
      <div
        role="button"
        tabIndex={0}
        aria-label={`Expand ${data.count} ${data.taskType} (${data.status})`}
        className={`relative cursor-pointer rounded-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 ${
          data.isMuted ? "opacity-60" : ""
        }`}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            e.stopPropagation();
            data.onExpand?.();
          }
        }}
      >
        {/* Stacked-card effect */}
        <div
          className={`absolute top-1 left-1 h-full w-full rounded-lg border-2 opacity-50 ${colors.border} ${colors.bg}`}
        />
        <div
          className={`relative rounded-lg border-2 px-3 py-2 shadow-md ${colors.border} ${colors.bg}`}
        >
          <Handle
            type="target"
            position={isHorizontal ? Position.Left : Position.Top}
            className="!bg-gray-400 dark:!bg-gray-500"
          />
          <div className="flex flex-col items-center gap-1">
            <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
              {truncateLabel(data.label)}
            </span>
            <div className="flex items-center gap-1">
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-semibold ${colors.badge}`}
              >
                ×{data.count}
              </span>
              <span className="text-[10px] uppercase text-gray-600 dark:text-gray-300">
                {data.status}
              </span>
            </div>
          </div>
          <Handle
            type="source"
            position={isHorizontal ? Position.Right : Position.Bottom}
            className="!bg-gray-400 dark:!bg-gray-500"
          />
        </div>
      </div>
    </Tooltip>
  );
}
