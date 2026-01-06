import { Handle, Position } from "@xyflow/react";
import type { TaskStatus } from "../types/task";
import { StatusBadge } from "./StatusBadge";
import type { LayoutDirection } from "./DagGraph";

export interface TaskNodeData extends Record<string, unknown> {
  label: string;
  taskId: string;
  status: TaskStatus;
  isSelected: boolean;
  isFilterMatch: boolean;
  direction: LayoutDirection;
  hasAssets: boolean;
}

const statusBorderColors: Record<TaskStatus, string> = {
  pending: "border-yellow-400",
  running: "border-blue-400",
  completed: "border-green-400",
  failed: "border-red-400",
};

const statusBorderColorsMuted: Record<TaskStatus, string> = {
  pending: "border-yellow-400/40",
  running: "border-blue-400/40",
  completed: "border-green-400/40",
  failed: "border-red-400/40",
};

interface TaskNodeProps {
  data: TaskNodeData;
}

export function TaskNode({ data }: TaskNodeProps) {
  const isMuted = !data.isFilterMatch;
  const isHorizontal = data.direction === "LR";

  return (
    <div
      className={`relative rounded-lg border-2 px-3 py-2 shadow-md transition-all ${
        isMuted
          ? `${statusBorderColorsMuted[data.status]} bg-gray-100 dark:bg-gray-800/50`
          : `${statusBorderColors[data.status]} bg-white dark:bg-gray-800`
      } ${
        data.isSelected
          ? "ring-2 ring-blue-500 ring-offset-2 dark:ring-offset-gray-900"
          : ""
      }`}
    >
      {/* Asset indicator */}
      {data.hasAssets && (
        <div
          className={`absolute -right-1 -top-1 flex h-4 w-4 items-center justify-center rounded-full bg-purple-500 ${
            isMuted ? "opacity-50" : ""
          }`}
          title="Has assets"
        >
          <svg
            className="h-2.5 w-2.5 text-white"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2.5}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19.5 14.25v-2.625a3.375 3.375 0 00-3.375-3.375h-1.5A1.125 1.125 0 0113.5 7.125v-1.5a3.375 3.375 0 00-3.375-3.375H8.25m2.25 0H5.625c-.621 0-1.125.504-1.125 1.125v17.25c0 .621.504 1.125 1.125 1.125h12.75c.621 0 1.125-.504 1.125-1.125V11.25a9 9 0 00-9-9z"
            />
          </svg>
        </div>
      )}
      <Handle
        type="target"
        position={isHorizontal ? Position.Left : Position.Top}
        className={
          isMuted ? "!bg-gray-300 dark:!bg-gray-600" : "!bg-gray-400 dark:!bg-gray-500"
        }
      />
      <div
        className={`flex flex-col items-center gap-1 ${isMuted ? "opacity-60" : ""}`}
      >
        <span
          className={`text-sm font-medium ${
            isMuted
              ? "text-gray-500 dark:text-gray-400"
              : "text-gray-900 dark:text-gray-100"
          }`}
        >
          {data.label}
        </span>
        <span
          className={`font-mono text-xs ${
            isMuted
              ? "text-gray-400 dark:text-gray-500"
              : "text-gray-500 dark:text-gray-400"
          }`}
        >
          {data.taskId.slice(0, 8)}
        </span>
        <StatusBadge status={data.status} muted={isMuted} />
      </div>
      <Handle
        type="source"
        position={isHorizontal ? Position.Right : Position.Bottom}
        className={
          isMuted ? "!bg-gray-300 dark:!bg-gray-600" : "!bg-gray-400 dark:!bg-gray-500"
        }
      />
    </div>
  );
}
