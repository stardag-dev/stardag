import { useCallback, useEffect, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { setAccessTokenGetter } from "./api/client";
import { AuthCallback } from "./components/AuthCallback";
import { DagGraph } from "./components/DagGraph";
import { TaskDetail } from "./components/TaskDetail";
import { TaskFilters } from "./components/TaskFilters";
import { TaskTable } from "./components/TaskTable";
import { ThemeToggle } from "./components/ThemeToggle";
import { UserMenu } from "./components/UserMenu";
import { AuthProvider, useAuth } from "./context/AuthContext";
import type React from "react";
import { ThemeProvider } from "./context/ThemeContext";
import { useTasks } from "./hooks/useTasks";
import type { Task } from "./types/task";

function Dashboard() {
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

  return <Dashboard />;
}

function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <AuthConnector>
          <Router />
        </AuthConnector>
      </AuthProvider>
    </ThemeProvider>
  );
}

export default App;
