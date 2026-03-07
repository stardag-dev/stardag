import { useCallback, useRef, useState } from "react";

export interface DagControlsState {
  upstreamDepth: number;
  maxPerType: number;
}

interface DagControlsProps {
  value: DagControlsState;
  onChange: (state: DagControlsState) => void;
  primaryCount: number;
  upstreamCount: number;
  groupCount: number;
  truncated: boolean;
}

export function DagControls({
  value,
  onChange,
  primaryCount,
  upstreamCount,
  groupCount,
  truncated,
}: DagControlsProps) {
  const [localDepth, setLocalDepth] = useState(value.upstreamDepth);
  const [localMaxPerType, setLocalMaxPerType] = useState(value.maxPerType);
  const depthDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const maxPerTypeDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleDepthChange = useCallback(
    (newDepth: number) => {
      setLocalDepth(newDepth);
      if (depthDebounceRef.current) clearTimeout(depthDebounceRef.current);
      depthDebounceRef.current = setTimeout(() => {
        onChange({ ...value, upstreamDepth: newDepth });
      }, 300);
    },
    [onChange, value],
  );

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

  return (
    <div className="flex items-center gap-3 text-xs text-gray-600 dark:text-gray-400">
      <div className="flex items-center gap-1.5">
        <label
          htmlFor="upstream-depth"
          className="whitespace-nowrap font-medium"
          title="How many levels of upstream dependencies to show beyond the primary tasks"
        >
          Upstream depth:
        </label>
        <input
          id="upstream-depth"
          type="range"
          min={0}
          max={5}
          value={localDepth}
          onChange={(e) => handleDepthChange(Number(e.target.value))}
          className="h-1 w-16 cursor-pointer accent-blue-500"
        />
        <span className="w-3 text-center font-mono">{localDepth}</span>
      </div>

      {value.upstreamDepth > 0 && (
        <>
          <div className="flex items-center gap-1.5">
            <label
              htmlFor="max-per-type"
              className="whitespace-nowrap font-medium"
              title="Max tasks of the same type per depth level before grouping into a batch node"
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

          <span className="text-gray-500 dark:text-gray-500">
            {primaryCount} primary + {upstreamCount} upstream
            {groupCount > 0 && ` (${groupCount} groups)`}
          </span>
        </>
      )}

      {truncated && (
        <span className="font-medium text-amber-600 dark:text-amber-400">
          Graph truncated
        </span>
      )}
    </div>
  );
}
