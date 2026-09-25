import { useEffect, useId, useRef, useState } from "react";
import { Tooltip } from "./ui/Tooltip";

interface GroupAfterControlProps {
  value: number;
  onChange: (value: number) => void;
  // Batches drawn now, for the summary.
  batchCount?: number;
  // Offered when batches were opened by a click; closes them again.
  onRegroup?: () => void;
}

const MIN = 1;
const MAX = 100;

/**
 * v1's "Group after" DAG control: how many members of one type, level and
 * status are drawn before they collapse into a batch node. Debounced, as
 * v1's was, so typing "12" does not regroup at "1". Sits in the plan graph
 * panel's header, where v1's `DagControls` sat.
 */
export function GroupAfterControl({
  value,
  onChange,
  batchCount = 0,
  onRegroup,
}: GroupAfterControlProps) {
  const id = useId();
  const [local, setLocal] = useState(value);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [synced, setSynced] = useState(value);
  if (value !== synced) {
    setSynced(value);
    setLocal(value);
  }
  useEffect(
    () => () => {
      if (timer.current) clearTimeout(timer.current);
    },
    [],
  );
  return (
    <div className="flex items-center gap-1.5 text-xs text-gray-600 dark:text-gray-400">
      <Tooltip content="Members of the same type, level and status drawn before they collapse into one batch node">
        <label htmlFor={id} className="font-medium whitespace-nowrap">
          Group after:
        </label>
      </Tooltip>
      <input
        id={id}
        type="number"
        min={MIN}
        max={MAX}
        value={local}
        onChange={(e) => {
          const raw = Number(e.target.value);
          const next = Number.isFinite(raw) ? Math.max(MIN, Math.min(MAX, raw)) : MIN;
          setLocal(next);
          if (timer.current) clearTimeout(timer.current);
          timer.current = setTimeout(() => onChange(next), 400);
        }}
        className="w-12 rounded border border-gray-300 bg-white px-1.5 py-0.5 text-center text-xs tabular-nums dark:border-gray-600 dark:bg-gray-700"
      />
      {batchCount > 0 && (
        <span className="whitespace-nowrap text-gray-500">
          ({batchCount} group{batchCount === 1 ? "" : "s"})
        </span>
      )}
      {onRegroup && (
        <button
          type="button"
          onClick={onRegroup}
          className="text-blue-600 hover:underline dark:text-blue-400"
        >
          Regroup
        </button>
      )}
    </div>
  );
}
