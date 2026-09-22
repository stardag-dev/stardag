import { useId, type ReactNode } from "react";

interface ToolbarButtonProps {
  /** The accessible name, and the tooltip's first line. */
  label: string;
  /** A second tooltip line saying what the thing is for. */
  hint?: string;
  onClick: () => void;
  disabled?: boolean;
  /** Draws the button as engaged — e.g. auto-refresh running. */
  active?: boolean;
  /**
   * Which edge the tooltip aligns to. The rightmost buttons in a toolbar
   * need "right" or a centred tooltip runs off the window.
   */
  align?: "center" | "right";
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
 * icon is costs nothing — and the native `title` attribute takes about a
 * second to appear, which is long enough that you give up and click to
 * find out. This shows in 100ms, which reads as instant.
 *
 * It is CSS-only: no timers, no state, nothing to clean up on unmount,
 * and it appears on keyboard focus as well as hover because
 * `group-focus-within` costs nothing extra. The tooltip stays in the DOM
 * and is wired up with `aria-describedby`, so it is the button's
 * description rather than something only sighted users get.
 *
 * Callers must not also set `title`, or the browser's slow tooltip
 * appears underneath this one saying the same thing.
 */
export function ToolbarButton({
  label,
  hint,
  onClick,
  disabled = false,
  active = false,
  align = "center",
  children,
  badge,
}: ToolbarButtonProps) {
  const tooltipId = useId();

  return (
    <span className="group relative inline-flex">
      <button
        type="button"
        onClick={onClick}
        disabled={disabled}
        aria-label={label}
        // Only the hint describes. Pointing this at the whole tooltip
        // made every button announce as "Refresh, button, Refresh
        // Double-click to…" — the name read twice, once as itself and
        // once as its own description.
        aria-describedby={hint ? tooltipId : undefined}
        className={`relative rounded-md p-1 transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-blue-500 disabled:opacity-50 ${
          active
            ? "bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300"
            : "text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
        }`}
      >
        {children}
        {badge}
      </button>
      <span
        role="tooltip"
        className={`pointer-events-none absolute top-full z-30 mt-1.5 w-max max-w-64 rounded-md bg-gray-900 px-2 py-1 text-xs text-gray-100 opacity-0 shadow-lg transition-opacity delay-100 duration-75 group-focus-within:opacity-100 group-hover:opacity-100 dark:bg-gray-700 ${
          align === "right" ? "right-0" : "left-1/2 -translate-x-1/2"
        }`}
      >
        {label}
        {hint && (
          <span id={tooltipId} className="block text-gray-400">
            {hint}
          </span>
        )}
      </span>
    </span>
  );
}
