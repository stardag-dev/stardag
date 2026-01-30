import { useState, useEffect } from "react";
import { DagGraph, type LayoutDirection } from "./DagGraph";
import type { TaskWithContext } from "../hooks/useTasks";
import type { TaskGraphResponse } from "../types/task";

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

// Python code snippet
const PYTHON_CODE = `dataset = Dataset(
    dump=Dump(),
    params=preprocess_params
)
train_dataset = Subset(dataset, filter=train_partition)
test_dataset = Subset(dataset, filter=test_partition)
metrics = Metrics(
    predictions=Predictions(
        trained_model=TrainedModel(
            model=LogisticRegression(),
            dataset=train_dataset,
        ),
        dataset=test_dataset,
    )
)
print(metrics.model_dump_json(indent=2))
sd.build(metrics)`;

// Simple Python syntax highlighter with VS Code-like colors
function PythonHighlight({ code }: { code: string }) {
  const highlightCode = (text: string) => {
    // Order matters - more specific patterns first
    const patterns: [RegExp, string][] = [
      // Strings (single and double quoted)
      [/"[^"]*"|'[^']*'/g, "text-[#ce9178]"],
      // Comments
      [/#.*/g, "text-[#6a9955]"],
      // Numbers
      [/\b\d+\b/g, "text-[#b5cea8]"],
      // Keywords
      [
        /\b(import|from|as|def|class|return|if|else|elif|for|while|in|and|or|not|True|False|None)\b/g,
        "text-[#c586c0]",
      ],
      // Built-in functions
      [/\b(print|len|range|str|int|float|list|dict|set)\b/g, "text-[#dcdcaa]"],
      // Class names (capitalized words followed by opening paren)
      [/\b([A-Z][a-zA-Z0-9]*)\s*\(/g, "text-[#4ec9b0]"],
      // Function/method calls
      [/\.([a-z_][a-z0-9_]*)\s*\(/gi, "text-[#dcdcaa]"],
      // Variables/identifiers (handled by default color)
    ];

    // Simple tokenizer - process line by line for better control
    const lines = text.split("\n");
    return lines.map((line, lineIndex) => {
      // Find all matches for all patterns
      const allMatches: {
        match: string;
        index: number;
        className: string;
      }[] = [];

      patterns.forEach(([pattern, className]) => {
        const regex = new RegExp(pattern.source, pattern.flags);
        let match;
        while ((match = regex.exec(line)) !== null) {
          // For class names pattern, we want to highlight only the class name, not the paren
          if (pattern.source.includes("([A-Z]")) {
            allMatches.push({
              match: match[1],
              index: match.index,
              className,
            });
          } else if (pattern.source.includes("\\.([a-z_]")) {
            // For method calls, include the dot
            allMatches.push({
              match: "." + match[1],
              index: match.index,
              className,
            });
          } else {
            allMatches.push({
              match: match[0],
              index: match.index,
              className,
            });
          }
        }
      });

      // Sort by index
      allMatches.sort((a, b) => a.index - b.index);

      // Build tokens, avoiding overlaps
      let lastEnd = 0;
      const finalTokens: React.ReactNode[] = [];

      allMatches.forEach((m, i) => {
        if (m.index >= lastEnd) {
          // Add plain text before this match
          if (m.index > lastEnd) {
            finalTokens.push(
              <span key={`${lineIndex}-plain-${i}`} className="text-[#9cdcfe]">
                {line.slice(lastEnd, m.index)}
              </span>,
            );
          }
          // Add the highlighted match
          finalTokens.push(
            <span key={`${lineIndex}-match-${i}`} className={m.className}>
              {m.match}
            </span>,
          );
          lastEnd = m.index + m.match.length;
        }
      });

      // Add remaining plain text
      if (lastEnd < line.length) {
        finalTokens.push(
          <span key={`${lineIndex}-end`} className="text-[#9cdcfe]">
            {line.slice(lastEnd)}
          </span>,
        );
      }

      return (
        <div key={lineIndex}>
          {finalTokens.length > 0 ? finalTokens : <span>&nbsp;</span>}
        </div>
      );
    });
  };

  return (
    <pre className="m-0 bg-transparent p-4 font-mono text-[0.8rem] leading-relaxed text-left">
      <code>{highlightCode(code)}</code>
    </pre>
  );
}

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
      asset_count: 0,
    },
    {
      id: 2,
      task_id: "dataset-1",
      task_name: "Dataset",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 0,
    },
    {
      id: 3,
      task_id: "subset-train",
      task_name: "Subset (train)",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 0,
    },
    {
      id: 4,
      task_id: "subset-test",
      task_name: "Subset (test)",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 0,
    },
    {
      id: 5,
      task_id: "trained-model-1",
      task_name: "TrainedModel",
      task_namespace: "ml_pipeline",
      status: "completed",
      asset_count: 0,
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
}

function DemoCard({ title, icon, children, className = "" }: DemoCardProps) {
  return (
    <div
      className={`flex flex-col rounded-xl bg-gray-800/50 border border-gray-700/50 overflow-hidden ${className}`}
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
  const isWideScreen = useIsWideScreen();
  const dagDirection: LayoutDirection = isWideScreen ? "LR" : "TB";

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
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
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
            <PythonHighlight code={PYTHON_CODE} />
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
          className="lg:col-span-2"
        >
          <div className="h-72">
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
