import { useState } from "react";
import { PythonCodeBlock } from "./PythonCodeBlock";

const codeFiles = import.meta.glob("../code-examples/**/*.py", {
  eager: true,
  query: "?raw",
  import: "default",
}) as Record<string, string>;

interface Subtab {
  key: string;
  label: string;
}

interface Feature {
  key: string;
  label: string;
  subtabs?: Subtab[];
}

const FEATURES: Feature[] = [
  {
    key: "compose",
    label: "Compose",
    subtabs: [
      { key: "decorator-api", label: "Decorator API" },
      { key: "class-api", label: "Class API" },
    ],
  },
  {
    key: "pydantic",
    label: "Pydantic",
    subtabs: [
      { key: "decorator-api", label: "Decorator API" },
      { key: "class-api", label: "Class API" },
    ],
  },
  {
    key: "async-io",
    label: "Async IO",
    subtabs: [
      { key: "decorator-api", label: "Decorator API" },
      { key: "class-api", label: "Class API" },
    ],
  },
  {
    key: "parameter-hashing",
    label: "Parameter Hashing",
    subtabs: [
      { key: "composability", label: "Composability" },
      { key: "control", label: "Control" },
    ],
  },
  {
    key: "configure-env",
    label: "Configure Env",
    subtabs: [
      { key: "env-vars", label: "Env Vars" },
      { key: "profile", label: "Profile" },
      { key: "customize", label: "Customize" },
    ],
  },
  {
    key: "serialization",
    label: "Serialization",
    subtabs: [
      { key: "customize", label: "Customize" },
      { key: "load-from-registry", label: "Load from Registry" },
    ],
  },
  { key: "modal", label: "Execute on Modal" },
  { key: "prefect", label: "Embed in Prefect" },
];

function getCode(feature: Feature, subtab?: Subtab): string {
  const path = subtab
    ? `../code-examples/${feature.key}/${subtab.key}.py`
    : `../code-examples/${feature.key}.py`;
  return (codeFiles[path] ?? "# Example coming soon...").trimEnd();
}

function resolveSubtab(feature: Feature, preferredLabel: string): Subtab | undefined {
  if (!feature.subtabs) return undefined;
  return feature.subtabs.find((s) => s.label === preferredLabel) ?? feature.subtabs[0];
}

export function CodeExampleTabs() {
  const [featureIdx, setFeatureIdx] = useState(0);
  const [preferredSubtab, setPreferredSubtab] = useState("Decorator API");

  const feature = FEATURES[featureIdx];
  const subtab = resolveSubtab(feature, preferredSubtab);
  const code = getCode(feature, subtab);

  const handleFeatureChange = (idx: number) => {
    setFeatureIdx(idx);
  };

  const handleSubtabChange = (s: Subtab) => {
    setPreferredSubtab(s.label);
  };

  return (
    <div className="mx-auto mb-10 w-full max-w-2xl overflow-hidden rounded-xl border border-gray-700/50 bg-gray-800/50 text-left">
      {/* Feature pills — horizontally scrollable */}
      <div
        className="flex gap-1 overflow-x-auto border-b border-gray-700/50 px-2 py-2"
        style={{ scrollbarWidth: "none", msOverflowStyle: "none" }}
      >
        {FEATURES.map((f, i) => (
          <button
            key={f.key}
            onClick={() => handleFeatureChange(i)}
            className={`shrink-0 rounded-full px-3 py-1 text-xs font-medium transition-colors ${
              i === featureIdx
                ? "bg-blue-600 text-white"
                : "text-gray-400 hover:bg-gray-700/50 hover:text-gray-200"
            }`}
          >
            {f.label}
          </button>
        ))}
      </div>

      {/* Sub-tabs (underline style) */}
      {feature.subtabs && (
        <div className="flex gap-1 overflow-x-auto border-b border-gray-700/50 px-3">
          {feature.subtabs.map((s) => (
            <button
              key={s.key}
              onClick={() => handleSubtabChange(s)}
              className={`whitespace-nowrap px-3 py-2 text-xs font-medium transition-colors ${
                subtab?.key === s.key
                  ? "border-b-2 border-blue-500 text-white"
                  : "text-gray-400 hover:text-gray-200"
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>
      )}

      {/* Code block */}
      <PythonCodeBlock code={code} />
    </div>
  );
}
