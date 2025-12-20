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

interface AuthContextType {
  user: User | null;
  isLoading: boolean;
  isAuthenticated: boolean;
  login: () => Promise<void>;
  logout: () => Promise<void>;
  getAccessToken: () => Promise<string | null>;
}

const AuthContext = createContext<AuthContextType | null>(null);

interface AuthProviderProps {
  children: ReactNode;
}

export function AuthProvider({ children }: AuthProviderProps) {
  const [user, setUser] = useState<User | null>(null);
  const [isLoading, setIsLoading] = useState(true);

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
    const handleUserUnloaded = () => setUser(null);

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
    await manager.signoutRedirect();
  }, [manager]);

  const getAccessToken = useCallback(async (): Promise<string | null> => {
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

  const value: AuthContextType = {
    user,
    isLoading,
    isAuthenticated: !!user && !user.expired,
    login,
    logout,
    getAccessToken,
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
