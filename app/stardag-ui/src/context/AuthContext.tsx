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

// Storage keys for org-scoped tokens
const ACCESS_TOKEN_STORAGE_PREFIX = "stardag_access_token_";
const TOKEN_EXPIRY_STORAGE_PREFIX = "stardag_token_expiry_";

interface OrgToken {
  accessToken: string;
  expiresAt: number; // Unix timestamp in ms
}

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
  // Get the OIDC access token (for token exchange only)
  getOidcAccessToken: () => Promise<string | null>;
  // Get org-scoped access token for API calls
  getAccessToken: (orgId: string | null) => Promise<string | null>;
  // Exchange OIDC token for org-scoped token
  exchangeForOrgToken: (orgId: string) => Promise<string | null>;
  // Current org for which we have a valid token
  currentTokenOrgId: string | null;
  // Token exchange in progress
  isExchangingToken: boolean;
}

const AuthContext = createContext<AuthContextType | null>(null);

interface AuthProviderProps {
  children: ReactNode;
}

// Helper to get/set org tokens from localStorage
function getStoredOrgToken(orgId: string): OrgToken | null {
  const token = localStorage.getItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${orgId}`);
  const expiry = localStorage.getItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${orgId}`);
  if (token && expiry) {
    const expiresAt = parseInt(expiry, 10);
    // Check if not expired (with 30s buffer)
    if (expiresAt > Date.now() + 30000) {
      return { accessToken: token, expiresAt };
    }
  }
  return null;
}

function storeOrgToken(orgId: string, token: string, expiresIn: number): void {
  const expiresAt = Date.now() + expiresIn * 1000;
  localStorage.setItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${orgId}`, token);
  localStorage.setItem(
    `${TOKEN_EXPIRY_STORAGE_PREFIX}${orgId}`,
    expiresAt.toString(),
  );
}

function clearOrgToken(orgId: string): void {
  localStorage.removeItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${orgId}`);
  localStorage.removeItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${orgId}`);
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [currentTokenOrgId, setCurrentTokenOrgId] = useState<string | null>(
    null,
  );
  const [isExchangingToken, setIsExchangingToken] = useState(false);

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
    const handleUserLoaded = (user: User) => setUser(user);
    const handleUserUnloaded = () => {
      setUser(null);
      setCurrentTokenOrgId(null);
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
    const oidcKeys = Object.keys(localStorage).filter((k) =>
      k.startsWith("oidc."),
    );
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
    // Clear all stored org tokens
    for (const key of Object.keys(localStorage)) {
      if (
        key.startsWith(ACCESS_TOKEN_STORAGE_PREFIX) ||
        key.startsWith(TOKEN_EXPIRY_STORAGE_PREFIX)
      ) {
        localStorage.removeItem(key);
      }
    }
    setCurrentTokenOrgId(null);

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
  // Used for bootstrap endpoints (/ui/me, /ui/me/invites) before org selection
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

  // Exchange OIDC token for org-scoped token
  const exchangeForOrgToken = useCallback(
    async (orgId: string): Promise<string | null> => {
      // Check cache first
      const cached = getStoredOrgToken(orgId);
      if (cached) {
        setCurrentTokenOrgId(orgId);
        return cached.accessToken;
      }

      // Get OIDC access token
      const oidcToken = await getOidcAccessToken();
      if (!oidcToken) {
        console.warn("No OIDC token available for exchange");
        return null;
      }

      setIsExchangingToken(true);
      try {
        const response = await exchangeToken(oidcToken, orgId);
        storeOrgToken(orgId, response.access_token, response.expires_in);
        setCurrentTokenOrgId(orgId);
        return response.access_token;
      } catch (error) {
        console.error("Token exchange failed:", error);
        clearOrgToken(orgId);
        return null;
      } finally {
        setIsExchangingToken(false);
      }
    },
    [getOidcAccessToken],
  );

  // Get access token for API calls (org-scoped if orgId provided)
  const getAccessToken = useCallback(
    async (orgId: string | null): Promise<string | null> => {
      // If no org specified, use ID token for bootstrap endpoints
      // (ID token contains email/name claims needed by /ui/me endpoints)
      // NOTE: Access token doesn't have user claims in Cognito
      if (!orgId) {
        return getIdToken();
      }

      // Check if we have a valid cached token for this org
      const cached = getStoredOrgToken(orgId);
      if (cached) {
        setCurrentTokenOrgId(orgId);
        return cached.accessToken;
      }

      // Need to exchange for a new token
      return exchangeForOrgToken(orgId);
    },
    [getIdToken, exchangeForOrgToken],
  );

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: !!user && !user.expired,
    login,
    logout,
    getOidcAccessToken,
    getAccessToken,
    exchangeForOrgToken,
    currentTokenOrgId,
    isExchangingToken,
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
