import { useCallback, useEffect, useId, useRef, useState } from "react";

export interface DagControlsState {
  upstreamDepth: number;
  downstreamDepth: number;
  maxPerType: number;
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

function useDebouncedNumber(
  initial: number,
  onCommit: (v: number) => void,
  { min = 0, max = 999 }: { min?: number; max?: number } = {},
) {
  const [local, setLocal] = useState(initial);
  const ref = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleChange = useCallback(
    (raw: number) => {
      if (!Number.isFinite(raw)) {
        if (ref.current) clearTimeout(ref.current);
        ref.current = null;
        setLocal(min);
        return;
      }
      const clamped = Math.max(min, Math.min(max, raw));
      setLocal(clamped);
      if (ref.current) clearTimeout(ref.current);
      ref.current = setTimeout(() => onCommit(clamped), 400);
    },
    [onCommit, min, max],
  );

  useEffect(() => {
    return () => {
      if (ref.current) clearTimeout(ref.current);
    };
  }, []);

  return [local, handleChange] as const;
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
  const [localUpstream, setLocalUpstream] = useDebouncedNumber(
    value.upstreamDepth,
    useCallback(
      (v: number) => onChange({ ...value, upstreamDepth: v }),
      [onChange, value],
    ),
    { min: 0, max: 100 },
  );

  const [localDownstream, setLocalDownstream] = useDebouncedNumber(
    value.downstreamDepth,
    useCallback(
      (v: number) => onChange({ ...value, downstreamDepth: v }),
      [onChange, value],
    ),
    { min: 0, max: 100 },
  );

  const [localMaxPerType, setLocalMaxPerType] = useDebouncedNumber(
    value.maxPerType,
    useCallback(
      (v: number) => onChange({ ...value, maxPerType: v }),
      [onChange, value],
    ),
    { min: 1, max: 100 },
  );

  const baseId = useId();
  const hasTraversal = value.upstreamDepth !== 0 || value.downstreamDepth !== 0;
  const totalExtra = upstreamCount + downstreamCount;

  const inputClass =
    "w-12 rounded border border-gray-300 bg-white px-1.5 py-0.5 text-center text-xs tabular-nums dark:border-gray-600 dark:bg-gray-700";

  return (
    <div className="flex items-center gap-3 text-xs text-gray-600 dark:text-gray-400">
      <div className="flex items-center gap-1.5">
        <label
          htmlFor={`${baseId}-upstream`}
          className="whitespace-nowrap font-medium"
          title="How many levels of upstream dependencies to show (0 = none)"
        >
          Upstream:
        </label>
        <input
          id={`${baseId}-upstream`}
          type="number"
          min={0}
          max={100}
          value={localUpstream}
          onChange={(e) => setLocalUpstream(Number(e.target.value))}
          className={inputClass}
        />
      </div>

      <div className="flex items-center gap-1.5">
        <label
          htmlFor={`${baseId}-downstream`}
          className="whitespace-nowrap font-medium"
          title="How many levels of downstream dependents to show (0 = none)"
        >
          Downstream:
        </label>
        <input
          id={`${baseId}-downstream`}
          type="number"
          min={0}
          max={100}
          value={localDownstream}
          onChange={(e) => setLocalDownstream(Number(e.target.value))}
          className={inputClass}
        />
      </div>

      <div className="flex items-center gap-1.5">
        <label
          htmlFor={`${baseId}-max-per-type`}
          className="whitespace-nowrap font-medium"
          title="Max tasks of the same type/status per depth level before grouping into a batch node"
        >
          Group after:
        </label>
        <input
          id={`${baseId}-max-per-type`}
          type="number"
          min={1}
          max={100}
          value={localMaxPerType}
          onChange={(e) => setLocalMaxPerType(Number(e.target.value))}
          className={inputClass}
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
