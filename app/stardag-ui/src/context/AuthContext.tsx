import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { getUserManager } from "../auth/userManager";
import { exchangeToken, loginLocal, registerLocal } from "../api/auth";
import { getAuthConfig, getCognitoLogoutUrl, isCognitoIssuer } from "../auth/config";
import { API_V1_UI } from "../api/config";

// Storage keys for workspace-scoped tokens
const ACCESS_TOKEN_STORAGE_PREFIX = "stardag_access_token_";
const TOKEN_EXPIRY_STORAGE_PREFIX = "stardag_token_expiry_";
// Storage keys for local-auth session tokens
const SESSION_TOKEN_STORAGE_KEY = "stardag_session_token";
const SESSION_EXPIRY_STORAGE_KEY = "stardag_session_expiry";

interface WorkspaceToken {
  accessToken: string;
  expiresAt: number; // Unix timestamp in ms
}

interface GetAccessTokenOptions {
  /**
   * Skip the localStorage token cache and always re-exchange via the IdP
   * (oidc mode) or the stored session token (local mode). Used by the
   * fetch wrapper after an unexpected 401.
   */
  forceRefresh?: boolean;
}

/**
 * Minimal user shape exposed by the auth context. Structurally satisfied
 * by oidc-client-ts's User; synthesized from /ui/me in local auth mode.
 */
export interface AuthUser {
  profile: {
    sub?: string;
    email?: string;
    name?: string;
    preferred_username?: string;
  };
  expired?: boolean;
}

interface AuthContextType {
  user: AuthUser | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  // Auth mode resolved at boot: "oidc" (external IdP), "local"
  // (email/password against the API), or "disabled".
  authMode: "oidc" | "local" | "disabled";
  // Whether self-service registration is enabled (local mode only)
  registrationEnabled: boolean;
  login: () => Promise<void>;
  // Local-mode login/registration (throws Error with message on failure)
  loginWithPassword: (email: string, password: string) => Promise<void>;
  registerWithPassword: (
    email: string,
    password: string,
    displayName?: string,
  ) => Promise<void>;
  logout: () => Promise<void>;
  // Get the OIDC access token (oidc) / session token (local) for exchange
  getOidcAccessToken: () => Promise<string | null>;
  // Get workspace-scoped access token for API calls.
  // ``forceRefresh: true`` skips the localStorage cache and re-exchanges.
  getAccessToken: (
    workspaceId: string | null,
    opts?: GetAccessTokenOptions,
  ) => Promise<string | null>;
  // Exchange OIDC/session token for workspace-scoped token.
  // ``forceRefresh: true`` skips the localStorage cache and re-exchanges.
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

function clearAllWorkspaceTokens(): void {
  for (const key of Object.keys(localStorage)) {
    if (
      key.startsWith(ACCESS_TOKEN_STORAGE_PREFIX) ||
      key.startsWith(TOKEN_EXPIRY_STORAGE_PREFIX)
    ) {
      localStorage.removeItem(key);
    }
  }
}

// --- Local-auth session token helpers ---

function getStoredSessionToken(): string | null {
  const token = localStorage.getItem(SESSION_TOKEN_STORAGE_KEY);
  const expiry = localStorage.getItem(SESSION_EXPIRY_STORAGE_KEY);
  if (token && expiry && parseInt(expiry, 10) > Date.now() + 30000) {
    return token;
  }
  return null;
}

function storeSessionToken(token: string, expiresIn: number): void {
  localStorage.setItem(SESSION_TOKEN_STORAGE_KEY, token);
  localStorage.setItem(
    SESSION_EXPIRY_STORAGE_KEY,
    (Date.now() + expiresIn * 1000).toString(),
  );
}

function clearSessionToken(): void {
  localStorage.removeItem(SESSION_TOKEN_STORAGE_KEY);
  localStorage.removeItem(SESSION_EXPIRY_STORAGE_KEY);
}

// Fetch the user profile with an explicit bearer token. Used during
// local-mode boot/login, before the API client's token handler is wired.
async function fetchProfileAsAuthUser(token: string): Promise<AuthUser | null> {
  const response = await fetch(`${API_V1_UI}/me`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  if (!response.ok) {
    console.warn(`[Auth] Failed to fetch profile: ${response.status}`);
    return null;
  }
  const data = await response.json();
  return {
    profile: {
      sub: data.user?.id,
      email: data.user?.email,
      name: data.user?.display_name ?? undefined,
    },
  };
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<AuthUser | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [currentTokenWorkspaceId, setCurrentTokenWorkspaceId] = useState<string | null>(
    null,
  );
  const [isExchangingToken, setIsExchangingToken] = useState(false);
  const [sessionExpired, setSessionExpired] = useState(false);

  const authConfig = getAuthConfig();
  const authMode = authConfig.mode;

  const notifySessionExpired = useCallback(() => {
    setSessionExpired(true);
  }, []);

  const clearSessionExpired = useCallback(() => {
    setSessionExpired(false);
  }, []);

  const manager = authMode === "oidc" ? getUserManager() : null;

  // Check for existing session on mount
  useEffect(() => {
    // Local mode: restore session from stored session token
    if (authMode === "local") {
      const token = getStoredSessionToken();
      if (!token) {
        setIsLoading(false);
        return;
      }
      let cancelled = false;
      fetchProfileAsAuthUser(token)
        .then((profile) => {
          if (cancelled) return;
          if (profile) {
            setUser(profile);
          } else {
            clearSessionToken();
          }
        })
        .catch((error) => {
          console.error("Failed to restore local session:", error);
        })
        .finally(() => {
          if (!cancelled) setIsLoading(false);
        });
      return () => {
        cancelled = true;
      };
    }

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
    const handleUserLoaded = (user: AuthUser) => {
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
  }, [manager, authMode]);

  const login = useCallback(async () => {
    if (!manager) {
      console.warn("Auth not configured (or local mode - use loginWithPassword)");
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

  // Local-mode login: store the session token and load the user profile
  const loginWithPassword = useCallback(async (email: string, password: string) => {
    const response = await loginLocal(email, password);
    storeSessionToken(response.session_token, response.expires_in);
    clearAllWorkspaceTokens();
    const profile = await fetchProfileAsAuthUser(response.session_token);
    if (!profile) {
      throw new Error("Signed in, but failed to load the user profile");
    }
    setUser(profile);
    setSessionExpired(false);
  }, []);

  const registerWithPassword = useCallback(
    async (email: string, password: string, displayName?: string) => {
      const response = await registerLocal(email, password, displayName);
      storeSessionToken(response.session_token, response.expires_in);
      clearAllWorkspaceTokens();
      const profile = await fetchProfileAsAuthUser(response.session_token);
      if (!profile) {
        throw new Error("Registered, but failed to load the user profile");
      }
      setUser(profile);
      setSessionExpired(false);
    },
    [],
  );

  const logout = useCallback(async () => {
    // Clear all stored workspace tokens
    clearAllWorkspaceTokens();
    setCurrentTokenWorkspaceId(null);

    // Local mode: drop the session and return to the login screen
    if (authMode === "local") {
      clearSessionToken();
      setUser(null);
      setSessionExpired(false);
      return;
    }

    if (!manager) {
      console.warn("Auth not configured");
      return;
    }

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
  }, [manager, authMode]);

  // Get OIDC ID token (contains user claims like email, name)
  // Used for bootstrap endpoints (/ui/me, /ui/me/invites) before workspace selection
  // Local mode: the session token plays this role.
  const getIdToken = useCallback(async (): Promise<string | null> => {
    if (authMode === "local") {
      return getStoredSessionToken();
    }
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
  }, [manager, authMode]);

  // Force a silent renew of the IdP session and return the renewed
  // ID token. Distinguished from ``getIdToken`` because the API client's
  // 401 retry path can't trust whatever's already cached locally — the
  // cached id_token may itself be the reason we got the 401 (very rare,
  // e.g. clock skew or Cognito-side revocation).
  // Local mode: session tokens can't be renewed; return it while valid.
  const getRefreshedIdToken = useCallback(async (): Promise<string | null> => {
    if (authMode === "local") {
      return getStoredSessionToken();
    }
    if (!manager) return null;
    try {
      const renewedUser = await manager.signinSilent();
      return renewedUser?.id_token ?? null;
    } catch {
      return null;
    }
  }, [manager, authMode]);

  // Get OIDC access token (oidc) / session token (local) for token exchange
  const getOidcAccessToken = useCallback(async (): Promise<string | null> => {
    if (authMode === "local") {
      return getStoredSessionToken();
    }
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
  }, [manager, authMode]);

  // Force a silent renew and return the renewed access token. Sibling to
  // ``getRefreshedIdToken``; used by ``exchangeForWorkspaceToken`` when
  // the caller asked for ``forceRefresh: true``.
  const getRefreshedOidcAccessToken = useCallback(async (): Promise<string | null> => {
    if (authMode === "local") {
      return getStoredSessionToken();
    }
    if (!manager) return null;
    try {
      const renewedUser = await manager.signinSilent();
      return renewedUser?.access_token ?? null;
    } catch {
      return null;
    }
  }, [manager, authMode]);

  // Exchange OIDC/session token for workspace-scoped token.
  // ``forceRefresh: true`` skips the localStorage cache and re-exchanges —
  // used after an unexpected 401 to recover from a stale cached internal
  // token (e.g. signed by a prior API container's keypair or invalidated
  // by a server-side rotation).
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

      // Get the exchange credential. If forceRefresh is set, prefer a
      // freshly-renewed IdP token rather than whatever is in the user
      // manager (the cached one might also be stale relative to the API).
      const exchangeCredential = opts.forceRefresh
        ? await getRefreshedOidcAccessToken()
        : await getOidcAccessToken();
      if (!exchangeCredential) {
        console.warn("No token available for exchange");
        return null;
      }

      setIsExchangingToken(true);
      try {
        const response = await exchangeToken(exchangeCredential, workspaceId);
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
      // If no workspace specified, use ID token (oidc) / session token
      // (local) for bootstrap endpoints
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
    authMode,
    registrationEnabled: authConfig.localRegistrationEnabled,
    login,
    loginWithPassword,
    registerWithPassword,
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
