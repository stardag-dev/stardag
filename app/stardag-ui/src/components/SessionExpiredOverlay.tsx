import { useState } from "react";
import { useAuth } from "../context/AuthContext";

/**
 * Modal that takes over the screen when the API client has signalled an
 * unrecoverable 401. Replaces the previous silent empty-state UX.
 *
 * The overlay is non-dismissible by design — the user genuinely cannot
 * use the app until they re-authenticate, so any "close" affordance
 * would just lead to more empty states.
 */
export function SessionExpiredOverlay() {
  const { sessionExpired, login, logout } = useAuth();
  const [isLoggingIn, setIsLoggingIn] = useState(false);

  if (!sessionExpired) return null;

  async function handleSignIn() {
    setIsLoggingIn(true);
    try {
      // ``login()`` calls signinRedirect — the browser navigates away to
      // Cognito and comes back to the OIDC callback URL, which finishes
      // the sign-in flow and clears the sessionExpired flag via the
      // AuthContext's user-loaded handler.
      await login();
    } catch (error) {
      console.error("Re-login failed:", error);
      setIsLoggingIn(false);
    }
  }

  async function handleSignOut() {
    // If for some reason the user wants to sign out fully (e.g. they
    // signed in as the wrong account upstream and need to switch),
    // ``logout`` clears local state + redirects to the IdP's logout.
    try {
      await logout();
    } catch (error) {
      console.error("Logout failed:", error);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4"
      role="dialog"
      aria-modal="true"
      aria-labelledby="session-expired-title"
    >
      <div className="w-full max-w-md rounded-lg bg-white p-6 shadow-2xl dark:bg-gray-800">
        <h2
          id="session-expired-title"
          className="text-lg font-semibold text-gray-900 dark:text-gray-100"
        >
          Session expired
        </h2>
        <p className="mt-2 text-sm text-gray-600 dark:text-gray-400">
          Your sign-in has expired or was invalidated. Please sign in again to continue.
        </p>
        <div className="mt-5 flex items-center justify-end gap-3">
          <button
            type="button"
            onClick={handleSignOut}
            className="rounded-md px-3 py-2 text-sm text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            Sign out
          </button>
          <button
            type="button"
            onClick={handleSignIn}
            disabled={isLoggingIn}
            className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-60"
          >
            {isLoggingIn ? "Signing in…" : "Sign in again"}
          </button>
        </div>
      </div>
    </div>
  );
}
