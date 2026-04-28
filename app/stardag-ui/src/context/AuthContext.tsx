import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { User } from "oidc-client-ts";
import { getUserManager } from "../auth/userManager";
import { exchangeToken } from "../api/auth";
import { getCognitoLogoutUrl, isCognitoIssuer } from "../auth/config";

// Storage keys for workspace-scoped tokens
const ACCESS_TOKEN_STORAGE_PREFIX = "stardag_access_token_";
const TOKEN_EXPIRY_STORAGE_PREFIX = "stardag_token_expiry_";

interface WorkspaceToken {
  accessToken: string;
  expiresAt: number; // Unix timestamp in ms
}

interface GetAccessTokenOptions {
  /**
   * Skip the localStorage token cache and always re-exchange via Cognito.
   * Used by the fetch wrapper after an unexpected 401.
   */
  forceRefresh?: boolean;
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
  // Get the OIDC access token (for token exchange only)
  getOidcAccessToken: () => Promise<string | null>;
  // Get workspace-scoped access token for API calls.
  // ``forceRefresh: true`` skips the localStorage cache and re-exchanges.
  getAccessToken: (
    workspaceId: string | null,
    opts?: GetAccessTokenOptions,
  ) => Promise<string | null>;
  // Exchange OIDC token for workspace-scoped token. ``forceRefresh: true``
  // skips the localStorage cache and re-exchanges from a freshly-renewed
  // Cognito session.
  exchangeForWorkspaceToken: (
    workspaceId: string,
    opts?: GetAccessTokenOptions,
  ) => Promise<string | null>;
  // Current workspace for which we have a valid token
  currentTokenWorkspaceId: string | null;
  // Token exchange in progress
  isExchangingToken: boolean;
  // True when the API client signalled an unrecoverable 401. The UI shows
  // a "session expired" overlay; clears on successful re-login.
  sessionExpired: boolean;
  // Imperatively flag the session as expired (used by the API client's
  // 401 retry path).
  notifySessionExpired: () => void;
  // Clear the flag — call after a successful re-login so the overlay
  // doesn't keep shadowing the app.
  clearSessionExpired: () => void;
}

const AuthContext = createContext<AuthContextType | null>(null);

interface AuthProviderProps {
  children: ReactNode;
}

// Helper to get/set workspace tokens from localStorage
function getStoredWorkspaceToken(workspaceId: string): WorkspaceToken | null {
  const token = localStorage.getItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${workspaceId}`);
  const expiry = localStorage.getItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${workspaceId}`);
  if (token && expiry) {
    const expiresAt = parseInt(expiry, 10);
    // Check if not expired (with 30s buffer)
    if (expiresAt > Date.now() + 30000) {
      return { accessToken: token, expiresAt };
    }
  }
  return null;
}

function storeWorkspaceToken(
  workspaceId: string,
  token: string,
  expiresIn: number,
): void {
  const expiresAt = Date.now() + expiresIn * 1000;
  localStorage.setItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${workspaceId}`, token);
  localStorage.setItem(
    `${TOKEN_EXPIRY_STORAGE_PREFIX}${workspaceId}`,
    expiresAt.toString(),
  );
}

function clearWorkspaceToken(workspaceId: string): void {
  localStorage.removeItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${workspaceId}`);
  localStorage.removeItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${workspaceId}`);
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [currentTokenWorkspaceId, setCurrentTokenWorkspaceId] = useState<string | null>(
    null,
  );
  const [isExchangingToken, setIsExchangingToken] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  const notifySessionExpired = useCallback(() => {
    setSessionExpired(true);
  }, []);

  const clearSessionExpired = useCallback(() => {
    setSessionExpired(false);
  }, []);

  const manager = getUserManager();

  // Check for existing session on mount
  useEffect(() => {
    if (!manager) {
      setIsLoading(false);
      return;
    }

    async function loadUser() {
      try {
        const user = await manager!.getUser();
        if (user && !user.expired) {
          setUser(user);
        }
      } catch (error) {
        console.error("Failed to load user:", error);
      } finally {
        setIsLoading(false);
      }
    }

    loadUser();

    // Listen for user changes (e.g., token refresh)
    const handleUserLoaded = (user: User) => {
      setUser(user);
      // A fresh user load (silent renew completed, redirect callback
      // returned, etc.) means the session is healthy again — clear any
      // stale "session expired" flag so the overlay disappears.
      setSessionExpired(false);
    };
    const handleUserUnloaded = () => {
      setUser(null);
      setCurrentTokenWorkspaceId(null);
    };

    manager.events.addUserLoaded(handleUserLoaded);
    manager.events.addUserUnloaded(handleUserUnloaded);

    return () => {
      manager.events.removeUserLoaded(handleUserLoaded);
      manager.events.removeUserUnloaded(handleUserUnloaded);
    };
  }, [manager]);

  const login = useCallback(async () => {
    if (!manager) {
      console.warn("Auth not configured");
      return;
    }
    console.log("[OIDC] Login initiated, calling signinRedirect...");
    // Log localStorage state before redirect
    const oidcKeys = Object.keys(localStorage).filter((k) => k.startsWith("oidc."));
    console.log("[OIDC] localStorage oidc keys before redirect:", oidcKeys);
    await manager.signinRedirect();
    // Note: This line won't execute since signinRedirect navigates away
    console.log("[OIDC] signinRedirect completed (shouldn't see this)");
  }, [manager]);

  const logout = useCallback(async () => {
    if (!manager) {
      console.warn("Auth not configured");
      return;
    }
    // Clear all stored workspace tokens
    for (const key of Object.keys(localStorage)) {
      if (
        key.startsWith(ACCESS_TOKEN_STORAGE_PREFIX) ||
        key.startsWith(TOKEN_EXPIRY_STORAGE_PREFIX)
      ) {
        localStorage.removeItem(key);
      }
    }
    setCurrentTokenWorkspaceId(null);

    // Handle Cognito logout specially since it doesn't follow standard OIDC logout
    // Cognito requires client_id and uses logout_uri instead of post_logout_redirect_uri
    if (isCognitoIssuer()) {
      const cognitoLogoutUrl = getCognitoLogoutUrl();
      if (cognitoLogoutUrl) {
        console.log("[Auth] Using Cognito-specific logout URL");
        // Remove user from local storage first
        await manager.removeUser();
        // Redirect to Cognito logout endpoint
        window.location.href = cognitoLogoutUrl;
        return;
      }
      console.warn(
        "[Auth] Cognito domain not configured, falling back to standard logout",
      );
    }

    // Standard OIDC logout for Keycloak and other providers
    await manager.signoutRedirect();
  }, [manager]);

  // Get OIDC ID token (contains user claims like email, name)
  // Used for bootstrap endpoints (/ui/me, /ui/me/invites) before workspace selection
  const getIdToken = useCallback(async (): Promise<string | null> => {
    if (!manager) return null;

    try {
      const user = await manager.getUser();
      if (user && !user.expired && user.id_token) {
        console.log("[Auth] Using ID token for bootstrap endpoint");
        return user.id_token;
      }
      // Try silent renew
      const renewedUser = await manager.signinSilent();
      return renewedUser?.id_token ?? null;
    } catch {
      return null;
    }
  }, [manager]);

  // Force a silent renew of the Cognito session and return the renewed
  // ID token. Distinguished from ``getIdToken`` because the API client's
  // 401 retry path can't trust whatever's already cached locally — the
  // cached id_token may itself be the reason we got the 401 (very rare,
  // e.g. clock skew or Cognito-side revocation).
  const getRefreshedIdToken = useCallback(async (): Promise<string | null> => {
    if (!manager) return null;
    try {
      const renewedUser = await manager.signinSilent();
      return renewedUser?.id_token ?? null;
    } catch {
      return null;
    }
  }, [manager]);

  // Get OIDC access token (for token exchange)
  const getOidcAccessToken = useCallback(async (): Promise<string | null> => {
    if (!manager) return null;

    try {
      const user = await manager.getUser();
      if (user && !user.expired) {
        return user.access_token;
      }
      // Try silent renew
      const renewedUser = await manager.signinSilent();
      return renewedUser?.access_token ?? null;
    } catch {
      return null;
    }
  }, [manager]);

  // Force a silent renew and return the renewed access token. Sibling to
  // ``getRefreshedIdToken``; used by ``exchangeForWorkspaceToken`` when
  // the caller asked for ``forceRefresh: true``.
  const getRefreshedOidcAccessToken = useCallback(async (): Promise<string | null> => {
    if (!manager) return null;
    try {
      const renewedUser = await manager.signinSilent();
      return renewedUser?.access_token ?? null;
    } catch {
      return null;
    }
  }, [manager]);

  // Exchange OIDC token for workspace-scoped token. ``forceRefresh: true``
  // skips the localStorage cache and re-exchanges with the Cognito-provided
  // OIDC token — used after an unexpected 401 to recover from a stale
  // cached internal token (e.g. signed by a prior API container's keypair
  // or invalidated by a server-side rotation).
  const exchangeForWorkspaceToken = useCallback(
    async (
      workspaceId: string,
      opts: GetAccessTokenOptions = {},
    ): Promise<string | null> => {
      if (!opts.forceRefresh) {
        const cached = getStoredWorkspaceToken(workspaceId);
        if (cached) {
          setCurrentTokenWorkspaceId(workspaceId);
          return cached.accessToken;
        }
      } else {
        // Drop the stale cache entry so subsequent reads don't accidentally
        // pick it up again.
        clearWorkspaceToken(workspaceId);
      }

      // Get OIDC access token. If forceRefresh is set, prefer a freshly-
      // renewed Cognito token rather than whatever is in the user manager
      // (the cached one might also be stale relative to the API).
      const oidcToken = opts.forceRefresh
        ? await getRefreshedOidcAccessToken()
        : await getOidcAccessToken();
      if (!oidcToken) {
        console.warn("No OIDC token available for exchange");
        return null;
      }

      setIsExchangingToken(true);
      try {
        const response = await exchangeToken(oidcToken, workspaceId);
        storeWorkspaceToken(workspaceId, response.access_token, response.expires_in);
        setCurrentTokenWorkspaceId(workspaceId);
        return response.access_token;
      } catch (error) {
        console.error("Token exchange failed:", error);
        clearWorkspaceToken(workspaceId);
        return null;
      } finally {
        setIsExchangingToken(false);
      }
    },
    [getOidcAccessToken, getRefreshedOidcAccessToken],
  );

  // Get access token for API calls (workspace-scoped if workspaceId provided)
  const getAccessToken = useCallback(
    async (
      workspaceId: string | null,
      opts: GetAccessTokenOptions = {},
    ): Promise<string | null> => {
      // If no workspace specified, use ID token for bootstrap endpoints
      // (ID token contains email/name claims needed by /ui/me endpoints)
      // NOTE: Access token doesn't have user claims in Cognito
      if (!workspaceId) {
        return opts.forceRefresh ? getRefreshedIdToken() : getIdToken();
      }

      if (!opts.forceRefresh) {
        // Check if we have a valid cached token for this workspace
        const cached = getStoredWorkspaceToken(workspaceId);
        if (cached) {
          setCurrentTokenWorkspaceId(workspaceId);
          return cached.accessToken;
        }
      }

      // Need to (re-)exchange for a new token
      return exchangeForWorkspaceToken(workspaceId, opts);
    },
    [getIdToken, getRefreshedIdToken, exchangeForWorkspaceToken],
  );

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: !!user && !user.expired,
    login,
    logout,
    getOidcAccessToken,
    getAccessToken,
    exchangeForWorkspaceToken,
    currentTokenWorkspaceId,
    isExchangingToken,
    sessionExpired,
    notifySessionExpired,
    clearSessionExpired,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): AuthContextType {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth must be used within an AuthProvider");
  }
  return context;
}
