import { User, UserManager, WebStorageStateStore, Log } from "oidc-client-ts";
import {
  OIDC_ISSUER,
  OIDC_CLIENT_ID,
  OIDC_REDIRECT_URI,
  OIDC_POST_LOGOUT_REDIRECT_URI,
} from "./config";

// Build version for debugging (set at build time)
const BUILD_VERSION = import.meta.env.VITE_BUILD_VERSION || new Date().toISOString();
console.log(`[OIDC] Build version: ${BUILD_VERSION}`);
console.log(
  `[OIDC] Config: issuer=${OIDC_ISSUER}, clientId=${OIDC_CLIENT_ID}, redirectUri=${OIDC_REDIRECT_URI}`,
);

// Enable oidc-client-ts debug logging
Log.setLogger(console);
Log.setLevel(Log.DEBUG);

// Create UserManager instance (singleton)
let userManager: UserManager | null = null;

export function getUserManager(): UserManager | null {
  if (!OIDC_ISSUER) {
    // Auth not configured - return null
    return null;
  }

  if (!userManager) {
    userManager = new UserManager({
      authority: OIDC_ISSUER,
      client_id: OIDC_CLIENT_ID,
      redirect_uri: OIDC_REDIRECT_URI,
      post_logout_redirect_uri: OIDC_POST_LOGOUT_REDIRECT_URI,
      response_type: "code",
      scope: "openid profile email",
      automaticSilentRenew: true,
      // Use localStorage for both user and state storage to survive redirects
      // (Cognito → GitHub → Cognito → app involves multiple redirects)
      userStore: new WebStorageStateStore({ store: window.localStorage }),
      stateStore: new WebStorageStateStore({ store: window.localStorage }),
    });
  }
  return userManager;
}

// Callback handler for OIDC redirect
export async function handleAuthCallback(): Promise<User | null> {
  const manager = getUserManager();
  if (!manager) return null;

  // Debug: Log current URL and localStorage state
  console.log("[OIDC] handleAuthCallback called");
  console.log("[OIDC] Current URL:", window.location.href);
  console.log("[OIDC] URL search params:", window.location.search);

  // Parse URL params
  const params = new URLSearchParams(window.location.search);
  const code = params.get("code");
  const state = params.get("state");
  const error = params.get("error");
  const errorDescription = params.get("error_description");

  console.log(
    "[OIDC] URL params: code=",
    code ? "present" : "missing",
    "state=",
    state,
    "error=",
    error,
  );

  // Check for OAuth error response from IdP
  if (error) {
    console.error("[OIDC] OAuth error from IdP:", error, errorDescription);
    throw new Error(`OAuth error: ${error} - ${errorDescription}`);
  }

  // Check if we have the required params
  if (!code || !state) {
    console.error("[OIDC] Missing code or state in callback URL");
    throw new Error("Invalid callback: missing code or state parameter");
  }

  // Log all oidc-related keys in localStorage
  const oidcKeys = Object.keys(localStorage).filter((k) => k.startsWith("oidc."));
  console.log("[OIDC] localStorage oidc keys:", oidcKeys);
  oidcKeys.forEach((k) => {
    console.log(`[OIDC] ${k}:`, localStorage.getItem(k));
  });

  // Check if the state from URL matches any stored state
  const expectedStateKey = `oidc.${state}`;
  const storedState = localStorage.getItem(expectedStateKey);
  console.log(
    "[OIDC] Looking for state key:",
    expectedStateKey,
    "Found:",
    storedState ? "yes" : "no",
  );

  try {
    const user = await manager.signinRedirectCallback();
    console.log(
      "[OIDC] Callback successful, user:",
      user?.profile?.email || user?.profile?.sub,
    );
    return user;
  } catch (error) {
    console.error("[OIDC] Auth callback failed:", error);
    // Log more details about the error
    if (error instanceof Error) {
      console.error("[OIDC] Error name:", error.name);
      console.error("[OIDC] Error message:", error.message);
      console.error("[OIDC] Error stack:", error.stack);
    }
    throw error;
  }
}
