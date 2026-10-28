import {
  act,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { TaskSummary } from "../types/task";

vi.mock("../api/registry", () => ({ fetchTasks: vi.fn() }));

import { fetchTasks, type TaskFilters } from "../api/registry";
import { notifyLocationChange } from "../hooks/useTaskSearchUrl";
import { TASK_PAGE_SIZE, TaskSearch } from "./TaskSearch";

const mocked = vi.mocked(fetchTasks);

function task(id: string, overrides: Partial<TaskSummary> = {}): TaskSummary {
  return {
    task_id: id,
    task_namespace: "examples.ml",
    task_name: "TrainModel",
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
    tags: ["baseline"],
    description: "A model trained on the sample data",
    ...overrides,
  };
}

const T1 = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";
const T2 = "aa11bb22-0000-5000-8000-000000000002";

// The search's own reads, not the one-off read that seeds autocomplete.
function searchCalls(): TaskFilters[] {
  return mocked.mock.calls
    .map((call) => call[1] ?? {})
    .filter((filters) => filters.limit === TASK_PAGE_SIZE);
}

function lastSearch(): TaskFilters {
  const calls = searchCalls();
  return calls[calls.length - 1];
}

beforeEach(() => {
  mocked.mockReset();
  mocked.mockResolvedValue({ tasks: [task(T1)], next_cursor: null });
  localStorage.clear();
  window.history.replaceState({}, "", "/tasks");
});

describe("TaskSearch", () => {
  it("reads its search from the URL", async () => {
    window.history.replaceState({}, "", "/tasks?tag=prod&status=completed&namespace=");
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    expect(lastSearch()).toMatchObject({
      tags: ["prod"],
      status: "completed",
      namespace: "",
    });
    expect(screen.getByLabelText("Filter by task status")).toHaveValue("completed");
    expect(screen.getByText("(root)")).toBeInTheDocument();
  });

  it("debounces typing into q, in the URL without a history entry", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const before = window.history.length;
    fireEvent.change(screen.getByRole("combobox", { name: "Search tasks" }), {
      target: { value: "train" },
    });
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "train" }));
    expect(window.location.search).toBe("?q=train");
    expect(window.history.length).toBe(before);
  });

  it("issues no request for text under three characters, and says so", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "tr" } });
    expect(
      screen.getByText(/Type at least 3 characters to search names/),
    ).toBeInTheDocument();
    // Suggestions still come from what was seen.
    expect(
      within(screen.getByRole("listbox")).getByRole("option", {
        name: /examples\.ml\.TrainModel/,
      }),
    ).toBeInTheDocument();
    await waitFor(() => expect(window.location.search).toBe("?q=tr"));
    expect(searchCalls().every((filters) => filters.q === undefined)).toBe(true);
    expect(searchCalls()).toHaveLength(1);
    fireEvent.change(box, { target: { value: "tra" } });
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "tra" }));
  });

  it("keeps the rows when text is shortened under three characters", async () => {
    window.history.replaceState({}, "", "/tasks?q=tra");
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const calls = searchCalls().length;
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.change(box, { target: { value: "tr" } });
    await waitFor(() => expect(window.location.search).toBe("?q=tr"));
    await new Promise((r) => setTimeout(r, 50));
    expect(searchCalls()).toHaveLength(calls);
    expect(screen.getByText("TrainModel")).toBeInTheDocument();
    // A filter change still reads, without q.
    fireEvent.change(screen.getByLabelText("Filter by task status"), {
      target: { value: "failed" },
    });
    await waitFor(() =>
      expect(lastSearch()).toEqual({ status: "failed", limit: TASK_PAGE_SIZE }),
    );
    // So does clearing the box.
    fireEvent.change(box, { target: { value: "" } });
    await waitFor(() => expect(window.location.search).toBe("?status=failed"));
  });

  it("settles typing with trailing whitespace", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const spy = vi.spyOn(window.history, "replaceState");
    fireEvent.change(screen.getByRole("combobox", { name: "Search tasks" }), {
      target: { value: "train " },
    });
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "train" }));
    await new Promise((r) => setTimeout(r, 700));
    expect(spy).toHaveBeenCalledTimes(1);
    spy.mockRestore();
  });

  it("clears a failed Load more once a retry succeeds", async () => {
    let failNext = true;
    mocked.mockImplementation(async (_env, filters = {}) => {
      if (filters.cursor) {
        if (failNext) {
          failNext = false;
          throw new Error("page failed");
        }
        return {
          tasks: [task(T2, { task_name: "Evaluate" })],
          next_cursor: null,
        };
      }
      return { tasks: [task(T1)], next_cursor: "c1" };
    });
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("page failed");
    fireEvent.click(screen.getByRole("button", { name: "Load more" }));
    await screen.findByText("Evaluate");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("still pages after a pending read is cancelled by shortening the text", async () => {
    window.history.replaceState({}, "", "/tasks?q=tra");
    mocked.mockImplementation(async (_env, filters = {}) => {
      if (filters.cursor) {
        return {
          tasks: [task(T2, { task_name: "Evaluate" })],
          next_cursor: null,
        };
      }
      if (filters.q === "trai") return new Promise(() => {}); // never settles
      return { tasks: [task(T1)], next_cursor: "c1" };
    });
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.change(box, { target: { value: "trai" } });
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "trai" }));
    // Shortened under three characters while that read is still pending.
    fireEvent.change(box, { target: { value: "tr" } });
    await waitFor(() => expect(window.location.search).toBe("?q=tr"));
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Evaluate");
    expect(screen.queryByRole("button", { name: /Loading/ })).toBeNull();
  });

  it("does not send a short q read from the URL", async () => {
    window.history.replaceState({}, "", "/tasks?q=ab&status=failed");
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    expect(lastSearch()).toEqual({ status: "failed", limit: TASK_PAGE_SIZE });
  });

  it("searches an id-like input by id prefix and says so", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    fireEvent.change(screen.getByRole("combobox", { name: "Search tasks" }), {
      target: { value: "DF0C8B03" },
    });
    await waitFor(() => expect(lastSearch()).toMatchObject({ idPrefix: "df0c8b03" }));
    expect(lastSearch().q).toBeUndefined();
    expect(screen.getByText(/task id starts with/)).toBeInTheDocument();
  });

  it("adds a clicked tag as a filter, as a new history entry", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const before = window.history.length;
    fireEvent.click(screen.getByRole("button", { name: "Filter by tag baseline" }));
    await waitFor(() => expect(lastSearch()).toMatchObject({ tags: ["baseline"] }));
    expect(window.location.search).toBe("?tag=baseline");
    expect(window.history.length).toBe(before + 1);
    // The filter chip removes it again.
    fireEvent.click(screen.getByRole("button", { name: "Remove tag filter baseline" }));
    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("follows Back/Forward, search box included", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    act(() => {
      window.history.pushState({}, "", "/tasks?q=evaluate");
      window.dispatchEvent(new PopStateEvent("popstate"));
    });
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "evaluate" }));
    expect(screen.getByRole("combobox", { name: "Search tasks" })).toHaveValue(
      "evaluate",
    );
  });

  it("follows the app's own navigation to the same path", async () => {
    window.history.replaceState({}, "", "/tasks?q=train");
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await waitFor(() => expect(lastSearch()).toMatchObject({ q: "train" }));
    act(() => {
      window.history.pushState({}, "", "/tasks");
      notifyLocationChange();
    });
    await waitFor(() => expect(lastSearch()).toEqual({ limit: TASK_PAGE_SIZE }));
    expect(screen.getByRole("combobox", { name: "Search tasks" })).toHaveValue("");
  });

  it("pages with the cursor and the same filters", async () => {
    window.history.replaceState({}, "", "/tasks?q=train");
    mocked.mockImplementation(async (_env, filters = {}) =>
      filters.cursor
        ? {
            tasks: [task(T2, { task_name: "Evaluate", tags: [] })],
            next_cursor: null,
          }
        : { tasks: [task(T1)], next_cursor: "c1" },
    );
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    fireEvent.click(await screen.findByRole("button", { name: "Load more" }));
    await screen.findByText("Evaluate");
    expect(lastSearch()).toMatchObject({ q: "train", cursor: "c1" });
    expect(screen.getByText("TrainModel")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Load more" })).toBeNull();
  });

  it("opens a task from its row, and a pasted full id on Enter", async () => {
    const onOpenTask = vi.fn();
    render(<TaskSearch environmentId="env-1" onOpenTask={onOpenTask} />);
    fireEvent.click(await screen.findByText("TrainModel"));
    expect(onOpenTask).toHaveBeenCalledWith(T1);
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.change(box, { target: { value: T2.replace(/-/g, "") } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onOpenTask).toHaveBeenLastCalledWith(T2);
  });

  it("autocompletes names and tags seen before, and applies the choice", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    await screen.findByText("TrainModel");
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.focus(box);
    fireEvent.change(box, { target: { value: "trai" } });
    const listbox = await screen.findByRole("listbox");
    const option = within(listbox).getByRole("option", {
      name: /examples\.ml\.TrainModel/,
    });
    fireEvent.mouseDown(option);
    await waitFor(() =>
      expect(lastSearch()).toMatchObject({
        namespace: "examples.ml",
        name: "TrainModel",
      }),
    );
    expect(box).toHaveValue("");
  });

  it("adds a typed #tag as a filter on Enter", async () => {
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    const box = screen.getByRole("combobox", { name: "Search tasks" });
    fireEvent.change(box, { target: { value: "#nightly" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(lastSearch()).toMatchObject({ tags: ["nightly"] }));
    expect(lastSearch().q).toBeUndefined();
  });

  it("says when nothing matches, and offers to clear", async () => {
    window.history.replaceState({}, "", "/tasks?q=nothing");
    mocked.mockResolvedValue({ tasks: [], next_cursor: null });
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    expect(await screen.findByText("No tasks match this search")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Clear search" })[0]);
    await waitFor(() => expect(window.location.search).toBe(""));
  });

  it("shows a failed read with a retry", async () => {
    mocked.mockRejectedValueOnce(new Error("registry down"));
    render(<TaskSearch environmentId="env-1" onOpenTask={() => {}} />);
    expect(await screen.findByRole("alert")).toHaveTextContent("registry down");
    mocked.mockResolvedValue({ tasks: [task(T1)], next_cursor: null });
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await screen.findByText("TrainModel");
  });
});
