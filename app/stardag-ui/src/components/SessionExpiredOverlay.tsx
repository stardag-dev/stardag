import { useState } from "react";
import { useAuth } from "../context/AuthContext";
import { Modal } from "./Modal";

/**
 * Modal that takes over the screen when the API client has signalled an
 * unrecoverable 401. Replaces the previous silent empty-state UX.
 *
 * Non-dismissible by design — the user genuinely cannot use the app
 * until they re-authenticate, so any "close" affordance would just
 * lead to more empty states. We pass a no-op ``onClose`` to ``Modal``
 * and disable the overlay-click + close-button affordances. Pressing
 * Escape will fire the no-op ``onClose`` and the modal stays mounted.
 */
export function SessionExpiredOverlay() {
  const { sessionExpired, login, logout, authMode } = useAuth();
  const [isLoggingIn, setIsLoggingIn] = useState(false);

  async function handleSignIn() {
    setIsLoggingIn(true);
    try {
      if (authMode === "local") {
        // Local mode: sessions can't be silently renewed. Drop the
        // session — the router lands on the sign-in form.
        await logout();
        return;
      }
      // ``login()`` calls signinRedirect — the browser navigates away to
      // Cognito and comes back to the OIDC callback URL, which finishes
      // the sign-in flow and clears the sessionExpired flag via the
      // AuthContext's user-loaded handler.
      await login();
    } catch (error) {
      console.error("Re-login failed:", error);
    } finally {
      // Reset in ``finally`` so the button doesn't stay disabled if
      // ``login()`` resolves without navigating (misconfigured redirect,
      // disabled auth, etc.). In the happy path the page navigates away
      // before this matters.
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
    <Modal
      isOpen={sessionExpired}
      onClose={() => {
        /* non-dismissible */
      }}
      title="Session expired"
      closeOnOverlay={false}
      showCloseButton={false}
    >
      <p className="text-sm text-gray-600 dark:text-gray-400">
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
    </Modal>
  );
}
