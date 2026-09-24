import type { ReactNode } from "react";

/** A spinner with its words, for a read in flight. */
export function Spinner({ children }: { children: ReactNode }) {
  return (
    <p
      role="status"
      className="flex items-center gap-2 text-sm text-gray-600 dark:text-gray-400"
    >
      <span
        aria-hidden="true"
        className="h-4 w-4 animate-spin rounded-full border-2 border-blue-500 border-t-transparent"
      />
      <span>{children}</span>
    </p>
  );
}
