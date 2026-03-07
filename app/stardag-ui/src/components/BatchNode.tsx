import { Handle, Position } from "@xyflow/react";
import type { LayoutDirection } from "./DagGraph";

export interface BatchNodeData extends Record<string, unknown> {
  label: string;
  count: number;
  taskNamespace: string;
  depth: number;
  direction: LayoutDirection;
}

interface BatchNodeProps {
  data: BatchNodeData;
}

export function BatchNode({ data }: BatchNodeProps) {
  const isHorizontal = data.direction === "LR";

  return (
    <div className="relative">
      {/* Stacked card effect */}
      <div className="absolute left-1 top-1 h-full w-full rounded-lg border-2 border-gray-300/50 bg-gray-200/50 dark:border-gray-600/50 dark:bg-gray-700/50" />
      <div className="relative rounded-lg border-2 border-gray-300 bg-gray-100 px-3 py-2 shadow-md dark:border-gray-600 dark:bg-gray-800/80">
        <Handle
          type="target"
          position={isHorizontal ? Position.Left : Position.Top}
          className="!bg-gray-400 dark:!bg-gray-500"
        />
        <div className="flex flex-col items-center gap-1 opacity-70">
          <span className="text-sm font-medium text-gray-700 dark:text-gray-300">
            {data.label}
          </span>
          <span className="rounded-full bg-gray-300 px-2 py-0.5 text-xs font-semibold text-gray-600 dark:bg-gray-600 dark:text-gray-300">
            x{data.count}
          </span>
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
