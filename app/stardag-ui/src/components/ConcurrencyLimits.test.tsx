import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { BreadcrumbProvider } from "../context/BreadcrumbContext";
import type { ConcurrencyLimit } from "../types/task";

const role = vi.hoisted(() => ({ current: "admin" as "admin" | "member" }));
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1" },
    activeWorkspaceRole: role.current,
  }),
}));
vi.mock("../api/registry", () => ({
  fetchConcurrencyLimits: vi.fn(),
  setConcurrencyLimit: vi.fn(async () => ({ key: "k", max_concurrent: 1 })),
  deleteConcurrencyLimit: vi.fn(async () => {}),
}));
vi.mock("./TaskDetail", () => ({
  TaskDetail: ({ taskId }: { taskId: string }) => (
    <div data-testid="detail">{taskId}</div>
  ),
}));

import {
  deleteConcurrencyLimit,
  fetchConcurrencyLimits,
  setConcurrencyLimit,
} from "../api/registry";
import { ConcurrencyLimits } from "./ConcurrencyLimits";

const BUILD_ID = "01a0c5c3-f18e-7d22-bcaf-add71bd0287c";
const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";

const limits: ConcurrencyLimit[] = [
  {
    key: "gpu",
    max_concurrent: 2,
    in_use: 1,
    holders: [
      {
        task_id: TASK_ID,
        task_name: "Train",
        build_id: BUILD_ID,
        plan_id: "plan-1",
        execution_id: "0199aaaa-bbbb-cccc",
        started_at: "2026-09-24T00:00:00Z",
      },
    ],
  },
  { key: "db", max_concurrent: 5, in_use: 0, holders: [] },
];

function renderPage(onSelectBuild = vi.fn()) {
  render(
    <BreadcrumbProvider>
      <ConcurrencyLimits onSelectBuild={onSelectBuild} />
    </BreadcrumbProvider>,
  );
  return { onSelectBuild };
}

beforeEach(() => {
  role.current = "admin";
  vi.mocked(fetchConcurrencyLimits).mockReset().mockResolvedValue(limits);
  vi.mocked(setConcurrencyLimit).mockClear();
  vi.mocked(deleteConcurrencyLimit).mockClear();
});

describe("ConcurrencyLimits", () => {
  it("lists each key with its cap and occupied slots, holders included", async () => {
    renderPage();
    expect(await screen.findByText("gpu")).toBeInTheDocument();
    expect(vi.mocked(fetchConcurrencyLimits)).toHaveBeenCalledWith("env-1", true);
    const toggles = screen.getAllByTitle("Show current slot holders");
    expect(toggles.map((t) => t.textContent)).toEqual(["1", "0"]);
  });

  it("drills into a key's holders, without evict, pointing at builds stop", async () => {
    const { onSelectBuild } = renderPage();
    await screen.findByText("gpu");
    fireEvent.click(screen.getAllByTitle("Show current slot holders")[0]);
    expect(screen.getByText("Train")).toBeInTheDocument();
    expect(screen.getByText("0199aaaa")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /evict/i })).not.toBeInTheDocument();
    expect(screen.getByText("stardag builds stop --mark-lost")).toBeInTheDocument();

    fireEvent.click(screen.getByTitle(`Open build ${BUILD_ID}`));
    expect(onSelectBuild).toHaveBeenCalledWith(BUILD_ID);
    fireEvent.click(screen.getByText("Train"));
    expect(screen.getByTestId("detail")).toHaveTextContent(TASK_ID);
  });

  it("creates, edits and deletes limits as an admin", async () => {
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPage();
    await screen.findByText("gpu");

    fireEvent.change(screen.getByLabelText("Key"), { target: { value: "io" } });
    fireEvent.change(screen.getByLabelText("Max concurrent"), {
      target: { value: "0" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add limit" }));
    await waitFor(() =>
      expect(vi.mocked(setConcurrencyLimit)).toHaveBeenCalledWith("io", 0, "env-1"),
    );

    fireEvent.click(screen.getAllByTitle("Edit max concurrency")[0]);
    fireEvent.change(screen.getByLabelText("Max concurrent for gpu"), {
      target: { value: "3" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() =>
      expect(vi.mocked(setConcurrencyLimit)).toHaveBeenCalledWith("gpu", 3, "env-1"),
    );

    fireEvent.click(screen.getAllByRole("button", { name: "Delete" })[1]);
    await waitFor(() =>
      expect(vi.mocked(deleteConcurrencyLimit)).toHaveBeenCalledWith("db", "env-1"),
    );
  });

  it("offers no mutations to a workspace member", async () => {
    role.current = "member";
    renderPage();
    await screen.findByText("gpu");
    expect(screen.queryByRole("button", { name: "Add limit" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
    expect(screen.queryByTitle("Edit max concurrency")).not.toBeInTheDocument();
  });
});
