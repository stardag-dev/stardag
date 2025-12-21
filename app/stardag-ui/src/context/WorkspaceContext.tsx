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
  // Refresh data
  refresh: () => Promise<void>;
  // User's role in active org
  activeOrgRole: "owner" | "admin" | "member" | null;
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
  const loadOrganizations = useCallback(async () => {
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

      // Restore active org from localStorage or use first org
      const savedOrgId = localStorage.getItem(STORAGE_KEY_ORG);
      const savedOrg = profile.organizations.find((o) => o.id === savedOrgId);
      const orgToActivate = savedOrg || profile.organizations[0] || null;

      if (orgToActivate) {
        setActiveOrgState(orgToActivate);
        // Load workspaces for this org
        const ws = await fetchWorkspaces(orgToActivate.id);
        setWorkspaces(ws);

        // Restore active workspace or use first
        const savedWorkspaceId = localStorage.getItem(STORAGE_KEY_WORKSPACE);
        const savedWorkspace = ws.find((w) => w.id === savedWorkspaceId);
        setActiveWorkspaceState(savedWorkspace || ws[0] || null);
      }
    } catch (error) {
      console.error("Failed to load organizations:", error);
    } finally {
      setIsLoading(false);
    }
  }, [isAuthenticated]);

  useEffect(() => {
    loadOrganizations();
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
