import { useState } from "react";
import { PythonCodeBlock } from "./PythonCodeBlock";
import decoratorApiCode from "../code-examples/decorator-api.py?raw";
import classApiCode from "../code-examples/class-api.py?raw";

type Tab = "decorator" | "class";

const TABS: { key: Tab; label: string }[] = [
  { key: "decorator", label: "Decorator API" },
  { key: "class", label: "Class API" },
];

const CODE: Record<Tab, string> = {
  decorator: decoratorApiCode.trimEnd(),
  class: classApiCode.trimEnd(),
};

export function CodeExampleTabs() {
  const [activeTab, setActiveTab] = useState<Tab>("decorator");

  return (
    <div className="mx-auto mb-10 w-full max-w-2xl overflow-hidden rounded-xl border border-gray-700/50 bg-gray-800/50 text-left">
      {/* Tab bar */}
      <div className="flex border-b border-gray-700/50 px-1">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === tab.key
                ? "border-b-2 border-blue-500 text-white"
                : "text-gray-400 hover:text-gray-200"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Code block */}
      <PythonCodeBlock code={CODE[activeTab]} />
    </div>
  );
}
