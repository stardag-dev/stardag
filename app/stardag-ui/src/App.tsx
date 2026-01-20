import { useCallback, useEffect, useMemo, useState } from "react";
import { setAccessTokenGetter } from "./api/client";
import { AuthCallback } from "./components/AuthCallback";
import { BuildsList } from "./components/BuildsList";
import { BuildView } from "./components/BuildView";
import { CreateWorkspace } from "./components/CreateWorkspace";
import { Logo } from "./components/Logo";
import { OnboardingModal } from "./components/OnboardingModal";
import { WorkspaceSelector } from "./components/WorkspaceSelector";
import { WorkspaceSettings } from "./components/WorkspaceSettings";
import { PendingInvites } from "./components/PendingInvites";
import type { NavItem } from "./components/Sidebar";
import { Sidebar } from "./components/Sidebar";
import { TaskExplorer } from "./components/TaskExplorer";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { AuthProvider, useAuth } from "./context/AuthContext";
import type React from "react";
import { ThemeProvider } from "./context/ThemeContext";
import { EnvironmentProvider, useEnvironment } from "./context/EnvironmentContext";

// Main app layout with sidebar
interface MainLayoutProps {
  children: React.ReactNode;
  activeNav: NavItem;
  onNavigate: (item: NavItem) => void;
  showHeader?: boolean;
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}

function MainLayout({
  children,
  activeNav,
  onNavigate,
  showHeader = true,
  sidebarCollapsed,
  onToggleSidebar,
}: MainLayoutProps) {
  return (
    <div className="flex h-screen bg-gray-100 dark:bg-gray-900">
      {/* Sidebar */}
      <Sidebar
        activeItem={activeNav}
        onNavigate={onNavigate}
        collapsed={sidebarCollapsed}
        onToggleCollapse={onToggleSidebar}
      />

      {/* Main content area */}
      <div className="flex flex-1 flex-col overflow-hidden">
        {/* Header */}
        {showHeader && (
          <header className="flex h-14 items-center justify-between border-b border-gray-200 bg-white px-4 dark:border-gray-700 dark:bg-gray-800">
            <div className="flex items-center gap-4">
              <WorkspaceSelector />
            </div>
            <div className="flex items-center gap-3">
              <ThemeToggle />
              <UserMenu />
            </div>
          </header>
        )}

        {/* Content */}
        <main className="flex-1 overflow-auto">{children}</main>
      </div>
    </div>
  );
}

// Shared sidebar props for all pages
interface SidebarStateProps {
  sidebarCollapsed: boolean;
  onToggleSidebar: () => void;
}

// Home page with builds list
interface HomePageProps extends SidebarStateProps {
  onNavigate: (item: NavItem) => void;
  onSelectBuild: (buildId: string) => void;
}

function HomePage({
  onNavigate,
  onSelectBuild,
  sidebarCollapsed,
  onToggleSidebar,
}: HomePageProps) {
  return (
    <MainLayout
      activeNav="home"
      onNavigate={onNavigate}
      sidebarCollapsed={sidebarCollapsed}
      onToggleSidebar={onToggleSidebar}
    >
      <BuildsList onSelectBuild={onSelectBuild} />
    </MainLayout>
  );
}

// Single build view
interface BuildPageProps extends SidebarStateProps {
  buildId: string;
  onNavigate: (item: NavItem) => void;
  onBack: () => void;
  onNavigateToBuild: (buildId: string) => void;
}

function BuildPage({
  buildId,
  onNavigate,
  onBack,
  onNavigateToBuild,
  sidebarCollapsed,
  onToggleSidebar,
}: BuildPageProps) {
  return (
    <MainLayout
      activeNav="home"
      onNavigate={onNavigate}
      sidebarCollapsed={sidebarCollapsed}
      onToggleSidebar={onToggleSidebar}
    >
      <BuildView
        buildId={buildId}
        onBack={onBack}
        onNavigateToBuild={onNavigateToBuild}
      />
    </MainLayout>
  );
}

// Task explorer page
interface TaskExplorerPageProps extends SidebarStateProps {
  onNavigate: (item: NavItem) => void;
  onNavigateToBuild: (buildId: string) => void;
}

function TaskExplorerPage({
  onNavigate,
  onNavigateToBuild,
  sidebarCollapsed,
  onToggleSidebar,
}: TaskExplorerPageProps) {
  return (
    <MainLayout
      activeNav="tasks"
      onNavigate={onNavigate}
      sidebarCollapsed={sidebarCollapsed}
      onToggleSidebar={onToggleSidebar}
    >
      <TaskExplorer onNavigateToBuild={onNavigateToBuild} />
    </MainLayout>
  );
}

// Settings page (reusing existing component)
interface SettingsPageProps extends SidebarStateProps {
  onNavigate: (item: NavItem) => void;
  onNavigatePath: (path: string) => void;
}

function SettingsPage({
  onNavigate,
  onNavigatePath,
  sidebarCollapsed,
  onToggleSidebar,
}: SettingsPageProps) {
  return (
    <MainLayout
      activeNav="settings"
      onNavigate={onNavigate}
      showHeader={false}
      sidebarCollapsed={sidebarCollapsed}
      onToggleSidebar={onToggleSidebar}
    >
      <WorkspaceSettings onNavigate={onNavigatePath} />
    </MainLayout>
  );
}

// Connect API client to auth when auth context is available
function AuthConnector({ children }: { children: React.ReactNode }) {
  const { getAccessToken } = useAuth();

  useEffect(() => {
    setAccessTokenGetter(getAccessToken);
  }, [getAccessToken]);

  return <>{children}</>;
}

// Page to view and accept pending invites
interface InvitesPageProps {
  onNavigate: (path: string) => void;
}

function InvitesPage({ onNavigate }: InvitesPageProps) {
  const { isAuthenticated } = useAuth();

  if (!isAuthenticated) {
    return (
      <div className="flex h-screen items-center justify-center bg-gray-100 dark:bg-gray-900">
        <div className="text-center">
          <p className="text-gray-600 dark:text-gray-400">
            Please sign in to view your invites.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gray-100 dark:bg-gray-900">
      <header className="flex items-center justify-between border-b border-gray-200 bg-white px-4 py-3 dark:border-gray-700 dark:bg-gray-800">
        <div className="flex items-center gap-4">
          <button
            onClick={() => onNavigate("/")}
            className="text-gray-900 hover:text-blue-600 dark:text-gray-100 dark:hover:text-blue-400"
          >
            <Logo size="md" />
          </button>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>

      <main className="mx-auto max-w-2xl px-4 py-8">
        <PendingInvites />

        <div className="mt-8 text-center">
          <button
            onClick={() => onNavigate("/workspaces/new")}
            className="text-sm text-gray-500 hover:text-gray-700 dark:text-gray-400 dark:hover:text-gray-200"
          >
            Create a New Workspace
          </button>
        </div>
      </main>
    </div>
  );
}

// Landing page for non-authenticated users
function LandingPage() {
  const { login } = useAuth();

  return (
    <div className="flex min-h-screen flex-col bg-gradient-to-br from-gray-900 via-gray-800 to-gray-900">
      <header className="flex items-center justify-between px-6 py-4">
        <Logo size="lg" className="text-white" />
        <button
          onClick={login}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700"
        >
          Sign In
        </button>
      </header>

      <main className="flex flex-1 items-center justify-center px-6">
        <div className="max-w-2xl text-center">
          <div className="mb-6">
            <span
              className="select-none font-mono text-6xl font-medium text-white"
              style={{ fontFamily: "'IBM Plex Mono', monospace" }}
            >
              Stardag
            </span>
          </div>
          <h2 className="mb-6 text-3xl font-bold text-white">
            Declarative DAG Framework
          </h2>
          <p className="mb-8 text-xl text-gray-300">
            Build composable data pipelines with type-safe tasks, deterministic outputs,
            and bottom-up execution. Track, monitor, and manage your workflows with
            ease.
          </p>
          <div className="flex justify-center gap-4">
            <button
              onClick={login}
              className="rounded-md bg-blue-600 px-6 py-3 text-lg font-medium text-white transition-colors hover:bg-blue-700"
            >
              Get Started
            </button>
            <a
              href="https://github.com/andhus/stardag"
              target="_blank"
              rel="noopener noreferrer"
              className="rounded-md border border-gray-600 px-6 py-3 text-lg font-medium text-gray-300 transition-colors hover:bg-gray-800"
            >
              View on GitHub
            </a>
          </div>

          <div className="mt-16 grid grid-cols-1 gap-8 text-left md:grid-cols-3">
            <div className="rounded-lg bg-gray-800/50 p-6">
              <div className="mb-3 text-blue-400">
                <svg
                  className="h-8 w-8"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"
                  />
                </svg>
              </div>
              <h3 className="mb-2 text-lg font-semibold text-white">
                Composable Tasks
              </h3>
              <p className="text-sm text-gray-400">
                Build complex pipelines from simple, reusable task components with full
                type safety.
              </p>
            </div>
            <div className="rounded-lg bg-gray-800/50 p-6">
              <div className="mb-3 text-green-400">
                <svg
                  className="h-8 w-8"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                  />
                </svg>
              </div>
              <h3 className="mb-2 text-lg font-semibold text-white">
                Deterministic Outputs
              </h3>
              <p className="text-sm text-gray-400">
                Parameter-based hashing ensures reproducible builds and efficient
                caching.
              </p>
            </div>
            <div className="rounded-lg bg-gray-800/50 p-6">
              <div className="mb-3 text-purple-400">
                <svg
                  className="h-8 w-8"
                  fill="none"
                  stroke="currentColor"
                  viewBox="0 0 24 24"
                >
                  <path
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    strokeWidth={2}
                    d="M13 10V3L4 14h7v7l9-11h-7z"
                  />
                </svg>
              </div>
              <h3 className="mb-2 text-lg font-semibold text-white">Smart Execution</h3>
              <p className="text-sm text-gray-400">
                Bottom-up, Makefile-style execution builds only what's needed.
              </p>
            </div>
          </div>
        </div>
      </main>

      <footer className="px-6 py-4 text-center text-sm text-gray-500">
        Built with Stardag
      </footer>
    </div>
  );
}

// URL-based routing with support for new views
function Router() {
  const { isAuthenticated } = useAuth();
  const { getEnvironmentPath } = useEnvironment();
  const [path, setPath] = useState(window.location.pathname);
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);

  const handleToggleSidebar = useCallback(() => {
    setSidebarCollapsed((prev) => !prev);
  }, []);

  // Derive selectedBuildId from path instead of using effect
  const selectedBuildId = useMemo(() => {
    const buildMatch = path.match(/\/builds\/([^/]+)/);
    return buildMatch ? buildMatch[1] : null;
  }, [path]);

  useEffect(() => {
    const handlePopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigateTo = useCallback((newPath: string) => {
    window.history.pushState({}, "", newPath);
    setPath(newPath);
  }, []);

  // Parse path to determine view
  const getViewFromPath = useCallback(() => {
    if (path === "/callback") return "callback";
    if (path === "/settings") return "settings";
    if (path === "/invites") return "invites";
    if (path === "/workspaces/new") return "new-workspace";

    // Check for tasks path: /tasks, /:org/tasks, or /:org/:environment/tasks
    if (path === "/tasks" || path.endsWith("/tasks")) return "tasks";

    // Check for build ID in path: /builds/:id or /:org/:environment/builds/:id
    const buildMatch = path.match(/\/builds\/([^/]+)/);
    if (buildMatch) {
      return "build";
    }

    return "home";
  }, [path]);

  // Handle sidebar navigation
  const handleNavigation = useCallback(
    (item: NavItem) => {
      const basePath = getEnvironmentPath();
      switch (item) {
        case "home":
          navigateTo(basePath || "/");
          break;
        case "tasks":
          navigateTo(`${basePath}/tasks`);
          break;
        case "settings":
          navigateTo("/settings");
          break;
      }
    },
    [navigateTo, getEnvironmentPath],
  );

  // Handle build selection
  const handleSelectBuild = useCallback(
    (buildId: string) => {
      const basePath = getEnvironmentPath();
      navigateTo(`${basePath}/builds/${buildId}`);
    },
    [navigateTo, getEnvironmentPath],
  );

  // Handle back from build view
  const handleBackFromBuild = useCallback(() => {
    const basePath = getEnvironmentPath();
    navigateTo(basePath || "/");
  }, [navigateTo, getEnvironmentPath]);

  // Auth callback - always handle regardless of auth state
  if (path === "/callback") {
    return (
      <AuthCallback
        onSuccess={() => navigateTo("/")}
        onError={() => {
          // Stay on callback page to show error
        }}
      />
    );
  }

  // Landing page for non-authenticated users
  if (!isAuthenticated) {
    return <LandingPage />;
  }

  // Authenticated routes - wrap with onboarding modal
  const view = getViewFromPath();

  const renderAuthenticatedContent = () => {
    switch (view) {
      case "settings":
        return (
          <SettingsPage
            onNavigate={handleNavigation}
            onNavigatePath={navigateTo}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={handleToggleSidebar}
          />
        );

      case "invites":
        return <InvitesPage onNavigate={navigateTo} />;

      case "new-workspace":
        return <CreateWorkspace onNavigate={navigateTo} />;

      case "tasks":
        return (
          <TaskExplorerPage
            onNavigate={handleNavigation}
            onNavigateToBuild={handleSelectBuild}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={handleToggleSidebar}
          />
        );

      case "build":
        if (selectedBuildId) {
          return (
            <BuildPage
              buildId={selectedBuildId}
              onNavigate={handleNavigation}
              onBack={handleBackFromBuild}
              onNavigateToBuild={handleSelectBuild}
              sidebarCollapsed={sidebarCollapsed}
              onToggleSidebar={handleToggleSidebar}
            />
          );
        }
        return (
          <HomePage
            onNavigate={handleNavigation}
            onSelectBuild={handleSelectBuild}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={handleToggleSidebar}
          />
        );

      case "home":
      default:
        return (
          <HomePage
            onNavigate={handleNavigation}
            onSelectBuild={handleSelectBuild}
            sidebarCollapsed={sidebarCollapsed}
            onToggleSidebar={handleToggleSidebar}
          />
        );
    }
  };

  return (
    <>
      <OnboardingModal />
      {renderAuthenticatedContent()}
    </>
  );
}

function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthConnector>
          <EnvironmentProvider>
            <Router />
          </EnvironmentProvider>
        </AuthConnector>
      </AuthProvider>
    </ThemeProvider>
  );
}

export default App;
