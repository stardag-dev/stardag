import { useEffect, useState } from "react";
import { fetchServerVersion } from "../api/version";

/**
 * Small muted footer line showing the server release version.
 *
 * Fetches GET /api/v1/version lazily on mount and renders nothing while
 * loading or when the fetch fails - it must never break the page it sits on.
 */
export function ServerVersionFooter() {
  const [version, setVersion] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchServerVersion()
      .then((v) => {
        if (!cancelled) setVersion(v.server_version);
      })
      .catch(() => {
        // Omit the footer entirely on error.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!version) return null;

  return (
    <p className="mx-auto max-w-4xl px-4 pb-6 text-xs text-gray-400 dark:text-gray-500">
      Stardag server v{version}
    </p>
  );
}
