import { useCallback, useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { setAccessTokenGetter } from "./api/client";
import { AuthCallback } from "./components/AuthCallback";
import { DagGraph } from "./components/DagGraph";
import { OrganizationSettings } from "./components/OrganizationSettings";
import { PendingInvites } from "./components/PendingInvites";
import { TaskDetail } from "./components/TaskDetail";
import { TaskFilters } from "./components/TaskFilters";
import { TaskTable } from "./components/TaskTable";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { WorkspaceSelector } from "./components/WorkspaceSelector";
import { AuthProvider, useAuth } from "./context/AuthContext";
import type React from "react";
import { ThemeProvider } from "./context/ThemeContext";
import { WorkspaceProvider } from "./context/WorkspaceContext";
import { useTasks } from "./hooks/useTasks";
import type { Task } from "./types/task";

interface DashboardProps {
  onNavigate: (path: string) => void;
}

function Dashboard({ onNavigate }: DashboardProps) {
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const {
    tasks,
    tasksWithContext,
    graph,
    currentBuild,
    builds,
    total,
    page,
    setPage,
    loading,
    error,
    familyFilter,
    setFamilyFilter,
    statusFilter,
    setStatusFilter,
    pageSize,
    totalPages,
    selectBuild,
  } = useTasks();

  const handleDagTaskClick = useCallback(
    (taskId: string) => {
      // Look in tasksWithContext first (includes related tasks)
      const task = tasksWithContext.find((t) => t.task_id === taskId);
      if (task) setSelectedTask(task);
    },
    [tasksWithContext],
  );

  return (
    <div className="flex h-screen flex-col bg-gray-100 dark:bg-gray-900">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <div className="flex items-center gap-4">
          <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">
            Stardag
          </h1>
          <WorkspaceSelector />
          {builds.length > 0 && (
            <select
              value={currentBuild?.id ?? ""}
              onChange={(e) => selectBuild(e.target.value)}
              className="rounded-md border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-700 px-3 py-1.5 text-sm text-gray-900 dark:text-gray-100 focus:border-blue-500 focus:outline-none focus:ring-1 focus:ring-blue-500"
            >
              {builds.map((build) => (
                <option key={build.id} value={build.id}>
                  {build.name} ({build.status})
                </option>
              ))}
            </select>
          )}
        </div>
        <div className="flex items-center gap-3">
          <button
            onClick={() => onNavigate("/settings")}
            className="rounded-md p-2 text-gray-500 hover:bg-gray-100 hover:text-gray-700 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
            title="Organization Settings"
          >
            <svg
              className="h-5 w-5"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z"
              />
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M15 12a3 3 0 11-6 0 3 3 0 016 0z"
              />
            </svg>
          </button>
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>

      {/* Main content */}
      <div className="flex-1 overflow-hidden">
        <PanelGroup direction="horizontal">
          {/* Left column: Filters + DAG + List */}
          <Panel defaultSize={selectedTask ? 70 : 100} minSize={40}>
            <div className="flex h-full flex-col">
              {/* Filters */}
              <TaskFilters
                familyFilter={familyFilter}
                onFamilyFilterChange={setFamilyFilter}
                statusFilter={statusFilter}
                onStatusFilterChange={setStatusFilter}
              />

              {/* DAG + List with resizable split */}
              <PanelGroup direction="vertical" className="flex-1">
                {/* DAG Graph */}
                <Panel defaultSize={50} minSize={20}>
                  <div className="h-full border-b border-gray-200 dark:border-gray-700">
                    <DagGraph
                      tasks={tasksWithContext}
                      graph={graph}
                      selectedTaskId={selectedTask?.task_id ?? null}
                      onTaskClick={handleDagTaskClick}
                    />
                  </div>
                </Panel>

                <PanelResizeHandle className="h-1 bg-gray-200 dark:bg-gray-700 hover:bg-blue-400 dark:hover:bg-blue-500 transition-colors cursor-row-resize" />

                {/* Task List */}
                <Panel defaultSize={50} minSize={20}>
                  <TaskTable
                    tasks={tasks}
                    loading={loading}
                    error={error}
                    selectedTaskId={selectedTask?.task_id ?? null}
                    onSelectTask={setSelectedTask}
                    page={page}
                    pageSize={pageSize}
                    total={total}
                    totalPages={totalPages}
                    onPageChange={setPage}
                  />
                </Panel>
              </PanelGroup>
            </div>
          </Panel>

          {/* Right column: Task Detail (only when task selected) */}
          {selectedTask && (
            <>
              <PanelResizeHandle className="w-1 bg-gray-200 dark:bg-gray-700 hover:bg-blue-400 dark:hover:bg-blue-500 transition-colors cursor-col-resize" />
              <Panel defaultSize={30} minSize={20} maxSize={50}>
                <div className="h-full border-l border-gray-200 dark:border-gray-700">
                  <TaskDetail
                    task={selectedTask}
                    onClose={() => setSelectedTask(null)}
                  />
                </div>
              </Panel>
            </>
          )}
        </PanelGroup>
      </div>
    </div>
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
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <div className="flex items-center gap-4">
          <button
            onClick={() => onNavigate("/")}
            className="text-xl font-bold text-gray-900 dark:text-gray-100 hover:text-blue-600 dark:hover:text-blue-400"
          >
            Stardag
          </button>
        </div>
        <div className="flex items-center gap-3">
          <ThemeToggle />
          <UserMenu />
        </div>
      </header>

      {/* Content */}
      <main className="mx-auto max-w-2xl px-4 py-8">
        <PendingInvites />

        <div className="mt-8 text-center">
          <button
            onClick={() => onNavigate("/")}
            className="text-sm text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200"
          >
            Back to Dashboard
          </button>
        </div>
      </main>
    </div>
  );
}

// Simple URL-based routing
function Router() {
  const [path, setPath] = useState(window.location.pathname);

  useEffect(() => {
    const handlePopState = () => setPath(window.location.pathname);
    window.addEventListener("popstate", handlePopState);
    return () => window.removeEventListener("popstate", handlePopState);
  }, []);

  const navigateTo = useCallback((newPath: string) => {
    window.history.pushState({}, "", newPath);
    setPath(newPath);
  }, []);

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

  if (path === "/settings") {
    return <OrganizationSettings onNavigate={navigateTo} />;
  }

  if (path === "/invites") {
    return <InvitesPage onNavigate={navigateTo} />;
  }

  return <Dashboard onNavigate={navigateTo} />;
}

function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthConnector>
          <WorkspaceProvider>
            <Router />
          </WorkspaceProvider>
        </AuthConnector>
      </AuthProvider>
    </ThemeProvider>
  );
}

export default App;
