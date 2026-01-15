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
  type OrganizationSummary,
  type Environment,
} from "../api/organizations";
import { setCurrentOrgId } from "../api/client";
import { useAuth } from "./AuthContext";

interface EnvironmentContextType {
  // User's organizations
  organizations: OrganizationSummary[];
  // Active organization
  activeOrg: OrganizationSummary | null;
  setActiveOrg: (org: OrganizationSummary | null) => void;
  // Environments in active org
  environments: Environment[];
  // Active environment
  activeEnvironment: Environment | null;
  setActiveEnvironment: (environment: Environment | null) => void;
  // Loading state
  isLoading: boolean;
  // Token exchange in progress
  isExchangingToken: boolean;
  // Refresh data, optionally selecting a specific org/environment by slug
  refresh: (preferOrgSlug?: string, preferEnvironmentSlug?: string) => Promise<void>;
  // User's role in active org
  activeOrgRole: "owner" | "admin" | "member" | null;
  // Get the current URL path for org/environment
  getEnvironmentPath: () => string;
}

const EnvironmentContext = createContext<EnvironmentContextType | null>(null);

const STORAGE_KEY_ORG = "stardag_active_org_id";
const STORAGE_KEY_ENVIRONMENT = "stardag_active_environment_id";

interface EnvironmentProviderProps {
  children: ReactNode;
}

export function EnvironmentProvider({ children }: EnvironmentProviderProps) {
  const { isAuthenticated, user, exchangeForOrgToken, isExchangingToken } = useAuth();

  const [organizations, setOrganizations] = useState<OrganizationSummary[]>([]);
  const [activeOrg, setActiveOrgState] = useState<OrganizationSummary | null>(null);
  const [environments, setEnvironments] = useState<Environment[]>([]);
  const [activeEnvironment, setActiveEnvironmentState] = useState<Environment | null>(
    null,
  );
  const [isLoading, setIsLoading] = useState(false);

  // Update client's current org ID when activeOrg changes
  useEffect(() => {
    setCurrentOrgId(activeOrg?.id ?? null);
  }, [activeOrg]);

  // Load user's organizations when authenticated
  const loadOrganizations = useCallback(
    async (preferOrgSlug?: string, preferEnvironmentSlug?: string) => {
      if (!isAuthenticated) {
        setOrganizations([]);
        setActiveOrgState(null);
        setEnvironments([]);
        setActiveEnvironmentState(null);
        setCurrentOrgId(null);
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
          setCurrentOrgId(orgToActivate.id);

          // Exchange for org-scoped token
          await exchangeForOrgToken(orgToActivate.id);

          // Load environments for this org
          const envs = await fetchEnvironments(orgToActivate.id);
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
        console.error("Failed to load organizations:", error);
      } finally {
        setIsLoading(false);
      }
    },
    [isAuthenticated, exchangeForOrgToken],
  );

  useEffect(() => {
    // Parse URL to get initial org/environment slugs
    const path = window.location.pathname;
    const knownRoutes = ["/callback", "/settings", "/invites", "/organizations/new"];
    let orgSlug: string | undefined;
    let environmentSlug: string | undefined;

    if (!knownRoutes.some((r) => path.startsWith(r))) {
      const parts = path.split("/").filter(Boolean);
      orgSlug = parts[0] || undefined;
      environmentSlug = parts[1] || undefined;
    }

    loadOrganizations(orgSlug, environmentSlug);
  }, [loadOrganizations, user]);

  // Set active organization
  const setActiveOrg = useCallback(
    async (org: OrganizationSummary | null) => {
      setActiveOrgState(org);
      if (org) {
        localStorage.setItem(STORAGE_KEY_ORG, org.id);
        setCurrentOrgId(org.id);

        // Exchange for org-scoped token
        await exchangeForOrgToken(org.id);

        // Load environments for this org
        try {
          const envs = await fetchEnvironments(org.id);
          setEnvironments(envs);
          // Select first environment
          const firstEnvironment = envs[0] || null;
          setActiveEnvironmentState(firstEnvironment);
          if (firstEnvironment) {
            localStorage.setItem(STORAGE_KEY_ENVIRONMENT, firstEnvironment.id);
          } else {
            localStorage.removeItem(STORAGE_KEY_ENVIRONMENT);
          }
        } catch (error) {
          console.error("Failed to load environments:", error);
          setEnvironments([]);
          setActiveEnvironmentState(null);
        }
      } else {
        localStorage.removeItem(STORAGE_KEY_ORG);
        localStorage.removeItem(STORAGE_KEY_ENVIRONMENT);
        setCurrentOrgId(null);
        setEnvironments([]);
        setActiveEnvironmentState(null);
      }
    },
    [exchangeForOrgToken],
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

  // Get URL path for current org/environment
  const getEnvironmentPath = useCallback(() => {
    if (!activeOrg) return "/";
    if (!activeEnvironment || activeEnvironment.slug === "default") {
      return `/${activeOrg.slug}`;
    }
    return `/${activeOrg.slug}/${activeEnvironment.slug}`;
  }, [activeOrg, activeEnvironment]);

  const value: EnvironmentContextType = {
    organizations,
    activeOrg,
    setActiveOrg,
    environments,
    activeEnvironment,
    setActiveEnvironment,
    isLoading,
    isExchangingToken,
    refresh: loadOrganizations,
    activeOrgRole: activeOrg?.role || null,
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
