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
  // Get the Keycloak token (for token exchange only)
  getKeycloakToken: () => Promise<string | null>;
  // Get org-scoped access token for API calls
  getAccessToken: (orgId: string | null) => Promise<string | null>;
  // Exchange Keycloak token for org-scoped token
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
  localStorage.setItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${orgId}`, expiresAt.toString());
}

function clearOrgToken(orgId: string): void {
  localStorage.removeItem(`${ACCESS_TOKEN_STORAGE_PREFIX}${orgId}`);
  localStorage.removeItem(`${TOKEN_EXPIRY_STORAGE_PREFIX}${orgId}`);
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [currentTokenOrgId, setCurrentTokenOrgId] = useState<string | null>(null);
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
    await manager.signinRedirect();
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
    await manager.signoutRedirect();
  }, [manager]);

  // Get Keycloak token (for token exchange)
  const getKeycloakToken = useCallback(async (): Promise<string | null> => {
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

  // Exchange Keycloak token for org-scoped token
  const exchangeForOrgToken = useCallback(
    async (orgId: string): Promise<string | null> => {
      // Check cache first
      const cached = getStoredOrgToken(orgId);
      if (cached) {
        setCurrentTokenOrgId(orgId);
        return cached.accessToken;
      }

      // Get Keycloak token
      const keycloakToken = await getKeycloakToken();
      if (!keycloakToken) {
        console.warn("No Keycloak token available for exchange");
        return null;
      }

      setIsExchangingToken(true);
      try {
        const response = await exchangeToken(keycloakToken, orgId);
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
    [getKeycloakToken],
  );

  // Get access token for API calls (org-scoped if orgId provided)
  const getAccessToken = useCallback(
    async (orgId: string | null): Promise<string | null> => {
      // If no org specified, fall back to Keycloak token
      // (only for endpoints that don't need org scope, like /me)
      if (!orgId) {
        return getKeycloakToken();
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
    [getKeycloakToken, exchangeForOrgToken],
  );

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: !!user && !user.expired,
    login,
    logout,
    getKeycloakToken,
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
