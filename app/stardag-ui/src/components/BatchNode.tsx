import { Handle, Position } from "@xyflow/react";
import type { TaskStatus } from "../types/task";
import type { LayoutDirection } from "./DagGraph";

export interface BatchNodeData extends Record<string, unknown> {
  label: string;
  count: number;
  taskNamespace: string;
  depth: number;
  status: TaskStatus;
  direction: LayoutDirection;
}

const STATUS_COLORS: Record<
  string,
  { bg: string; border: string; badge: string; text: string }
> = {
  completed: {
    bg: "bg-green-50 dark:bg-green-900/20",
    border: "border-green-300 dark:border-green-700",
    badge: "bg-green-200 text-green-800 dark:bg-green-800 dark:text-green-200",
    text: "text-green-700 dark:text-green-300",
  },
  running: {
    bg: "bg-blue-50 dark:bg-blue-900/20",
    border: "border-blue-300 dark:border-blue-700",
    badge: "bg-blue-200 text-blue-800 dark:bg-blue-800 dark:text-blue-200",
    text: "text-blue-700 dark:text-blue-300",
  },
  failed: {
    bg: "bg-red-50 dark:bg-red-900/20",
    border: "border-red-300 dark:border-red-700",
    badge: "bg-red-200 text-red-800 dark:bg-red-800 dark:text-red-200",
    text: "text-red-700 dark:text-red-300",
  },
  pending: {
    bg: "bg-gray-100 dark:bg-gray-800/80",
    border: "border-gray-300 dark:border-gray-600",
    badge: "bg-gray-300 text-gray-600 dark:bg-gray-600 dark:text-gray-300",
    text: "text-gray-700 dark:text-gray-300",
  },
  unregistered: {
    bg: "bg-gray-50 dark:bg-gray-900/30",
    border: "border-dashed border-gray-300 dark:border-gray-600",
    badge: "bg-gray-200 text-gray-400 dark:bg-gray-700 dark:text-gray-500",
    text: "text-gray-400 dark:text-gray-500",
  },
};

function getStatusColors(status: string) {
  return STATUS_COLORS[status] ?? STATUS_COLORS.pending;
}

interface BatchNodeProps {
  data: BatchNodeData;
}

export function BatchNode({ data }: BatchNodeProps) {
  const isHorizontal = data.direction === "LR";
  const colors = getStatusColors(data.status);

  return (
    <div className="relative">
      {/* Stacked card effect */}
      <div
        className={`absolute left-1 top-1 h-full w-full rounded-lg border-2 ${colors.border} opacity-50 ${colors.bg}`}
      />
      <div
        className={`relative rounded-lg border-2 ${colors.border} ${colors.bg} px-3 py-2 shadow-md`}
      >
        <Handle
          type="target"
          position={isHorizontal ? Position.Left : Position.Top}
          className="!bg-gray-400 dark:!bg-gray-500"
        />
        <div className="flex flex-col items-center gap-1 opacity-80">
          <span className={`text-sm font-medium ${colors.text}`}>{data.label}</span>
          <div className="flex items-center gap-1">
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-semibold ${colors.badge}`}
            >
              x{data.count}
            </span>
            <span className="text-[10px] uppercase opacity-70">{data.status}</span>
          </div>
        </div>
        <Handle
          type="source"
          position={isHorizontal ? Position.Right : Position.Bottom}
          className="!bg-gray-400 dark:!bg-gray-500"
        />
      </div>
    </div>
  );
}
