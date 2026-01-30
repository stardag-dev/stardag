import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { setAccessTokenGetter } from "./api/client";
import { AuthCallback } from "./components/AuthCallback";
import { BuildsList } from "./components/BuildsList";
import { BuildView } from "./components/BuildView";
import { CreateWorkspace } from "./components/CreateWorkspace";
import { LandingPageDemo } from "./components/LandingPageDemo";
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

// USP cards data
const USP_CARDS = [
  {
    title: "Declarative & Composable",
    description:
      "Tasks are Pydantic-based specifications, not just code. Loose coupling, full reusability, easy testing, and static type checking of I/O contracts.",
    color: "text-blue-400",
    icon: (
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M4 5a1 1 0 011-1h14a1 1 0 011 1v2a1 1 0 01-1 1H5a1 1 0 01-1-1V5zM4 13a1 1 0 011-1h6a1 1 0 011 1v6a1 1 0 01-1 1H5a1 1 0 01-1-1v-6zM16 13a1 1 0 011-1h2a1 1 0 011 1v6a1 1 0 01-1 1h-2a1 1 0 01-1-1v-6z"
      />
    ),
  },
  {
    title: "Provenance & Reproducibility",
    description:
      "Searchable and human-readable specifications of how any asset was produced. Compare any two assets to see what changed.",
    color: "text-green-400",
    icon: (
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4"
      />
    ),
  },
  {
    title: "Smart Execution",
    description:
      "Makefile-style with persistent caching: build only what's incomplete. Full asyncio support, manage concurrency, remote execution with Modal.",
    color: "text-purple-400",
    icon: (
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth={2}
        d="M13 10V3L4 14h7v7l9-11h-7z"
      />
    ),
  },
];

// USP carousel component with auto-advance on mobile
function UspsCarousel() {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [activeIndex, setActiveIndex] = useState(0);
  const [isPaused, setIsPaused] = useState(false);

  // Auto-advance carousel on mobile
  useEffect(() => {
    const isMobile = window.innerWidth < 768;
    if (!isMobile || isPaused) return;

    const interval = setInterval(() => {
      setActiveIndex((prev) => (prev + 1) % USP_CARDS.length);
    }, 4000);

    return () => clearInterval(interval);
  }, [isPaused]);

  // Scroll to active card
  useEffect(() => {
    if (!scrollRef.current) return;
    const cardWidth = scrollRef.current.offsetWidth;
    scrollRef.current.scrollTo({
      left: activeIndex * cardWidth,
      behavior: "smooth",
    });
  }, [activeIndex]);

  // Update active index on manual scroll
  const handleScroll = () => {
    if (!scrollRef.current) return;
    const cardWidth = scrollRef.current.offsetWidth;
    const newIndex = Math.round(scrollRef.current.scrollLeft / cardWidth);
    if (newIndex !== activeIndex) {
      setActiveIndex(newIndex);
    }
  };

  const cardContent = (card: (typeof USP_CARDS)[0]) => (
    <>
      <div className={`mb-3 ${card.color}`}>
        <svg className="h-8 w-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          {card.icon}
        </svg>
      </div>
      <h3 className="mb-2 text-base font-semibold text-white sm:text-lg">
        {card.title}
      </h3>
      <p className="text-sm text-gray-400">{card.description}</p>
    </>
  );

  return (
    <div className="mt-16 w-full min-w-0">
      {/* Mobile carousel - uses grid for reliable sizing */}
      <div className="min-w-0 md:hidden">
        <div
          ref={scrollRef}
          className="grid min-w-0 auto-cols-[100%] grid-flow-col snap-x snap-mandatory gap-4 overflow-x-auto"
          style={{ scrollbarWidth: "none", msOverflowStyle: "none" }}
          onScroll={handleScroll}
          onTouchStart={() => setIsPaused(true)}
          onTouchEnd={() => setIsPaused(false)}
        >
          {USP_CARDS.map((card, index) => (
            <div
              key={index}
              className="min-w-0 snap-start overflow-hidden rounded-lg bg-gray-800/50 p-5 text-left"
            >
              {cardContent(card)}
            </div>
          ))}
        </div>

        {/* Dots indicator */}
        <div className="mt-4 flex justify-center gap-2">
          {USP_CARDS.map((_, index) => (
            <button
              key={index}
              onClick={() => {
                setActiveIndex(index);
                setIsPaused(true);
              }}
              className={`h-2 w-2 rounded-full transition-colors ${
                index === activeIndex ? "bg-blue-500" : "bg-gray-600"
              }`}
              aria-label={`Go to slide ${index + 1}`}
            />
          ))}
        </div>
      </div>

      {/* Desktop grid */}
      <div className="hidden gap-6 text-left md:grid md:grid-cols-3">
        {USP_CARDS.map((card, index) => (
          <div key={index} className="rounded-lg bg-gray-800/50 p-6">
            {cardContent(card)}
          </div>
        ))}
      </div>
    </div>
  );
}

// Landing page for non-authenticated users
function LandingPage() {
  const { login } = useAuth();

  return (
    <div className="flex min-h-screen flex-col overflow-x-hidden bg-gradient-to-br from-gray-900 via-gray-800 to-gray-900">
      <header className="flex items-center justify-between px-6 py-4">
        <Logo size="md" className="text-white" />
        <button
          onClick={login}
          className="rounded-md bg-blue-600 px-4 py-2 text-sm font-medium text-white transition-colors hover:bg-blue-700"
        >
          Sign In
        </button>
      </header>

      <main className="flex flex-1 items-center justify-center px-6 pb-16 pt-8 md:pt-0">
        <div className="min-w-0 max-w-5xl text-center">
          <div className="mb-8 mt-8 md:mt-0">
            <span
              className="select-none font-mono text-4xl font-medium text-white sm:text-5xl md:text-6xl"
              style={{ fontFamily: "'IBM Plex Mono', monospace" }}
            >
              Stardag
            </span>
          </div>
          <h2 className="mb-8 text-2xl font-bold text-white sm:text-3xl">
            <span className="whitespace-nowrap">Declarative DAGs</span>{" "}
            <span className="whitespace-nowrap">for Data & ML</span>
          </h2>
          <p className="mb-10 text-lg text-gray-300 sm:text-xl">
            A modern Python framework for building composable pipelines with persistent
            asset management. Track provenance, ensure reproducibility, and iterate
            faster on data science and ML workflows.
          </p>
          <div className="flex flex-col items-center gap-3 sm:flex-row sm:flex-wrap sm:justify-center sm:gap-4 mb-4">
            <button
              onClick={login}
              className="w-full rounded-md bg-blue-600 px-6 py-3 text-base font-medium text-white transition-colors hover:bg-blue-700 sm:w-auto sm:text-lg"
            >
              Get Started
            </button>
            <a
              href="https://docs.stardag.dev"
              target="_blank"
              rel="noopener noreferrer"
              className="w-full rounded-md border border-gray-600 px-6 py-3 text-center text-base font-medium text-gray-300 transition-colors hover:bg-gray-800 sm:w-auto sm:text-lg"
            >
              Documentation
            </a>
            <a
              href="https://github.com/stardag-dev/stardag"
              target="_blank"
              rel="noopener noreferrer"
              className="w-full rounded-md border border-gray-600 px-6 py-3 text-center text-base font-medium text-gray-300 transition-colors hover:bg-gray-800 sm:w-auto sm:text-lg"
            >
              GitHub
            </a>
          </div>

          <UspsCarousel />

          <LandingPageDemo />

          {/* Final CTA */}
          <div className="mt-20 mb-8 text-center">
            <p className="mb-6 text-lg text-gray-400 italic">
              Never send an asset on Slack or hardcode another file path{" "}
              <code className="rounded bg-gray-800 px-2 py-1 text-sm text-gray-300">
                mydata_v4_extra-cleaning-final2.h5
              </code>{" "}
              again.
            </p>
            <button
              onClick={login}
              className="rounded-md bg-blue-600 px-6 py-3 text-base font-medium text-white transition-colors hover:bg-blue-700 sm:text-lg"
            >
              Start building now
            </button>
          </div>
        </div>
      </main>
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
