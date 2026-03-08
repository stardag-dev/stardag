import { useState, useEffect } from "react";
import { DagGraph, type LayoutDirection } from "./DagGraph";
import { PythonCodeBlock } from "./PythonCodeBlock";
import type { TaskWithContext, TaskGraphResponse } from "../types/task";
import mlPipelineCode from "../code-examples/ml-pipeline.py?raw";

// Hook to detect if screen is wide (for responsive DAG direction)
function useIsWideScreen(breakpoint = 1024) {
  const [isWide, setIsWide] = useState(
    typeof window !== "undefined" ? window.innerWidth >= breakpoint : true,
  );

  useEffect(() => {
    const handleResize = () => setIsWide(window.innerWidth >= breakpoint);
    window.addEventListener("resize", handleResize);
    return () => window.removeEventListener("resize", handleResize);
  }, [breakpoint]);

  return isWide;
}

const PYTHON_CODE = mlPipelineCode.trimEnd();

// JSON representation (cleaned up per user request)
const JSON_DATA = {
  __namespace: "ml_pipeline",
  __name: "Metrics",
  version: "0",
  predictions: {
    __namespace: "ml_pipeline",
    __name: "Predictions",
    version: "0",
    trained_model: {
      __namespace: "ml_pipeline",
      __name: "TrainedModel",
      version: "0",
      model: {
        type: "LogisticRegression",
        penalty: "l2",
      },
      dataset: {
        __namespace: "ml_pipeline",
        __name: "Subset",
        version: "0",
        dataset: {
          __namespace: "ml_pipeline",
          __name: "Dataset",
          version: "0",
          dump: {
            __namespace: "ml_pipeline",
            __name: "Dump",
            version: "0",
            date: "2026-01-29",
            snapshot_slug: "default",
          },
          params: {
            category_thresholds: [0.0, 0.5, 1.0],
          },
        },
        filter: {
          categories: null,
          segments: null,
          random_partition: {
            num_buckets: 3,
            include_buckets: [0, 1],
            seed_salt: "default",
          },
        },
      },
      seed: 0,
    },
    dataset: {
      __namespace: "ml_pipeline",
      __name: "Subset",
      version: "0",
      dataset: {
        __namespace: "ml_pipeline",
        __name: "Dataset",
        version: "0",
        dump: {
          __namespace: "ml_pipeline",
          __name: "Dump",
          version: "0",
          date: "2026-01-29",
          snapshot_slug: "default",
        },
        params: {
          category_thresholds: [0.0, 0.5, 1.0],
        },
      },
      filter: {
        categories: null,
        segments: null,
        random_partition: {
          num_buckets: 3,
          include_buckets: [2],
          seed_salt: "default",
        },
      },
    },
  },
};

// Mock graph data matching the ML pipeline structure
const MOCK_GRAPH: TaskGraphResponse = {
  nodes: [
    {
      id: "1",
      task_id: "dump-1",
      task_name: "Dump",
      task_namespace: "ml_pipeline",
      status: "completed",
      artifact_count: 0,
    },
    {
      id: "2",
      task_id: "dataset-1",
      task_name: "Dataset",
      task_namespace: "ml_pipeline",
      status: "completed",
      artifact_count: 0,
    },
    {
      id: "3",
      task_id: "subset-train",
      task_name: "Subset (train)",
      task_namespace: "ml_pipeline",
      status: "completed",
      artifact_count: 0,
    },
    {
      id: "4",
      task_id: "subset-test",
      task_name: "Subset (test)",
      task_namespace: "ml_pipeline",
      status: "completed",
      artifact_count: 0,
    },
    {
      id: "5",
      task_id: "trained-model-1",
      task_name: "TrainedModel",
      task_namespace: "ml_pipeline",
      status: "completed",
      artifact_count: 0,
    },
    {
      id: "6",
      task_id: "predictions-1",
      task_name: "Predictions",
      task_namespace: "ml_pipeline",
      status: "running",
      artifact_count: 0,
    },
    {
      id: "7",
      task_id: "metrics-1",
      task_name: "Metrics",
      task_namespace: "ml_pipeline",
      status: "pending",
      artifact_count: 0,
    },
  ],
  edges: [
    { source: "1", target: "2" }, // Dump -> Dataset
    { source: "2", target: "3" }, // Dataset -> Subset (train)
    { source: "2", target: "4" }, // Dataset -> Subset (test)
    { source: "3", target: "5" }, // Subset (train) -> TrainedModel
    { source: "5", target: "6" }, // TrainedModel -> Predictions
    { source: "4", target: "6" }, // Subset (test) -> Predictions
    { source: "6", target: "7" }, // Predictions -> Metrics
  ],
};

// Convert to TaskWithContext for DagGraph
const MOCK_TASKS: TaskWithContext[] = MOCK_GRAPH.nodes.map((node) => ({
  id: node.id,
  task_id: node.task_id,
  environment_id: "demo",
  task_namespace: node.task_namespace,
  task_name: node.task_name,
  task_data: {},
  version: "0",
  output_uri: null,
  created_at: new Date().toISOString(),
  status: node.status,
  started_at: null,
  completed_at: null,
  error_message: null,
  artifact_count: node.artifact_count,
  isFilterMatch: true,
}));

// Collapsible JSON section component
interface CollapsibleJsonProps {
  label: string;
  data: unknown;
  defaultOpen?: boolean;
  level?: number;
}

function CollapsibleJson({
  label,
  data,
  defaultOpen = false,
  level = 0,
}: CollapsibleJsonProps) {
  const [isOpen, setIsOpen] = useState(defaultOpen);
  const isObject = typeof data === "object" && data !== null && !Array.isArray(data);
  const isArray = Array.isArray(data);
  const isExpandable = isObject || isArray;
  const indent = level > 0 ? 16 : 0;

  if (!isExpandable) {
    return (
      <div className="flex flex-wrap" style={{ paddingLeft: indent }}>
        <span className="text-purple-400">"{label}"</span>
        <span className="text-gray-400">:&nbsp;</span>
        <span
          className={
            typeof data === "string"
              ? "text-green-400"
              : typeof data === "number"
                ? "text-orange-400"
                : "text-blue-400"
          }
        >
          {typeof data === "string" ? `"${data}"` : String(data)}
        </span>
      </div>
    );
  }

  const entries = isArray ? data.map((v, i) => [i, v]) : Object.entries(data as object);
  const preview = isArray
    ? `[${(data as unknown[]).length} items]`
    : `{${Object.keys(data as object).length} keys}`;

  return (
    <div style={{ paddingLeft: indent }}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="-ml-1 flex items-center gap-1 rounded px-1 hover:bg-gray-700/50"
      >
        <span className="w-4 text-gray-500">{isOpen ? "▼" : "▶"}</span>
        <span className="text-purple-400">"{label}"</span>
        <span className="text-gray-400">:&nbsp;</span>
        {!isOpen && <span className="text-gray-500">{preview}</span>}
      </button>
      {isOpen && (
        <div className="ml-2 border-l border-gray-700">
          {entries.map(([key, value], index) => (
            <CollapsibleJson
              key={String(key)}
              label={String(key)}
              data={value}
              level={level + 1}
              defaultOpen={
                // Auto-expand first level task objects
                typeof value === "object" &&
                value !== null &&
                "__name" in (value as object) &&
                level === 0 &&
                index === 0
              }
            />
          ))}
        </div>
      )}
    </div>
  );
}

// Card wrapper component
interface DemoCardProps {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
  style?: React.CSSProperties;
}

function DemoCard({ title, icon, children, className = "", style }: DemoCardProps) {
  return (
    <div
      className={`flex flex-col rounded-xl bg-gray-800/50 border border-gray-700/50 overflow-hidden ${className}`}
      style={style}
    >
      <div className="flex items-center gap-3 px-5 py-3 border-b border-gray-700/50">
        <div className="text-blue-400">{icon}</div>
        <h3 className="text-lg font-semibold text-white">{title}</h3>
      </div>
      <div className="flex-1 overflow-hidden">{children}</div>
    </div>
  );
}

// Main demo component
export function LandingPageDemo() {
  // 750px breakpoint for horizontal layout
  const isHorizontalLayout = useIsWideScreen(750);
  // 550px breakpoint for DAG direction and height
  const isDagWide = useIsWideScreen(550);
  const dagDirection: LayoutDirection = isDagWide ? "LR" : "TB";

  return (
    <div className="mt-20 w-full min-w-0">
      <div className="mb-10 text-center">
        <h2 className="mb-3 text-2xl font-bold text-white sm:text-3xl">
          See It in Action
        </h2>
        <p className="mx-auto max-w-2xl text-gray-400">
          Define your pipeline in Python, inspect the specification, and visualize the
          dependency graph and progress.
        </p>
      </div>

      {/* Desktop: 2 columns top + full width bottom, Mobile: vertical stack */}
      <div
        className="grid gap-6"
        style={{ gridTemplateColumns: isHorizontalLayout ? "repeat(2, 1fr)" : "1fr" }}
      >
        {/* Define - Python Code */}
        <DemoCard
          title="Define"
          icon={
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M10 20l4-16m4 4l4 4-4 4M6 16l-4-4 4-4"
              />
            </svg>
          }
        >
          <div className="h-72 overflow-auto">
            <PythonCodeBlock code={PYTHON_CODE} />
          </div>
        </DemoCard>

        {/* Inspect - JSON */}
        <DemoCard
          title="Inspect"
          icon={
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"
              />
            </svg>
          }
        >
          <div className="h-72 overflow-auto p-4 font-mono text-xs text-left">
            <span className="text-gray-400">{"{"}</span>
            <div className="ml-4">
              {Object.entries(JSON_DATA).map(([key, value]) => (
                <CollapsibleJson
                  key={key}
                  label={key}
                  data={value}
                  defaultOpen={key === "predictions"}
                />
              ))}
            </div>
            <span className="text-gray-400">{"}"}</span>
          </div>
        </DemoCard>

        {/* Visualize - DAG Graph - spans full width on desktop */}
        <DemoCard
          title="Visualize"
          icon={
            <svg
              className="h-6 w-6"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M9 17V7m0 10a2 2 0 01-2 2H5a2 2 0 01-2-2V7a2 2 0 012-2h2a2 2 0 012 2m0 10a2 2 0 002 2h2a2 2 0 002-2M9 7a2 2 0 012-2h2a2 2 0 012 2m0 10V7m0 10a2 2 0 002 2h2a2 2 0 002-2V7a2 2 0 00-2-2h-2a2 2 0 00-2 2"
              />
            </svg>
          }
          style={isHorizontalLayout ? { gridColumn: "span 2" } : undefined}
        >
          {/* Height increases by ~50% when in vertical DAG mode (below 550px) */}
          <div style={{ height: isDagWide ? "18rem" : "27rem" }}>
            <DagGraph
              key={dagDirection}
              tasks={MOCK_TASKS}
              graph={MOCK_GRAPH}
              selectedTaskId={null}
              onTaskClick={() => {}}
              defaultDirection={dagDirection}
            />
          </div>
        </DemoCard>
      </div>
    </div>
  );
}
