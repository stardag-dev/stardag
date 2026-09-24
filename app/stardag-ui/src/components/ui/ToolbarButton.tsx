import { useId, type ReactNode } from "react";
import { Tooltip } from "./Tooltip";

interface ToolbarButtonProps {
  /** The accessible name, and the tooltip's first line. */
  label: string;
  /** A second tooltip line saying what the thing is for. */
  hint?: string;
  onClick: () => void;
  disabled?: boolean;
  /** Draws the button as engaged — e.g. auto-refresh running. */
  active?: boolean;
  /** The icon. */
  children: ReactNode;
  /** Drawn over the icon's top-right corner — a state dot, say. */
  badge?: ReactNode;
}

/**
 * An icon button in the build view toolbar, with a tooltip that appears
 * at once.
 *
 * The tooltip is the point. Moving four pills and two panels behind icons
 * makes the toolbar far quieter, but it only works if finding out what an
 * icon is costs nothing — which is why it is the shared `Tooltip` (no
 * delay, kept inside the window) rather than the native `title`.
 *
 * The hint is also kept in the DOM, visually hidden, as the button's
 * description: the tooltip exists only while shown, and the description
 * should not depend on hovering.
 */
export function ToolbarButton({
  label,
  hint,
  onClick,
  disabled = false,
  active = false,
  children,
  badge,
}: ToolbarButtonProps) {
  const hintId = useId();

  return (
    <Tooltip
      content={
        <>
          {label}
          {hint && <span className="block text-gray-400">{hint}</span>}
        </>
      }
    >
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        // Only the hint describes; the label is already the name.
        aria-describedby={hint ? hintId : undefined}
        className={`relative rounded-md p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 ${
          active
            ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
            : "text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
        }`}
      >
        {children}
        {badge}
      </button>
      {hint && (
        <span id={hintId} className="sr-only">
          {hint}
        </span>
      )}
    </Tooltip>
  );
}
