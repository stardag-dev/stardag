import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import { ConcurrencyLimits } from "./ConcurrencyLimits";

let mockEnvironmentId = "env-1";
let mockWorkspaceRole: "owner" | "admin" | "member" | null = "admin";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: {
      id: mockEnvironmentId,
      slug: "default",
      name: "default",
    },
    activeWorkspaceRole: mockWorkspaceRole,
  }),
}));

vi.mock("../api/concurrencyLimits", () => ({
  fetchConcurrencyLimits: vi.fn(),
  fetchConcurrencyLimitHolders: vi.fn(),
  upsertConcurrencyLimit: vi.fn(),
  deleteConcurrencyLimit: vi.fn(),
  evictConcurrencyLimitHolder: vi.fn(),
}));

vi.mock("../api/tasks", () => ({
  fetchTask: vi.fn(),
  fetchTaskArtifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
  fetchTaskEvents: vi.fn().mockResolvedValue([]),
  cancelTask: vi.fn(),
}));

import {
  evictConcurrencyLimitHolder,
  fetchConcurrencyLimitHolders,
  fetchConcurrencyLimits,
  upsertConcurrencyLimit,
} from "../api/concurrencyLimits";

const holder = {
  task_id: "task-abc-123456",
  task_namespace: "",
  task_name: "TrainModel",
  latest_status_at: "2026-01-01T10:00:00Z",
  latest_executor: "modal",
  latest_executor_ref: "fc-123",
  latest_executor_metadata: {
    kind: "modal",
    app_name: "my-app",
    workspace: "my-workspace",
    app_id: "ap-123",
    function_id: "fu-456",
  },
};

function renderPage() {
  return render(
    <BreadcrumbProvider>
      <ConcurrencyLimits />
    </BreadcrumbProvider>,
  );
}

describe("ConcurrencyLimits", () => {
  beforeEach(() => {
    mockEnvironmentId = "env-1";
    mockWorkspaceRole = "admin";
    vi.mocked(fetchConcurrencyLimits).mockResolvedValue([
      { key: "gpu", max_concurrent: 2 },
    ]);
    vi.mocked(fetchConcurrencyLimitHolders).mockResolvedValue({
      key: "gpu",
      holders: [holder],
      total: 1,
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("lists limits with holder counts", async () => {
    renderPage();

    expect(await screen.findByText("gpu")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();
    // Holder count fetched with limit=1
    await waitFor(() =>
      expect(fetchConcurrencyLimitHolders).toHaveBeenCalledWith("gpu", "env-1", 1),
    );
    expect(await screen.findByText("1")).toBeInTheDocument();
  });

  it("drops stale responses from a previous environment", async () => {
    // env-1's limits fetch resolves LATE — after the user has switched to
    // env-2 — and must not overwrite env-2's state.
    let resolveEnv1: (limits: { key: string; max_concurrent: number }[]) => void;
    vi.mocked(fetchConcurrencyLimits).mockImplementation((envId: string) => {
      if (envId === "env-1") {
        return new Promise((resolve) => {
          resolveEnv1 = resolve;
        });
      }
      return Promise.resolve([{ key: "env2-key", max_concurrent: 3 }]);
    });

    const { rerender } = renderPage();
    // env-1 load in flight; switch environments and re-render.
    mockEnvironmentId = "env-2";
    rerender(
      <BreadcrumbProvider>
        <ConcurrencyLimits />
      </BreadcrumbProvider>,
    );
    expect(await screen.findByText("env2-key")).toBeInTheDocument();

    // The slow env-1 response lands now: it must be dropped.
    resolveEnv1!([{ key: "stale-env1-key", max_concurrent: 9 }]);
    await waitFor(() =>
      expect(screen.queryByText("stale-env1-key")).not.toBeInTheDocument(),
    );
    expect(screen.getByText("env2-key")).toBeInTheDocument();
  });

  it("creates a limit via the form", async () => {
    vi.mocked(upsertConcurrencyLimit).mockResolvedValue({
      key: "db",
      max_concurrent: 5,
    });
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpu");

    await user.type(screen.getByPlaceholderText("e.g. gpu"), "db");
    const maxInput = screen.getByLabelText("Max concurrent");
    await user.clear(maxInput);
    await user.type(maxInput, "5");
    await user.click(screen.getByRole("button", { name: "Add limit" }));

    await waitFor(() =>
      expect(upsertConcurrencyLimit).toHaveBeenCalledWith("db", 5, "env-1"),
    );
  });

  it("drills into holders and evicts one after confirmation", async () => {
    vi.mocked(evictConcurrencyLimitHolder).mockResolvedValue({
      task_id: holder.task_id,
      status: "failed",
    });
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpu");

    // Expand the holders drill-down
    await user.click(await screen.findByTitle("Show current slot holders"));
    expect(await screen.findByText("TrainModel")).toBeInTheDocument();
    // Executor badge + Modal deep link render for the holder
    expect(screen.getByText("⚡ Modal")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View on Modal" })).toHaveAttribute(
      "href",
      "https://modal.com/apps/my-workspace/main/ap-123" +
        "?activeTab=functions&functionId=fu-456&functionSection=calls&fcId=fc-123",
    );

    await user.click(screen.getByRole("button", { name: "Evict" }));
    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() =>
      expect(evictConcurrencyLimitHolder).toHaveBeenCalledWith(
        "gpu",
        holder.task_id,
        "env-1",
      ),
    );

    confirmSpy.mockRestore();
  });

  it("does not evict when the confirm dialog is declined", async () => {
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpu");
    await user.click(await screen.findByTitle("Show current slot holders"));
    await screen.findByText("TrainModel");

    await user.click(screen.getByRole("button", { name: "Evict" }));
    expect(confirmSpy).toHaveBeenCalled();
    expect(evictConcurrencyLimitHolder).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  it("surfaces an evict failure as an action error", async () => {
    vi.mocked(evictConcurrencyLimitHolder).mockRejectedValue(
      new Error("Failed to evict holder: Not Found"),
    );
    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    const user = userEvent.setup();
    renderPage();
    await screen.findByText("gpu");
    await user.click(await screen.findByTitle("Show current slot holders"));
    await screen.findByText("TrainModel");

    await user.click(screen.getByRole("button", { name: "Evict" }));
    expect(
      await screen.findByText("Failed to evict holder: Not Found"),
    ).toBeInTheDocument();
    // The holder row is still rendered and actionable after the failure.
    expect(screen.getByRole("button", { name: "Evict" })).toBeEnabled();

    confirmSpy.mockRestore();
  });

  it("does not show a previous environment's holder count for a same-named key", async () => {
    // env-1's "gpu" has 5 holders; env-2 also has a "gpu" key but its
    // count fetch fails — env-2's row must show the unknown marker, not 5.
    vi.mocked(fetchConcurrencyLimitHolders).mockImplementation(
      (_key: string, envId: string) => {
        if (envId === "env-1") {
          return Promise.resolve({ key: "gpu", holders: [], total: 5 });
        }
        return Promise.reject(new Error("count fetch failed"));
      },
    );

    const { rerender } = renderPage();
    expect(await screen.findByText("5")).toBeInTheDocument();

    mockEnvironmentId = "env-2";
    rerender(
      <BreadcrumbProvider>
        <ConcurrencyLimits />
      </BreadcrumbProvider>,
    );
    // env-2's limits load (same "gpu" key) with the count fetch failing:
    // the row must show the unknown marker, never env-1's count.
    await waitFor(() => {
      expect(screen.getByTitle("Show current slot holders")).toHaveTextContent("—");
    });
    expect(screen.queryByText("5")).not.toBeInTheDocument();
  });

  it("hides limit and holder mutations from non-admin members", async () => {
    mockWorkspaceRole = "member";
    const user = userEvent.setup();
    renderPage();

    // Limits and holder counts are still viewable...
    expect(await screen.findByText("gpu")).toBeInTheDocument();
    expect(await screen.findByText("1")).toBeInTheDocument();
    expect(screen.getByText("2")).toBeInTheDocument();

    // ...but create/edit/delete are not.
    expect(screen.queryByRole("button", { name: "Add limit" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("e.g. gpu")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Edit max concurrency")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();

    // The holders drill-down still works, without the Evict action.
    await user.click(await screen.findByTitle("Show current slot holders"));
    expect(await screen.findByText("TrainModel")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Evict" })).not.toBeInTheDocument();
  });
});
