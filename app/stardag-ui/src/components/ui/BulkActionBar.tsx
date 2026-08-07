import type { ReactNode } from "react";

interface BulkActionBarProps {
  count: number;
  /** Singular noun for the selected rows; pluralised with a trailing "s". */
  noun?: string;
  onClear: () => void;
  /** Action buttons, rendered right-aligned. */
  children?: ReactNode;
  /**
   * Scope caveat rendered under the count — e.g. that the selection only
   * covers the current page. Keep it short.
   */
  note?: ReactNode;
}

/**
 * Bar that appears when a table has a selection, carrying the count and
 * the actions that apply to it.
 *
 * Renders nothing at all when the selection is empty, so callers can drop
 * it into a layout unconditionally. The count lives in an ``aria-live``
 * region so screen-reader users hear the selection grow and shrink.
 */
export function BulkActionBar({
  count,
  noun = "item",
  onClear,
  children,
  note,
}: BulkActionBarProps) {
  if (count === 0) return null;

  return (
    <div
      role="region"
      aria-label={`${noun} selection`}
      className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-blue-200 bg-blue-50 px-4 py-2 dark:border-blue-900 dark:bg-blue-950/40"
    >
      <div className="min-w-0 flex-1">
        <p
          aria-live="polite"
          className="text-sm font-medium text-blue-900 dark:text-blue-200"
        >
          {count} {noun}
          {count === 1 ? "" : "s"} selected
        </p>
        {note && (
          <p className="mt-0.5 text-xs text-blue-700/80 dark:text-blue-300/70">
            {note}
          </p>
        )}
      </div>
      <div className="flex items-center gap-2">
        {children}
        <button
          onClick={onClear}
          className="rounded-md px-2 py-1 text-sm font-medium text-blue-700 hover:bg-blue-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:text-blue-300 dark:hover:bg-blue-900/50"
        >
          Clear selection
        </button>
      </div>
    </div>
  );
}
