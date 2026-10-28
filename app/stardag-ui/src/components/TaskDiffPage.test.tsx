import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import type { Task, TaskInstance, TaskSummary } from "../types/task";

vi.mock("../api/registry", () => ({ fetchTask: vi.fn(), fetchTasks: vi.fn() }));

import { fetchTask, fetchTasks, type TaskFilters } from "../api/registry";
import { TaskDiffPage } from "./TaskDiffPage";

const T1 = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";
const T2 = "aa11bb22-0000-5000-8000-000000000002";

function summary(id: string, overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    task_id: id,
    task_namespace: "examples.ml",
    task_name: "Metrics",
    version: null,
    output_uri: null,
    status: "completed",
    status_at: "2026-10-01T00:00:00Z",
    started_at: null,
    completed_at: "2026-10-01T00:00:00Z",
    error_message: null,
    claim_expires_at: null,
    claim_plan_id: null,
    claim_build_id: null,
    execution_id: null,
    tags: [],
    description: null,
    ...overrides,
  };
}

function instance(id: string, threshold: number): TaskInstance {
  return {
    id,
    deployment_id: "dep-1",
    settings_hash: "s",
    instance_hash: `${id}-hash-0000`,
    body: { __namespace: "examples.ml", __name: "Metrics", threshold, split: "test" },
    expanded_at: null,
    created_at: "2026-10-01T00:00:00Z",
  };
}

const TASKS: Record<string, Task> = {
  [T1]: { ...summary(T1), instances: [instance("i-1b", 0.6), instance("i-1a", 0.5)] },
  [T2]: { ...summary(T2), instances: [instance("i-2", 0.9)] },
};

const mockedTask = vi.mocked(fetchTask);
const mockedTasks = vi.mocked(fetchTasks);

function searchFilters(): TaskFilters[] {
  return mockedTasks.mock.calls
    .map((call) => call[1] ?? {})
    .filter((filters) => filters.limit === 50);
}

function params() {
  return new URLSearchParams(window.location.search);
}

function renderPage(url: string, onOpenTask = vi.fn()) {
  window.history.replaceState({}, "", url);
  render(
    <BreadcrumbProvider>
      <TaskDiffPage environmentId="env-1" onOpenTask={onOpenTask} />
    </BreadcrumbProvider>,
  );
  return onOpenTask;
}

beforeEach(() => {
  mockedTask.mockReset();
  mockedTask.mockImplementation(async (id: string) => {
    const task = TASKS[id];
    if (!task) throw new Error("Task not found");
    return task;
  });
  mockedTasks.mockReset();
  mockedTasks.mockResolvedValue({
    tasks: [summary(T1), summary(T2, { tags: ["candidate"] })],
    next_cursor: null,
  });
  localStorage.clear();
});

describe("TaskDiffPage", () => {
  it("shows the left side pre-selected, with its instance picker", async () => {
    renderPage(
      `/task-diff?left=${T1}&leftInstance=i-1a&namespace=examples.ml&name=Metrics`,
    );
    const left = await screen.findByRole("region", { name: "Left side" });
    expect(await within(left).findByText("examples.ml.Metrics")).toBeInTheDocument();
    expect(within(left).getByText(T1.slice(0, 8))).toBeInTheDocument();
    expect(within(left).getByRole("combobox", { name: "Left instance" })).toHaveValue(
      "i-1a",
    );
  });

  it("pre-filters the right-hand search to the left task's type, and says so", async () => {
    renderPage(`/task-diff?left=${T1}`);
    await screen.findByText(/a diff is most useful between tasks of one type/);
    // A link without a search gets the type filter written into the URL.
    expect(params().get("namespace")).toBe("examples.ml");
    expect(params().get("name")).toBe("Metrics");
    expect(params().get("left")).toBe(T1);
    await waitFor(() =>
      expect(searchFilters().at(-1)).toMatchObject({
        namespace: "examples.ml",
        name: "Metrics",
      }),
    );
    // The filter is shown as removable chips.
    expect(
      screen.getByRole("button", { name: "Remove name filter" }),
    ).toBeInTheDocument();
  });

  it("allows loosening the filter, with a hint and a way back", async () => {
    renderPage(`/task-diff?left=${T1}&namespace=examples.ml&name=Metrics`);
    await screen.findByText(/a diff is most useful/);
    fireEvent.click(screen.getByRole("button", { name: "Remove name filter" }));
    expect(params().get("name")).toBeNull();
    // The selection survives a search edit.
    expect(params().get("left")).toBe(T1);
    const restore = await screen.findByRole("button", {
      name: "Limit to examples.ml.Metrics again",
    });
    expect(screen.getByText(/mostly additions and removals/)).toBeInTheDocument();
    fireEvent.click(restore);
    expect(params().get("name")).toBe("Metrics");
  });

  it("fills the right side from a search result, in the URL, and Back undoes it", async () => {
    renderPage(`/task-diff?left=${T1}&namespace=examples.ml&name=Metrics`);
    fireEvent.click(
      await screen.findByRole("button", {
        name: `Compare with examples.ml.Metrics ${T2.slice(0, 8)}`,
      }),
    );
    expect(params().get("right")).toBe(T2);
    expect(params().get("name")).toBe("Metrics");
    const tree = await screen.findByRole("tree", { name: "Parameter differences" });
    expect(
      within(tree).getByRole("treeitem", {
        name: "threshold: changed from 0.6, number to 0.9, number",
      }),
    ).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Right side" })).toBeInTheDocument();

    window.history.back();
    await waitFor(() => expect(params().get("right")).toBeNull());
    expect(
      await screen.findByText(/Pick a task to compare with from the search below/),
    ).toBeInTheDocument();
  });

  it("compares two instances of one task", async () => {
    renderPage(`/task-diff?left=${T1}&namespace=examples.ml&name=Metrics`);
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Or compare with another instance of this task",
      }),
    );
    expect(params().get("right")).toBe(T1);
    expect(params().get("rightInstance")).toBe("i-1a");
    expect(
      await screen.findByRole("treeitem", {
        name: "threshold: changed from 0.6, number to 0.5, number",
      }),
    ).toBeInTheDocument();
    // Read once: both sides are the same task.
    expect(mockedTask.mock.calls.filter(([id]) => id === T1)).toHaveLength(1);
  });

  it("switches instances and swaps sides through the URL", async () => {
    renderPage(`/task-diff?left=${T1}&right=${T2}`);
    const picker = await screen.findByRole("combobox", { name: "Left instance" });
    fireEvent.change(picker, { target: { value: "i-1a" } });
    expect(params().get("leftInstance")).toBe("i-1a");
    expect(
      await screen.findByRole("treeitem", {
        name: "threshold: changed from 0.5, number to 0.9, number",
      }),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Swap left and right" }));
    expect(params().get("left")).toBe(T2);
    expect(params().get("right")).toBe(T1);
    expect(params().get("rightInstance")).toBe("i-1a");
    expect(
      await screen.findByRole("treeitem", {
        name: "threshold: changed from 0.9, number to 0.5, number",
      }),
    ).toBeInTheDocument();
  });

  it("returns to the search from a picked right side", async () => {
    renderPage(`/task-diff?left=${T1}&right=${T2}`);
    await screen.findByRole("tree", { name: "Parameter differences" });
    fireEvent.click(screen.getByRole("button", { name: "Change" }));
    expect(params().get("right")).toBeNull();
    expect(
      await screen.findByRole("combobox", { name: "Search tasks" }),
    ).toBeInTheDocument();
  });

  it("explains itself without a left side", () => {
    const onOpenTask = renderPage("/task-diff");
    expect(screen.getByText("Nothing to compare yet")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search tasks" }));
    expect(onOpenTask).toHaveBeenCalledWith("");
  });

  it("reports a task that cannot be read", async () => {
    renderPage("/task-diff?left=missing");
    expect(
      await screen.findByText("Could not read the left task: Task not found"),
    ).toBeInTheDocument();
  });
});
