import type { ReactNode } from "react";

export type ResultBannerTone = "success" | "error" | "warning" | "info";

interface ResultBannerProps {
  tone: ResultBannerTone;
  children: ReactNode;
  onDismiss?: () => void;
  className?: string;
}

const TONE_STYLES: Record<ResultBannerTone, string> = {
  success:
    "border-green-200 bg-green-50 text-green-800 dark:border-green-900 dark:bg-green-900/20 dark:text-green-300",
  error:
    "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-900/20 dark:text-red-400",
  warning:
    "border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-900 dark:bg-amber-900/20 dark:text-amber-300",
  info: "border-blue-200 bg-blue-50 text-blue-800 dark:border-blue-900 dark:bg-blue-900/20 dark:text-blue-300",
};

/**
 * Inline outcome message — "cancelled 4 builds, released 11 claims", or
 * the error that stopped it.
 *
 * Inline rather than a floating toast on purpose: the outcome of a bulk
 * mutation belongs next to the data it changed, and stays put long enough
 * to be read and cross-checked against the refreshed table.
 *
 * Errors are announced assertively; everything else politely.
 */
export function ResultBanner({
  tone,
  children,
  onDismiss,
  className = "",
}: ResultBannerProps) {
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      aria-live={tone === "error" ? "assertive" : "polite"}
      className={`flex items-start gap-3 rounded-md border px-3 py-2 text-sm ${TONE_STYLES[tone]} ${className}`}
    >
      <div className="min-w-0 flex-1">{children}</div>
      {onDismiss && (
        <button
          onClick={onDismiss}
          aria-label="Dismiss message"
          className="-mr-1 rounded p-0.5 opacity-60 hover:opacity-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-current"
        >
          <svg
            className="h-4 w-4"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={2}
              d="M6 18L18 6M6 6l12 12"
            />
          </svg>
        </button>
      )}
    </div>
  );
}
