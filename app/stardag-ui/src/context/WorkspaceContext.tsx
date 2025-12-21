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
  fetchWorkspaces,
  type OrganizationSummary,
  type Workspace,
} from "../api/organizations";
import { useAuth } from "./AuthContext";

interface WorkspaceContextType {
  // User's organizations
  organizations: OrganizationSummary[];
  // Active organization
  activeOrg: OrganizationSummary | null;
  setActiveOrg: (org: OrganizationSummary | null) => void;
  // Workspaces in active org
  workspaces: Workspace[];
  // Active workspace
  activeWorkspace: Workspace | null;
  setActiveWorkspace: (workspace: Workspace | null) => void;
  // Loading state
  isLoading: boolean;
  // Refresh data, optionally selecting a specific org/workspace by slug
  refresh: (preferOrgSlug?: string, preferWorkspaceSlug?: string) => Promise<void>;
  // User's role in active org
  activeOrgRole: "owner" | "admin" | "member" | null;
  // Get the current URL path for org/workspace
  getWorkspacePath: () => string;
}

const WorkspaceContext = createContext<WorkspaceContextType | null>(null);

const STORAGE_KEY_ORG = "stardag_active_org_id";
const STORAGE_KEY_WORKSPACE = "stardag_active_workspace_id";

interface WorkspaceProviderProps {
  children: ReactNode;
}

export function WorkspaceProvider({ children }: WorkspaceProviderProps) {
  const { isAuthenticated, user } = useAuth();

  const [organizations, setOrganizations] = useState<OrganizationSummary[]>([]);
  const [activeOrg, setActiveOrgState] = useState<OrganizationSummary | null>(null);
  const [workspaces, setWorkspaces] = useState<Workspace[]>([]);
  const [activeWorkspace, setActiveWorkspaceState] = useState<Workspace | null>(null);
  const [isLoading, setIsLoading] = useState(false);

  // Load user's organizations when authenticated
  const loadOrganizations = useCallback(
    async (preferOrgSlug?: string, preferWorkspaceSlug?: string) => {
      if (!isAuthenticated) {
        setOrganizations([]);
        setActiveOrgState(null);
        setWorkspaces([]);
        setActiveWorkspaceState(null);
        return;
      }

      setIsLoading(true);
      try {
        const profile = await fetchUserProfile();
        setOrganizations(profile.organizations);

        // Priority for selecting org: 1) preferOrgSlug, 2) localStorage, 3) first org
        let orgToActivate: OrganizationSummary | null = null;
        if (preferOrgSlug) {
          orgToActivate =
            profile.organizations.find((o) => o.slug === preferOrgSlug) || null;
        }
        if (!orgToActivate) {
          const savedOrgId = localStorage.getItem(STORAGE_KEY_ORG);
          orgToActivate =
            profile.organizations.find((o) => o.id === savedOrgId) ||
            profile.organizations[0] ||
            null;
        }

        if (orgToActivate) {
          setActiveOrgState(orgToActivate);
          localStorage.setItem(STORAGE_KEY_ORG, orgToActivate.id);

          // Load workspaces for this org
          const ws = await fetchWorkspaces(orgToActivate.id);
          setWorkspaces(ws);

          // Priority for workspace: 1) preferWorkspaceSlug, 2) localStorage, 3) first
          let wsToActivate: Workspace | null = null;
          if (preferWorkspaceSlug) {
            wsToActivate = ws.find((w) => w.slug === preferWorkspaceSlug) || null;
          }
          if (!wsToActivate) {
            const savedWorkspaceId = localStorage.getItem(STORAGE_KEY_WORKSPACE);
            wsToActivate = ws.find((w) => w.id === savedWorkspaceId) || ws[0] || null;
          }

          setActiveWorkspaceState(wsToActivate);
          if (wsToActivate) {
            localStorage.setItem(STORAGE_KEY_WORKSPACE, wsToActivate.id);
          }
        }
      } catch (error) {
        console.error("Failed to load organizations:", error);
      } finally {
        setIsLoading(false);
      }
    },
    [isAuthenticated],
  );

  useEffect(() => {
    // Parse URL to get initial org/workspace slugs
    const path = window.location.pathname;
    const knownRoutes = ["/callback", "/settings", "/invites", "/organizations/new"];
    let orgSlug: string | undefined;
    let workspaceSlug: string | undefined;

    if (!knownRoutes.some((r) => path.startsWith(r))) {
      const parts = path.split("/").filter(Boolean);
      orgSlug = parts[0] || undefined;
      workspaceSlug = parts[1] || undefined;
    }

    loadOrganizations(orgSlug, workspaceSlug);
  }, [loadOrganizations, user]);

  // Set active organization
  const setActiveOrg = useCallback(async (org: OrganizationSummary | null) => {
    setActiveOrgState(org);
    if (org) {
      localStorage.setItem(STORAGE_KEY_ORG, org.id);
      // Load workspaces for this org
      try {
        const ws = await fetchWorkspaces(org.id);
        setWorkspaces(ws);
        // Select first workspace
        const firstWorkspace = ws[0] || null;
        setActiveWorkspaceState(firstWorkspace);
        if (firstWorkspace) {
          localStorage.setItem(STORAGE_KEY_WORKSPACE, firstWorkspace.id);
        } else {
          localStorage.removeItem(STORAGE_KEY_WORKSPACE);
        }
      } catch (error) {
        console.error("Failed to load workspaces:", error);
        setWorkspaces([]);
        setActiveWorkspaceState(null);
      }
    } else {
      localStorage.removeItem(STORAGE_KEY_ORG);
      localStorage.removeItem(STORAGE_KEY_WORKSPACE);
      setWorkspaces([]);
      setActiveWorkspaceState(null);
    }
  }, []);

  // Set active workspace
  const setActiveWorkspace = useCallback((workspace: Workspace | null) => {
    setActiveWorkspaceState(workspace);
    if (workspace) {
      localStorage.setItem(STORAGE_KEY_WORKSPACE, workspace.id);
    } else {
      localStorage.removeItem(STORAGE_KEY_WORKSPACE);
    }
  }, []);

  // Get URL path for current org/workspace
  const getWorkspacePath = useCallback(() => {
    if (!activeOrg) return "/";
    if (!activeWorkspace || activeWorkspace.slug === "default") {
      return `/${activeOrg.slug}`;
    }
    return `/${activeOrg.slug}/${activeWorkspace.slug}`;
  }, [activeOrg, activeWorkspace]);

  const value: WorkspaceContextType = {
    organizations,
    activeOrg,
    setActiveOrg,
    workspaces,
    activeWorkspace,
    setActiveWorkspace,
    isLoading,
    refresh: loadOrganizations,
    activeOrgRole: activeOrg?.role || null,
    getWorkspacePath,
  };

  return (
    <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
  );
}

// eslint-disable-next-line react-refresh/only-export-components
export function useWorkspace(): WorkspaceContextType {
  const context = useContext(WorkspaceContext);
  if (!context) {
    throw new Error("useWorkspace must be used within a WorkspaceProvider");
  }
  return context;
}
