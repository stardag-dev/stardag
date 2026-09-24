import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import type { Build, BuildListResponse } from "../types/task";

vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({ activeEnvironment: { id: "env-1" } }),
}));
vi.mock("../api/registry", () => ({ fetchBuilds: vi.fn() }));

import { fetchBuilds } from "../api/registry";
import { BuildsList } from "./BuildsList";

const mocked = vi.mocked(fetchBuilds);

function build(id: string, overrides: Partial<Build> = {}): Build {
  return {
    id,
    name: `build ${id}`,
    description: null,
    status: "running",
    root_task_ids: [],
    created_at: "2026-09-24T00:00:00Z",
    started_at: "2026-09-24T00:00:00Z",
    completed_at: null,
    last_active_at: "2026-09-24T00:00:00Z",
    is_resumed: false,
    status_triggered_by_user_id: null,
    executor_metadata: null,
    reactive_app_name: null,
    reactive_tick_kwargs: null,
    error_message: null,
    ...overrides,
  };
}

function page(ids: string[], total: number, next: string | null): BuildListResponse {
  return { builds: ids.map((id) => build(id)), total, next_cursor: next };
}

function renderList() {
  return render(
    <BreadcrumbProvider>
      <BuildsList onSelectBuild={() => {}} />
    </BreadcrumbProvider>,
  );
}

beforeEach(() => mocked.mockReset());

describe("BuildsList", () => {
  it("pages with the server's cursor and shows the total", async () => {
    mocked
      .mockResolvedValueOnce(page(["a"], 45, "cursor-2"))
      .mockResolvedValueOnce(page(["b"], 45, "cursor-3"))
      .mockResolvedValueOnce(page(["a"], 45, "cursor-2"));
    renderList();

    expect(await screen.findByText("build a")).toBeInTheDocument();
    expect(screen.getByText("45 builds")).toBeInTheDocument();
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    expect(mocked.mock.calls[0][1]).toMatchObject({ limit: 20, cursor: undefined });

    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText("build b")).toBeInTheDocument();
    expect(screen.getByText("Page 2 of 3")).toBeInTheDocument();
    expect(mocked.mock.calls[1][1]).toMatchObject({ cursor: "cursor-2" });

    fireEvent.click(screen.getByRole("button", { name: "Previous" }));
    expect(await screen.findByText("build a")).toBeInTheDocument();
    expect(mocked.mock.calls[2][1]).toMatchObject({ cursor: undefined });
  });

  it("says the list is ordered by last activity, as the server orders it", async () => {
    mocked.mockResolvedValueOnce(page(["a"], 1, null));
    renderList();
    await screen.findByText("build a");
    const header = screen.getByRole("columnheader", { name: /last active/i });
    expect(header).toHaveAttribute("aria-sort", "descending");
    expect(header.title).toMatch(/most recent first/);
    expect(screen.queryByText(/newest/i)).not.toBeInTheDocument();
  });

  it("returns to page 1 when a filter changes", async () => {
    mocked
      .mockResolvedValueOnce(page(["a"], 45, "cursor-2"))
      .mockResolvedValueOnce(page(["b"], 45, "cursor-3"))
      .mockResolvedValue(page(["c"], 1, null));
    renderList();
    await screen.findByText("build a");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await screen.findByText("build b");

    fireEvent.change(screen.getByLabelText("Filter by build status"), {
      target: { value: "failed" },
    });
    await screen.findByText("build c");
    expect(mocked.mock.lastCall?.[1]).toMatchObject({
      status: "failed",
      cursor: undefined,
    });
    await waitFor(() => expect(screen.queryByText(/^Page /)).not.toBeInTheDocument());
  });
});

describe("BuildsList idle filter", () => {
  it("sends idle_for_seconds and blocks the statuses the server refuses", async () => {
    mocked.mockResolvedValue(page(["a"], 1, null));
    renderList();
    await screen.findByText("build a");

    fireEvent.change(screen.getByLabelText("Filter by time since last activity"), {
      target: { value: "86400" },
    });
    await waitFor(() =>
      expect(mocked.mock.lastCall?.[1]).toMatchObject({ idleForSeconds: 86400 }),
    );
    expect(await screen.findByText("1 build running, idle ≥ 24h")).toBeInTheDocument();
    const failed = screen.getByRole("option", {
      name: "Failed — not idle-filterable",
    }) as HTMLOptionElement;
    expect(failed.disabled).toBe(true);
    const running = screen.getByRole("option", {
      name: "Running",
    }) as HTMLOptionElement;
    expect(running.disabled).toBe(false);
  });

  it("disables the idle filter while a finished status is selected", async () => {
    mocked.mockResolvedValue(page(["a"], 1, null));
    renderList();
    await screen.findByText("build a");
    fireEvent.change(screen.getByLabelText("Filter by build status"), {
      target: { value: "completed" },
    });
    expect(screen.getByLabelText("Filter by time since last activity")).toBeDisabled();
  });
});
