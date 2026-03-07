import { useCallback, useRef, useState } from "react";

export interface DagControlsState {
  upstreamDepth: number;
  downstreamDepth: number;
  maxPerType: number;
}

const DEPTH_OPTIONS = [0, 1, 2, 3, 4, 5, -1] as const; // -1 = "all"
const DEPTH_LABELS: Record<number, string> = { [-1]: "all" };

function depthLabel(d: number): string {
  return DEPTH_LABELS[d] ?? String(d);
}

interface DagControlsProps {
  value: DagControlsState;
  onChange: (state: DagControlsState) => void;
  primaryCount: number;
  upstreamCount: number;
  downstreamCount: number;
  groupCount: number;
  truncated: boolean;
}

export function DagControls({
  value,
  onChange,
  primaryCount,
  upstreamCount,
  downstreamCount,
  groupCount,
  truncated,
}: DagControlsProps) {
  const [localMaxPerType, setLocalMaxPerType] = useState(value.maxPerType);
  const maxPerTypeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleMaxPerTypeChange = useCallback(
    (newMax: number) => {
      const clamped = Math.max(1, Math.min(100, newMax));
      setLocalMaxPerType(clamped);
      if (maxPerTypeDebounceRef.current) clearTimeout(maxPerTypeDebounceRef.current);
      maxPerTypeDebounceRef.current = setTimeout(() => {
        onChange({ ...value, maxPerType: clamped });
      }, 300);
    },
    [onChange, value],
  );

  const hasTraversal = value.upstreamDepth !== 0 || value.downstreamDepth !== 0;
  const totalExtra = upstreamCount + downstreamCount;

  return (
    <div className="flex items-center gap-3 text-xs text-gray-600 dark:text-gray-400">
      {/* Upstream depth selector */}
      <div className="flex items-center gap-1">
        <span
          className="whitespace-nowrap font-medium"
          title="How many levels of upstream dependencies to show"
        >
          Up:
        </span>
        <div className="flex gap-0.5">
          {DEPTH_OPTIONS.map((d) => (
            <button
              key={`up-${d}`}
              onClick={() =>
                onChange({
                  ...value,
                  upstreamDepth: d === -1 ? 100 : d,
                })
              }
              className={`rounded px-1.5 py-0.5 font-mono transition-colors ${
                (d === -1 ? 100 : d) === value.upstreamDepth
                  ? "bg-blue-100 font-semibold text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                  : "text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
              }`}
            >
              {depthLabel(d)}
            </button>
          ))}
        </div>
      </div>

      {/* Downstream depth selector */}
      <div className="flex items-center gap-1">
        <span
          className="whitespace-nowrap font-medium"
          title="How many levels of downstream dependents to show"
        >
          Down:
        </span>
        <div className="flex gap-0.5">
          {DEPTH_OPTIONS.map((d) => (
            <button
              key={`down-${d}`}
              onClick={() =>
                onChange({
                  ...value,
                  downstreamDepth: d === -1 ? 100 : d,
                })
              }
              className={`rounded px-1.5 py-0.5 font-mono transition-colors ${
                (d === -1 ? 100 : d) === value.downstreamDepth
                  ? "bg-blue-100 font-semibold text-blue-700 dark:bg-blue-900 dark:text-blue-300"
                  : "text-gray-500 hover:bg-gray-100 dark:text-gray-400 dark:hover:bg-gray-700"
              }`}
            >
              {depthLabel(d)}
            </button>
          ))}
        </div>
      </div>

      {/* Group threshold - always visible */}
      <div className="flex items-center gap-1.5">
        <label
          htmlFor="max-per-type"
          className="whitespace-nowrap font-medium"
          title="Max tasks of the same type/status per depth level before grouping into a batch node"
        >
          Group after:
        </label>
        <input
          id="max-per-type"
          type="number"
          min={1}
          max={100}
          value={localMaxPerType}
          onChange={(e) => handleMaxPerTypeChange(Number(e.target.value))}
          className="w-12 rounded border border-gray-300 bg-white px-1.5 py-0.5 text-center text-xs tabular-nums dark:border-gray-600 dark:bg-gray-700"
        />
      </div>

      {/* Summary */}
      {(hasTraversal || groupCount > 0) && (
        <span className="text-gray-500 dark:text-gray-500">
          {primaryCount} primary
          {totalExtra > 0 && ` + ${totalExtra} deps`}
          {groupCount > 0 && ` (${groupCount} groups)`}
        </span>
      )}

      {truncated && (
        <span className="font-medium text-amber-600 dark:text-amber-400">
          Truncated
        </span>
      )}
    </div>
  );
}
