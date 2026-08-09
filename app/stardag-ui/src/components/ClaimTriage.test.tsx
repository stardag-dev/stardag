import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../types/task";
import { ClaimTriage } from "./ClaimTriage";

let mockWorkspaceRole: "owner" | "admin" | "member" | null = "admin";
vi.mock("../context/EnvironmentContext", () => ({
  useEnvironment: () => ({
    activeEnvironment: { id: "env-1", slug: "default", name: "default" },
    activeWorkspaceRole: mockWorkspaceRole,
  }),
}));

vi.mock("../api/tasks", () => ({
  fetchTasks: vi.fn(),
  cancelTask: vi.fn(),
}));

import { cancelTask, fetchTasks } from "../api/tasks";

const HOUR = 3600 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

// Two obviously fictional claim holders owned by two different builds.
const BUILD_A = "aaaaaaaa-1111-2222-3333-444444444444";
const BUILD_B = "bbbbbbbb-5555-6666-7777-888888888888";

function makeTask(
  overrides: Partial<Task> & Pick<Task, "task_id" | "task_name">,
): Task {
  return {
    id: `pk-${overrides.task_id}`,
    environment_id: "env-1",
    task_namespace: "",
    task_data: {},
    version: null,
    output_uri: null,
    created_at: ago(9 * HOUR),
    status: "running",
    started_at: ago(8 * HOUR),
    completed_at: null,
    error_message: null,
    artifact_count: 0,
    latest_status: "running",
    latest_status_at: ago(8 * HOUR),
    latest_status_build_id: BUILD_A,
    ...overrides,
  };
}

const grind = makeTask({ task_id: "tid-grind", task_name: "GrindBeans" });
const steam = makeTask({
  task_id: "tid-steam",
  task_name: "SteamMilk",
  latest_status: "suspended",
  status: "suspended",
  latest_status_at: ago(2 * HOUR),
  latest_status_build_id: BUILD_B,
});

function response(tasks: Task[]) {
  return { tasks, total: tasks.length, page: 1, page_size: 50 };
}

function renderTriage() {
  return render(
    <ClaimTriage
      environmentId="env-1"
      selectedTaskId={null}
      onSelectTask={() => {}}
      onNavigateToBuild={onNavigate}
    />,
  );
}

const onNavigate = vi.fn();

describe("ClaimTriage", () => {
  beforeEach(() => {
    mockWorkspaceRole = "admin";
    vi.mocked(fetchTasks).mockResolvedValue(response([grind, steam]));
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("asks for claim-holding statuses and shows the holder of each", async () => {
    const user = userEvent.setup();
    renderTriage();

    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchTasks).toHaveBeenCalledWith(
        expect.objectContaining({
          environment_id: "env-1",
          status: ["running", "suspended"],
          status_older_than: undefined,
        }),
      ),
    );

    expect(screen.getByText("8h 00m")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: BUILD_B.slice(0, 8) }));
    expect(onNavigate).toHaveBeenCalledWith(BUILD_B);
  });

  it("turns a staleness choice into an absolute cutoff", async () => {
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.selectOptions(
      screen.getByLabelText("Held for at least"),
      String(6 * 3600),
    );

    await waitFor(() => {
      const last = vi.mocked(fetchTasks).mock.calls.at(-1)![0]!;
      expect(last.status_older_than).toBeTruthy();
      // An absolute instant, not a duration: reproducible across pages.
      const cutoff = new Date(last.status_older_than!).getTime();
      expect(Date.now() - cutoff).toBeGreaterThanOrEqual(6 * HOUR - 5000);
      expect(Date.now() - cutoff).toBeLessThan(6 * HOUR + 5000);
    });
  });

  it("releases each selected claim under its own owning build", async () => {
    vi.mocked(cancelTask).mockResolvedValue("cancelled");
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.click(
      screen.getByRole("checkbox", { name: "Select all claims on this page" }),
    );
    expect(screen.getByText("2 claims selected")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Release claims" }));
    expect(await screen.findByText("Release 2 execution claims")).toBeInTheDocument();
    await user.click(
      screen.getAllByRole("button", { name: "Release claims" }).slice(-1)[0],
    );

    await waitFor(() => expect(cancelTask).toHaveBeenCalledTimes(2));
    expect(cancelTask).toHaveBeenCalledWith(BUILD_A, "tid-grind", "env-1");
    expect(cancelTask).toHaveBeenCalledWith(BUILD_B, "tid-steam", "env-1");
    // Refetched after mutating.
    await waitFor(() => expect(fetchTasks).toHaveBeenCalledTimes(2));
  });

  it("will not offer to release a claim a task is not holding", async () => {
    // Reachable by unchecking both status boxes, which used to drop the
    // status filter entirely and list every task in the environment.
    // Cancelling a completed task is not an error — the server records the
    // event and COMPLETED stays — so it reported a cheerful "released 1 of
    // 1 claims" having released nothing.
    vi.mocked(fetchTasks).mockResolvedValue({
      tasks: [
        makeTask({ task_id: "tid-grind", task_name: "GrindBeans" }),
        makeTask({
          task_id: "tid-done",
          task_name: "PourWater",
          status: "completed",
          latest_status: "completed",
        }),
      ],
      total: 2,
      page: 1,
      page_size: 25,
    });
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("PourWater");

    expect(
      screen.getByRole("checkbox", {
        name: /PourWater cannot be selected: it is completed and holds no claim/,
      }),
    ).toBeDisabled();

    // Select-all takes the claim holder and leaves the finished task.
    await user.click(
      screen.getByRole("checkbox", { name: "Select all claims on this page" }),
    );
    expect(screen.getByText("1 claim selected")).toBeInTheDocument();
  });

  it("keeps listing claim holders when every status box is unchecked", async () => {
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.click(screen.getByRole("checkbox", { name: "Running" }));
    await user.click(screen.getByRole("checkbox", { name: "Suspended" }));

    await waitFor(() => {
      const last = vi.mocked(fetchTasks).mock.calls.at(-1)![0]!;
      // Not `undefined`, which would list every task in the environment.
      expect(last.status).toEqual(["running", "suspended"]);
    });
  });

  it("reads the global status, not this build's, when judging a release", async () => {
    // The server reports both: `status` is what *this build* did (it did
    // cancel), `latest_status` is what the claim says (COMPLETED is sticky,
    // so nothing was held). Reading the former reports every no-op as a
    // success — the bug this whole guard exists to prevent.
    vi.mocked(cancelTask).mockResolvedValue("completed");
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.click(
      screen.getByRole("checkbox", { name: "Select all claims on this page" }),
    );
    await user.click(screen.getByRole("button", { name: "Release claims" }));
    await user.click(
      screen.getAllByRole("button", { name: "Release claims" }).slice(-1)[0],
    );

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Released 0 of 2 claims.");
    expect(banner).toHaveTextContent(/already completed — no claim to release/);
  });

  it("says so when a task finished before its claim could be released", async () => {
    // The race that survives the guard above: held when listed, finished
    // by the time the release lands. The write succeeds; COMPLETED is
    // sticky, so nothing was actually released.
    vi.mocked(cancelTask).mockImplementation(async (buildId: string) =>
      buildId === BUILD_B ? "completed" : "cancelled",
    );
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.click(
      screen.getByRole("checkbox", { name: "Select all claims on this page" }),
    );
    await user.click(screen.getByRole("button", { name: "Release claims" }));
    await user.click(
      screen.getAllByRole("button", { name: "Release claims" }).slice(-1)[0],
    );

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Released 1 of 2 claims.");
    expect(banner).toHaveTextContent("SteamMilk");
    expect(banner).toHaveTextContent(/already completed — no claim to release/);
  });

  it("reports partial failure per task rather than as one error", async () => {
    // A task can complete between being listed and being acted on, so a
    // mixed result is the normal case, not an exception.
    vi.mocked(cancelTask).mockImplementation(async (buildId: string) => {
      if (buildId === BUILD_B) throw new Error("Task not found");
      return "cancelled";
    });
    const user = userEvent.setup();
    renderTriage();
    await screen.findByText("GrindBeans");

    await user.click(
      screen.getByRole("checkbox", { name: "Select all claims on this page" }),
    );
    await user.click(screen.getByRole("button", { name: "Release claims" }));
    await user.click(
      screen.getAllByRole("button", { name: "Release claims" }).slice(-1)[0],
    );

    const banner = await screen.findByRole("status");
    expect(banner).toHaveTextContent("Released 1 of 2 claims.");
    // The task that failed is named, with the server's own reason.
    expect(banner).toHaveTextContent("SteamMilk");
    expect(banner).toHaveTextContent("Task not found");
    expect(banner).not.toHaveTextContent("GrindBeans");
  });

  it("does not let a task with no recorded owning build be selected", async () => {
    vi.mocked(fetchTasks).mockResolvedValue(
      response([grind, makeTask({ ...steam, latest_status_build_id: null })]),
    );
    renderTriage();
    await screen.findByText("SteamMilk");

    expect(screen.getByText("not recorded")).toBeInTheDocument();
    expect(
      screen.getByRole("checkbox", {
        name: /SteamMilk cannot be selected/,
      }),
    ).toBeDisabled();
  });

  it("gives non-admin members the view but no selection and no bulk action", async () => {
    mockWorkspaceRole = "member";
    renderTriage();

    expect(await screen.findByText("GrindBeans")).toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox", { name: /^Select / })).toHaveLength(0);
    expect(
      screen.queryByRole("button", { name: "Release claims" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByText("Releasing claims requires the workspace admin role."),
    ).toBeInTheDocument();
  });

  it("distinguishes 'nothing holds a claim' from 'nothing matches the filters'", async () => {
    vi.mocked(fetchTasks).mockResolvedValue(response([]));
    const user = userEvent.setup();
    renderTriage();

    expect(await screen.findByText(/nothing is holding a claim/i)).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("Held for at least"), "3600");
    expect(
      await screen.findByText(/Widen the filters to see every claim/),
    ).toBeInTheDocument();
  });

  it("surfaces a load failure", async () => {
    vi.mocked(fetchTasks).mockRejectedValue(new Error("Service unavailable"));
    renderTriage();
    expect(await screen.findByRole("alert")).toHaveTextContent("Service unavailable");
  });
});
