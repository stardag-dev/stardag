import { useState } from "react";
import { TaskDetail } from "./components/TaskDetail";
import { TaskList } from "./components/TaskList";
import type { Task } from "./types/task";

function App() {
  const [selectedTask, setSelectedTask] = useState<Task | null>(null);

  return (
    <div className="min-h-screen bg-gray-100">
      {/* Header */}
      <header className="bg-white shadow-sm">
        <div className="mx-auto max-w-7xl px-4 py-4 sm:px-6 lg:px-8">
          <h1 className="text-2xl font-bold text-gray-900">Stardag</h1>
        </div>
      </header>

      {/* Main content */}
      <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <div className="grid gap-6 lg:grid-cols-3">
          {/* Task list */}
          <div className={selectedTask ? "lg:col-span-2" : "lg:col-span-3"}>
            <TaskList onSelectTask={setSelectedTask} />
          </div>

          {/* Task detail panel */}
          {selectedTask && (
            <div className="lg:col-span-1">
              <TaskDetail task={selectedTask} onClose={() => setSelectedTask(null)} />
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

export default App;
