import type React from "react";

const BASE =
  "inline-flex max-w-[16rem] items-center gap-0.5 rounded-full bg-sky-100 px-2 py-0.5 text-[11px] font-medium text-sky-800 dark:bg-sky-900/40 dark:text-sky-200";

const FOCUS =
  "focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 focus-visible:ring-offset-1 dark:focus-visible:ring-offset-gray-800";

interface TagChipProps {
  tag: string;
  // Clicking the chip (e.g. "filter by this tag"); a plain label without.
  onClick?: () => void;
  // What the click does, for the tooltip and the accessible name.
  clickLabel?: string;
  // Shows a remove button.
  onRemove?: () => void;
  removeLabel?: string;
  // Rendered dimmed, e.g. while a save is in flight.
  pending?: boolean;
}

/**
 * A task tag. Tags are free-form, case-kept discovery labels, so the chip
 * truncates long ones and shows them whole in its tooltip.
 */
export function TagChip({
  tag,
  onClick,
  clickLabel,
  onRemove,
  removeLabel,
  pending = false,
}: TagChipProps) {
  const label = <span className="truncate">{tag}</span>;
  const dim = pending ? "opacity-60" : "";
  return (
    <span className={`${BASE} ${dim}`} title={clickLabel ?? tag}>
      {onClick ? (
        <button
          type="button"
          onClick={(e: React.MouseEvent) => {
            // Chips sit in clickable rows: never also open the row.
            e.stopPropagation();
            onClick();
          }}
          aria-label={clickLabel ?? tag}
          className={`min-w-0 truncate rounded-full hover:underline ${FOCUS}`}
        >
          {tag}
        </button>
      ) : (
        label
      )}
      {onRemove && (
        <button
          type="button"
          onClick={(e: React.MouseEvent) => {
            e.stopPropagation();
            onRemove();
          }}
          aria-label={removeLabel ?? `Remove tag ${tag}`}
          className={`-mr-1 ml-0.5 flex h-3.5 w-3.5 flex-shrink-0 items-center justify-center rounded-full text-sky-700 hover:bg-sky-200 hover:text-sky-900 dark:text-sky-300 dark:hover:bg-sky-800 dark:hover:text-white ${FOCUS}`}
        >
          <svg
            aria-hidden="true"
            className="h-2.5 w-2.5"
            viewBox="0 0 12 12"
            fill="none"
          >
            <path
              d="M3 3l6 6M9 3l-6 6"
              stroke="currentColor"
              strokeWidth={1.6}
              strokeLinecap="round"
            />
          </svg>
        </button>
      )}
    </span>
  );
}
