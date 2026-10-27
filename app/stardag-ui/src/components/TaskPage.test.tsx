import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({ activeEnvironment: { id: "env-1" } }),
}));
vi.mock("./TaskDetail", () => ({ TaskDetail: () => null }));
vi.mock("../api/registry", () => ({
  fetchTasks: vi.fn(async () => ({ tasks: [], next_cursor: null })),
}));

import { TaskPage } from "./TaskPage";

describe("TaskPage", () => {
  it("is the task search without a task id", async () => {
    window.history.replaceState({}, "", "/tasks");
    render(
      <BreadcrumbProvider>
        <TaskPage taskId={null} onOpenTask={() => {}} />
      </BreadcrumbProvider>,
    );
    expect(screen.getByRole("combobox", { name: "Search tasks" })).toBeInTheDocument();
    expect(await screen.findByText("No tasks yet")).toBeInTheDocument();
  });
});
