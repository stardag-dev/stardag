import { User, UserManager, WebStorageStateStore } from "oidc-client-ts";
import {
  OIDC_ISSUER,
  OIDC_CLIENT_ID,
  OIDC_REDIRECT_URI,
  OIDC_POST_LOGOUT_REDIRECT_URI,
} from "./config";

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
      userStore: new WebStorageStateStore({ store: window.localStorage }),
    });
  }
  return userManager;
}

// Callback handler for OIDC redirect
export async function handleAuthCallback(): Promise<User | null> {
  const manager = getUserManager();
  if (!manager) return null;

  try {
    const user = await manager.signinRedirectCallback();
    return user;
  } catch (error) {
    console.error("Auth callback failed:", error);
    throw error;
  }
}
