import { useCallback, useEffect, useRef, useState } from "react";

interface CopyChipProps {
  /** What the chip says — an abbreviation of `value`. */
  label: string;
  /** What lands on the clipboard. The whole point: it is not what is drawn. */
  value: string;
  /**
   * What the chip is, in words, for the tooltip. The full `value` is
   * appended to it, so a caller writes the noun and not the id.
   */
  title: string;
  className?: string;
}

/**
 * A small chip that shows an abbreviation and copies the full value.
 *
 * The pattern an identifier in a table wants. A build id is a UUID: too
 * long to draw in a row, and needed in full the moment somebody runs a
 * command against it. Drawing eight characters and copying thirty-six on
 * click serves both, where a truncated span serves neither.
 *
 * The "copied" flash is cleared on unmount, because a row can be
 * filtered, re-paged or navigated away from inside those two seconds.
 */
export function CopyChip({ label, value, title, className = "" }: CopyChipProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    [],
  );

  const handleCopy = useCallback(
    async (event: React.MouseEvent) => {
      // These chips live inside clickable rows; copying an id must not
      // also navigate away from the row it identifies.
      event.stopPropagation();
      try {
        await navigator.clipboard.writeText(value);
        setCopied(true);
        if (timer.current !== null) clearTimeout(timer.current);
        timer.current = setTimeout(() => {
          timer.current = null;
          setCopied(false);
        }, 2000);
      } catch (err) {
        console.error("Failed to copy:", err);
      }
    },
    [value],
  );

  return (
    <button
      type="button"
      onClick={handleCopy}
      title={`${title}: ${value} (click to copy)`}
      aria-label={`Copy ${title.toLowerCase()} ${value}`}
      className={`inline-flex items-center gap-1 rounded bg-gray-100 px-1.5 py-0.5 font-mono text-[11px] text-gray-600 hover:bg-gray-200 focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600 ${className}`}
    >
      {label}
      {copied && (
        <span aria-hidden="true" className="text-green-600 dark:text-green-400">
          ✓
        </span>
      )}
    </button>
  );
}
