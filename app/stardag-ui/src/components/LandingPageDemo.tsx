import { useState } from "react";
import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneDark } from "react-syntax-highlighter/dist/esm/styles/prism";
import { DagGraph } from "./DagGraph";
import type { TaskWithContext } from "../hooks/useTasks";
import type { TaskGraphResponse } from "../types/task";

// Python code snippet
const PYTHON_CODE = `dataset = Dataset(dump=Dump(), params=preprocess_params)
train_dataset = Subset(dataset=dataset, filter=train_partition)
test_dataset = Subset(dataset=dataset, filter=test_partition)
metrics = Metrics(
    predictions=Predictions(
        trained_model=TrainedModel(
            model=LogisticRegression(),
            dataset=train_dataset,
            seed=0,
        ),
        dataset=test_dataset,
    )
)
print(metrics.model_dump_json(indent=2))
sd.build(metrics)`;

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
      id: 1,
      task_id: "dump-1",
      task_name: "Dump",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 1,
    },
    {
      id: 2,
      task_id: "dataset-1",
      task_name: "Dataset",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 1,
    },
    {
      id: 3,
      task_id: "subset-train",
      task_name: "Subset (train)",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 1,
    },
    {
      id: 4,
      task_id: "subset-test",
      task_name: "Subset (test)",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 1,
    },
    {
      id: 5,
      task_id: "trained-model-1",
      task_name: "TrainedModel",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 1,
    },
    {
      id: 6,
      task_id: "predictions-1",
      task_name: "Predictions",
      task_namespace: "ml_pipeline",
      status: "running",
      asset_count: 0,
    },
    {
      id: 7,
      task_id: "metrics-1",
      task_name: "Metrics",
      task_namespace: "ml_pipeline",
      status: "pending",
      asset_count: 0,
    },
  ],
  edges: [
    { source: 1, target: 2 }, // Dump -> Dataset
    { source: 2, target: 3 }, // Dataset -> Subset (train)
    { source: 2, target: 4 }, // Dataset -> Subset (test)
    { source: 3, target: 5 }, // Subset (train) -> TrainedModel
    { source: 5, target: 6 }, // TrainedModel -> Predictions
    { source: 4, target: 6 }, // Subset (test) -> Predictions
    { source: 6, target: 7 }, // Predictions -> Metrics
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
  asset_count: node.asset_count,
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

  if (!isExpandable) {
    return (
      <div className="flex" style={{ paddingLeft: level * 16 }}>
        <span className="text-purple-400">"{label}"</span>
        <span className="text-gray-400">: </span>
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
    <div style={{ paddingLeft: level * 16 }}>
      <button
        onClick={() => setIsOpen(!isOpen)}
        className="flex items-center gap-1 hover:bg-gray-700/50 rounded px-1 -ml-1"
      >
        <span className="text-gray-500 w-4">{isOpen ? "▼" : "▶"}</span>
        <span className="text-purple-400">"{label}"</span>
        <span className="text-gray-400">: </span>
        {!isOpen && <span className="text-gray-500">{preview}</span>}
      </button>
      {isOpen && (
        <div className="border-l border-gray-700 ml-2">
          {entries.map(([key, value], index) => (
            <CollapsibleJson
              key={String(key)}
              label={String(key)}
              data={value}
              level={1}
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
  subtitle: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}

function DemoCard({ title, subtitle, icon, children, className = "" }: DemoCardProps) {
  return (
    <div
      className={`flex flex-col rounded-xl bg-gray-800/50 border border-gray-700/50 overflow-hidden ${className}`}
    >
      <div className="flex items-center gap-3 px-5 py-4 border-b border-gray-700/50">
        <div className="text-blue-400">{icon}</div>
        <div>
          <h3 className="text-lg font-semibold text-white">{title}</h3>
          <p className="text-sm text-gray-400">{subtitle}</p>
        </div>
      </div>
      <div className="flex-1 overflow-hidden">{children}</div>
    </div>
  );
}

// Main demo component
export function LandingPageDemo() {
  return (
    <div className="mt-20 w-full min-w-0">
      <div className="text-center mb-10">
        <h2 className="text-2xl font-bold text-white sm:text-3xl mb-3">
          See It in Action
        </h2>
        <p className="text-gray-400 max-w-2xl mx-auto">
          Define your pipeline in Python, inspect the full specification as JSON, and
          visualize the dependency graph.
        </p>
      </div>

      {/* Desktop: 3 columns, Mobile: vertical stack */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Define - Python Code */}
        <DemoCard
          title="Define"
          subtitle="Composable Python tasks"
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
          <div className="h-80 overflow-auto">
            <SyntaxHighlighter
              language="python"
              style={oneDark}
              customStyle={{
                margin: 0,
                padding: "1rem",
                background: "transparent",
                fontSize: "0.8rem",
                lineHeight: "1.5",
              }}
              showLineNumbers={false}
            >
              {PYTHON_CODE}
            </SyntaxHighlighter>
          </div>
        </DemoCard>

        {/* Inspect - JSON */}
        <DemoCard
          title="Inspect"
          subtitle="Full task specification"
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
          <div className="h-80 overflow-auto p-4 font-mono text-xs">
            <div className="text-gray-400">{"{"}</div>
            {Object.entries(JSON_DATA).map(([key, value]) => (
              <CollapsibleJson
                key={key}
                label={key}
                data={value}
                defaultOpen={key === "predictions"}
                level={1}
              />
            ))}
            <div className="text-gray-400">{"}"}</div>
          </div>
        </DemoCard>

        {/* Visualize - DAG Graph */}
        <DemoCard
          title="Visualize"
          subtitle="Interactive dependency graph"
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
                d="M7 21a4 4 0 01-4-4V5a2 2 0 012-2h4a2 2 0 012 2v12a4 4 0 01-4 4zm0 0h12a2 2 0 002-2v-4a2 2 0 00-2-2h-2.343M11 7.343l1.657-1.657a2 2 0 012.828 0l2.829 2.829a2 2 0 010 2.828l-8.486 8.485M7 17h.01"
              />
            </svg>
          }
          className="lg:row-span-1"
        >
          <div className="h-80">
            <DagGraph
              tasks={MOCK_TASKS}
              graph={MOCK_GRAPH}
              selectedTaskId={null}
              onTaskClick={() => {}}
            />
          </div>
        </DemoCard>
      </div>
    </div>
  );
}
