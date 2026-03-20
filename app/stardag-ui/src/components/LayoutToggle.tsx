import { Panel, useReactFlow } from "@xyflow/react";
import { useCallback } from "react";
import type { LayoutDirection } from "./dagLayout";

interface LayoutToggleProps {
  direction: LayoutDirection;
  onDirectionChange: (direction: LayoutDirection) => void;
  onResetLayout: () => void;
}

export function LayoutToggle({
  direction,
  onDirectionChange,
  onResetLayout,
}: LayoutToggleProps) {
  const { fitView } = useReactFlow();

  const handleDirectionChange = useCallback(
    (newDirection: LayoutDirection) => {
      onDirectionChange(newDirection);
      setTimeout(() => fitView({ padding: 0.2 }), 50);
    },
    [onDirectionChange, fitView],
  );

  const handleResetLayout = useCallback(() => {
    onResetLayout();
    setTimeout(() => fitView({ padding: 0.2 }), 50);
  }, [onResetLayout, fitView]);

  return (
    <Panel position="top-right">
      <div className="flex items-center gap-1 rounded-md border border-gray-200 bg-white p-1 shadow-sm dark:border-gray-700 dark:bg-gray-800">
        <button
          onClick={() => handleDirectionChange("LR")}
          className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
            direction === "LR"
              ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          }`}
          title="Left to Right"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M14 5l7 7m0 0l-7 7m7-7H3"
            />
          </svg>
        </button>
        <button
          onClick={() => handleDirectionChange("TB")}
          className={`rounded px-2 py-1 text-xs font-medium transition-colors ${
            direction === "TB"
              ? "bg-blue-100 text-blue-700 dark:bg-blue-900 dark:text-blue-300"
              : "text-gray-600 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          }`}
          title="Top to Bottom"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M19 14l-7 7m0 0l-7-7m7 7V3"
            />
          </svg>
        </button>
        <div className="mx-0.5 h-4 w-px bg-gray-200 dark:bg-gray-700" />
        <button
          onClick={handleResetLayout}
          className="rounded px-2 py-1 text-xs font-medium text-gray-600 transition-colors hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
          title="Reset layout"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            viewBox="0 0 24 24"
            stroke="currentColor"
            strokeWidth={2}
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
            />
          </svg>
        </button>
      </div>
    </Panel>
  );
}
