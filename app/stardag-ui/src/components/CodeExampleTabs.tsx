import { useState } from "react";
import { PythonCodeBlock } from "./PythonCodeBlock";
import decoratorApiCode from "../code-examples/decorator-api.py?raw";
import classApiCode from "../code-examples/class-api.py?raw";
import s3StorageCode from "../code-examples/s3-storage.py?raw";
import taskCompositionCode from "../code-examples/task-composition.py?raw";
import asyncIOCode from "../code-examples/async-io.py?raw";
import taskArePydanticCode from "../code-examples/task-are-pydantic.py?raw";

type MainTab = "decorator" | "class" | "explore";
type ExploreTab = "async-io" | "task-are-pydantic" | "s3-storage" | "composition";

const MAIN_TABS: { key: MainTab; label: string }[] = [
  { key: "decorator", label: "Decorator API" },
  { key: "class", label: "Class API" },
  { key: "explore", label: "Explore features..." },
];

const MAIN_CODE: Record<Exclude<MainTab, "explore">, string> = {
  decorator: decoratorApiCode.trimEnd(),
  class: classApiCode.trimEnd(),
};

const EXPLORE_TABS: { key: ExploreTab; label: string }[] = [
  { key: "task-are-pydantic", label: "Task are Pydantic" },
  { key: "async-io", label: "Async I/O" },
  { key: "s3-storage", label: "S3 Storage" },
  { key: "composition", label: "Task Composition" },
];

const EXPLORE_CODE: Record<ExploreTab, string> = {
  "task-are-pydantic": taskArePydanticCode.trimEnd(),
  "async-io": asyncIOCode.trimEnd(),
  "s3-storage": s3StorageCode.trimEnd(),
  composition: taskCompositionCode.trimEnd(),
};

export function CodeExampleTabs() {
  const [mainTab, setMainTab] = useState<MainTab>("decorator");
  const [exploreTab, setExploreTab] = useState<ExploreTab>("task-are-pydantic");

  const isExploring = mainTab === "explore";
  const code = isExploring ? EXPLORE_CODE[exploreTab] : MAIN_CODE[mainTab];

  return (
    <div className="mx-auto mb-10 w-full max-w-2xl overflow-hidden rounded-xl border border-gray-700/50 bg-gray-800/50 text-left">
      {/* Main tab bar */}
      <div className="flex items-center border-b border-gray-700/50 px-1">
        {MAIN_TABS.map((tab, i) => (
          <div key={tab.key} className="flex items-center">
            {i === MAIN_TABS.length - 1 && (
              <div className="mx-1 h-4 w-px bg-gray-600/50" />
            )}
            <button
              onClick={() => setMainTab(tab.key)}
              className={`px-4 py-2.5 text-sm font-medium transition-colors ${
                mainTab === tab.key
                  ? "border-b-2 border-blue-500 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {tab.label}
            </button>
          </div>
        ))}
      </div>

      {/* Explore sub-tabs */}
      {isExploring && (
        <div className="flex gap-1 border-b border-gray-700/50 px-2">
          {EXPLORE_TABS.map((tab) => (
            <button
              key={tab.key}
              onClick={() => setExploreTab(tab.key)}
              className={`rounded-md px-3 py-1.5 text-xs font-medium transition-colors ${
                exploreTab === tab.key
                  ? "bg-gray-700/60 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {tab.label}
            </button>
          ))}
        </div>
      )}

      {/* Code block */}
      <PythonCodeBlock code={code} />
    </div>
  );
}
