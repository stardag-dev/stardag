import { Handle, Position } from "@xyflow/react";
import type { TaskStatus } from "../types/task";
import { StatusBadge } from "./StatusBadge";

export interface TaskNodeData extends Record<string, unknown> {
  label: string;
  taskId: string;
  status: TaskStatus;
  isSelected: boolean;
}

const statusBorderColors: Record<TaskStatus, string> = {
  pending: "border-yellow-400",
  running: "border-blue-400",
  completed: "border-green-400",
  failed: "border-red-400",
};

interface TaskNodeProps {
  data: TaskNodeData;
}

export function TaskNode({ data }: TaskNodeProps) {
  return (
    <div
      className={`rounded-lg border-2 bg-white dark:bg-gray-800 px-3 py-2 shadow-md ${
        statusBorderColors[data.status]
      } ${
        data.isSelected
          ? "ring-2 ring-blue-500 ring-offset-2 dark:ring-offset-gray-900"
          : ""
      }`}
    >
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-gray-400 dark:!bg-gray-500"
      />
      <div className="flex flex-col items-center gap-1">
        <span className="text-sm font-medium text-gray-900 dark:text-gray-100">
          {data.label}
        </span>
        <span className="font-mono text-xs text-gray-500 dark:text-gray-400">
          {data.taskId.slice(0, 8)}
        </span>
        <StatusBadge status={data.status} />
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-gray-400 dark:!bg-gray-500"
      />
    </div>
  );
}
