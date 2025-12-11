import { useCallback, useState } from "react";
import { Panel, PanelGroup, PanelResizeHandle } from "react-resizable-panels";
import { DagGraph } from "./components/DagGraph";
import { TaskDetail } from "./components/TaskDetail";
import { TaskFilters } from "./components/TaskFilters";
import { TaskTable } from "./components/TaskTable";
import { ThemeToggle } from "./components/ThemeToggle";
import { ThemeProvider } from "./context/ThemeContext";
import { useTasks } from "./hooks/useTasks";
import type { Task } from "./types/task";

function Dashboard() {
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);
  const {
    tasks,
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
  } = useTasks();

  const handleTaskClick = useCallback(
    (taskId: string) => {
      const task = tasks.find((t) => t.task_id === taskId);
      if (task) setSelectedTask(task);
    },
    [tasks],
  );

  return (
    <div className="flex h-screen flex-col bg-gray-100 dark:bg-gray-900">
      {/* Header */}
      <header className="flex items-center justify-between border-b border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 px-4 py-3">
        <h1 className="text-xl font-bold text-gray-900 dark:text-gray-100">Stardag</h1>
        <ThemeToggle />
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
                      tasks={tasks}
                      selectedTaskId={selectedTask?.task_id ?? null}
                      onTaskClick={handleTaskClick}
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

function App() {
  return (
    <ThemeProvider>
      <Dashboard />
    </ThemeProvider>
  );
}

export default App;
