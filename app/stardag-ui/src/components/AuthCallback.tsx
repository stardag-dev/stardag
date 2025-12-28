import { useEffect, useState, useRef } from "react";
import { handleAuthCallback } from "../auth/userManager";

interface AuthCallbackProps {
  onSuccess: () => void;
  onError: (error: Error) => void;
}

export function AuthCallback({ onSuccess, onError }: AuthCallbackProps) {
  const [error, setError] = useState<string | null>(null);
  const processedRef = useRef(false);

  useEffect(() => {
    // Prevent double-processing (React StrictMode or re-renders)
    if (processedRef.current) {
      console.log("[AuthCallback] Already processed, skipping");
      return;
    }
    processedRef.current = true;

    async function processCallback() {
      try {
        await handleAuthCallback();
        // Clear URL params after successful callback to prevent re-processing on refresh
        window.history.replaceState({}, "", window.location.pathname);
        onSuccess();
      } catch (err) {
        const errorMessage =
          err instanceof Error ? err.message : "Authentication failed";
        setError(errorMessage);
        onError(err instanceof Error ? err : new Error(errorMessage));
      }
    }

    processCallback();
  }, [onSuccess, onError]);

  if (error) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="rounded-lg bg-white dark:bg-gray-800 p-6 shadow-lg max-w-md">
          <h2 className="text-lg font-semibold text-red-600 dark:text-red-400 mb-2">
            Authentication Error
          </h2>
          <p className="text-gray-600 dark:text-gray-300">{error}</p>
          <button
            onClick={() => (window.location.href = "/")}
            className="mt-4 px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Return Home
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
      <div className="text-center">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mx-auto mb-4"></div>
        <p className="text-gray-600 dark:text-gray-300">Completing authentication...</p>
      </div>
    </div>
  );
}
