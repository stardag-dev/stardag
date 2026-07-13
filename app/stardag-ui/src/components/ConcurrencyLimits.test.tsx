import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import { ConcurrencyLimits } from "./ConcurrencyLimits";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1", slug: "default", name: "default" },
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
      "https://modal.com/apps/my-workspace/main/deployed/my-app?functionCallId=fc-123",
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
});
