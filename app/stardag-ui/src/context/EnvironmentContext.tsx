import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import {
  fetchUserProfile,
  fetchEnvironments,
  type WorkspaceSummary,
  type Environment,
} from "../api/workspaces";
import { setCurrentWorkspaceId } from "../api/client";
import { useAuth } from "./AuthContext";

interface EnvironmentContextType {
  // User's workspaces
  workspaces: WorkspaceSummary[];
  // Active workspace
  activeWorkspace: WorkspaceSummary | null;
  setActiveWorkspace: (workspace: WorkspaceSummary | null) => void;
  // Environments in active workspace
  environments: Environment[];
  // Active environment
  activeEnvironment: Environment | null;
  setActiveEnvironment: (environment: Environment | null) => void;
  // Loading state
  isLoading: boolean;
  // Token exchange in progress
  isExchangingToken: boolean;
  // Refresh data, optionally selecting a specific workspace/environment by slug
  refresh: (
    preferWorkspaceSlug?: string,
    preferEnvironmentSlug?: string,
  ) => Promise<void>;
  // User's role in active workspace
  activeWorkspaceRole: "owner" | "admin" | "member" | null;
  // Get the current URL path for workspace/environment
  getEnvironmentPath: () => string;
}

const EnvironmentContext = createContext<EnvironmentContextType | null>(null);

const STORAGE_KEY_WORKSPACE = "stardag_active_workspace_id";
const STORAGE_KEY_ENVIRONMENT = "stardag_active_environment_id";

interface EnvironmentProviderProps {
  children: ReactNode;
}

export function EnvironmentProvider({ children }: EnvironmentProviderProps) {
  const { isAuthenticated, user, exchangeForWorkspaceToken, isExchangingToken } =
    useAuth();

  const [workspaces, setWorkspaces] = useState<WorkspaceSummary[]>([]);
  const [activeWorkspace, setActiveWorkspaceState] = useState<WorkspaceSummary | null>(
    null,
  );
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [activeEnvironment, setActiveEnvironmentState] = useState<Environment | null>(
    null,
  );
  const [isLoading, setIsLoading] = useState(false);

  // Update client's current workspace ID when activeWorkspace changes
  useEffect(() => {
    setCurrentWorkspaceId(activeWorkspace?.id ?? null);
  }, [activeWorkspace]);

  // Load user's workspaces when authenticated
  const loadWorkspaces = useCallback(
    async (preferWorkspaceSlug?: string, preferEnvironmentSlug?: string) => {
      if (!isAuthenticated) {
        setWorkspaces([]);
        setActiveWorkspaceState(null);
        setEnvironments([]);
        setActiveEnvironmentState(null);
        setCurrentWorkspaceId(null);
        return;
      }

      setIsLoading(true);
      try {
        const profile = await fetchUserProfile();
        setWorkspaces(profile.workspaces);

        // Priority for selecting workspace: 1) preferWorkspaceSlug, 2) localStorage, 3) first workspace
        let workspaceToActivate: WorkspaceSummary | null = null;
        if (preferWorkspaceSlug) {
          workspaceToActivate =
            profile.workspaces.find((w) => w.slug === preferWorkspaceSlug) || null;
        }
        if (!workspaceToActivate) {
          const savedWorkspaceId = localStorage.getItem(STORAGE_KEY_WORKSPACE);
          workspaceToActivate =
            profile.workspaces.find((w) => w.id === savedWorkspaceId) ||
            profile.workspaces[0] ||
            null;
        }

        if (workspaceToActivate) {
          setActiveWorkspaceState(workspaceToActivate);
          localStorage.setItem(STORAGE_KEY_WORKSPACE, workspaceToActivate.id);
          setCurrentWorkspaceId(workspaceToActivate.id);

          // Exchange for workspace-scoped token
          await exchangeForWorkspaceToken(workspaceToActivate.id);

          // Load environments for this workspace
          const envs = await fetchEnvironments(workspaceToActivate.id);
          setEnvironments(envs);

          // Priority for environment: 1) preferEnvironmentSlug, 2) localStorage, 3) first
          let envToActivate: Environment | null = null;
          if (preferEnvironmentSlug) {
            envToActivate = envs.find((e) => e.slug === preferEnvironmentSlug) || null;
          }
          if (!envToActivate) {
            const savedEnvironmentId = localStorage.getItem(STORAGE_KEY_ENVIRONMENT);
            envToActivate =
              envs.find((e) => e.id === savedEnvironmentId) || envs[0] || null;
          }

          setActiveEnvironmentState(envToActivate);
          if (envToActivate) {
            localStorage.setItem(STORAGE_KEY_ENVIRONMENT, envToActivate.id);
          }
        }
      } catch (error) {
        console.error("Failed to load workspaces:", error);
      } finally {
        setIsLoading(false);
      }
    },
    [isAuthenticated, exchangeForWorkspaceToken],
  );

  useEffect(() => {
    // Parse URL to get initial workspace/environment slugs
    const path = window.location.pathname;
    const knownRoutes = ["/callback", "/settings", "/invites", "/workspaces/new"];
    let workspaceSlug: string | undefined;
    let environmentSlug: string | undefined;

    if (!knownRoutes.some((r) => path.startsWith(r))) {
      const parts = path.split("/").filter(Boolean);
      workspaceSlug = parts[0] || undefined;
      environmentSlug = parts[1] || undefined;
    }

    loadWorkspaces(workspaceSlug, environmentSlug);
  }, [loadWorkspaces, user]);

  // Set active workspace
  const setActiveWorkspace = useCallback(
    async (workspace: WorkspaceSummary | null) => {
      setActiveWorkspaceState(workspace);
      if (workspace) {
        localStorage.setItem(STORAGE_KEY_WORKSPACE, workspace.id);
        setCurrentWorkspaceId(workspace.id);

        // Exchange for workspace-scoped token
        await exchangeForWorkspaceToken(workspace.id);

        // Load environments for this workspace
        try {
          const envs = await fetchEnvironments(workspace.id);
          setEnvironments(envs);
          // Select first environment
          const firstEnvironment = envs[0] || null;
          setActiveEnvironmentState(firstEnvironment);
          if (firstEnvironment) {
            localStorage.setItem(STORAGE_KEY_ENVIRONMENT, firstEnvironment.id);
          } else {
            localStorage.removeItem(STORAGE_KEY_ENVIRONMENT);
          }

          // Navigate to home page when switching workspaces to avoid
          // "not found" errors from workspace-specific resources (builds, tasks, etc.)
          window.history.pushState({}, "", "/");
          window.dispatchEvent(new PopStateEvent("popstate"));
        } catch (error) {
          console.error("Failed to load environments:", error);
          setEnvironments([]);
          setActiveEnvironmentState(null);
        }
      } else {
        localStorage.removeItem(STORAGE_KEY_WORKSPACE);
        localStorage.removeItem(STORAGE_KEY_ENVIRONMENT);
        setCurrentWorkspaceId(null);
        setEnvironments([]);
        setActiveEnvironmentState(null);
      }
    },
    [exchangeForWorkspaceToken],
  );

  // Set active environment
  const setActiveEnvironment = useCallback((environment: Environment | null) => {
    setActiveEnvironmentState(environment);
    if (environment) {
      localStorage.setItem(STORAGE_KEY_ENVIRONMENT, environment.id);
    } else {
      localStorage.removeItem(STORAGE_KEY_ENVIRONMENT);
    }
  }, []);

  // Get URL path for current workspace/environment
  const getEnvironmentPath = useCallback(() => {
    if (!activeWorkspace) return "/";
    if (!activeEnvironment || activeEnvironment.slug === "default") {
      return `/${activeWorkspace.slug}`;
    }
    return `/${activeWorkspace.slug}/${activeEnvironment.slug}`;
  }, [activeWorkspace, activeEnvironment]);

  const value: EnvironmentContextType = {
    workspaces,
    activeWorkspace,
    setActiveWorkspace,
    environments,
    activeEnvironment,
    setActiveEnvironment,
    isLoading,
    isExchangingToken,
    refresh: loadWorkspaces,
    activeWorkspaceRole: activeWorkspace?.role || null,
    getEnvironmentPath,
  };

  return (
    <EnvironmentContext.Provider value={value}>{children}</EnvironmentContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useEnvironment(): EnvironmentContextType {
  const context = useContext(EnvironmentContext);
  if (!context) {
    throw new Error("useEnvironment must be used within an EnvironmentProvider");
  }
  return context;
}
