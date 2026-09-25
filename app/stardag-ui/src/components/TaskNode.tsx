import { Handle, Position } from "@xyflow/react";
import type { TaskStatus } from "../types/task";
import { shortTaskId } from "../utils/ids";
import { StatusBadge } from "./StatusBadge";
import { truncateLabel, type LayoutDirection } from "./dagLayout";

/** One plan member in the DAG. The node's id is its instance id. */
export interface TaskNodeData extends Record<string, unknown> {
  label: string;
  taskId: string;
  status: TaskStatus;
  isSelected: boolean;
  // Filtered out, or excluded from the plan: drawn muted.
  isMuted: boolean;
  excluded: boolean;
  direction: LayoutDirection;
}

const statusBorderColors: Record<TaskStatus, string> = {
  pending: "border-yellow-400",
  running: "border-blue-400",
  suspended: "border-purple-400",
  interrupted: "border-orange-400",
  completed: "border-green-400",
  failed: "border-red-400",
  skipped: "border-amber-400",
  cancelled: "border-gray-400",
};

export function TaskNode({ data }: { data: TaskNodeData }) {
  const isHorizontal = data.direction === "LR";
  const handleClass = data.isMuted
    ? "!bg-gray-300 dark:!bg-gray-600"
    : "!bg-gray-400 dark:!bg-gray-500";
  return (
    <div
      className={`relative rounded-lg border-2 px-3 py-2 shadow-md transition-all ${
        statusBorderColors[data.status]
      } ${data.excluded ? "border-dashed" : ""} ${
        data.isMuted
          ? "bg-gray-100 opacity-60 dark:bg-gray-800/50"
          : "bg-white dark:bg-gray-800"
      } ${
        data.isSelected
          ? "ring-2 ring-blue-500 ring-offset-2 dark:ring-offset-gray-900"
          : ""
      }`}
      title={data.excluded ? `${data.label} — excluded from this plan` : data.label}
    >
      <Handle
        type="target"
        position={isHorizontal ? Position.Left : Position.Top}
        className={handleClass}
      />
      <div className="flex flex-col items-center gap-1">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {truncateLabel(data.label)}
        </span>
        <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
          {shortTaskId(data.taskId)}
        </span>
        <StatusBadge status={data.status} muted={data.isMuted} />
      </div>
      <Handle
        type="source"
        position={isHorizontal ? Position.Right : Position.Bottom}
        className={handleClass}
      />
    </div>
  );
}
