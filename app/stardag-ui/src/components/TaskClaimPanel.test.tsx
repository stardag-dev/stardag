import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Task } from "../types/task";
import { CLAIM_ACTION_LABELS } from "../utils/claims";

vi.mock("../api/registry", async () => {
  const actual =
    await vi.importActual<typeof import("../api/registry")>("../api/registry");
  return {
    RegistryError: actual.RegistryError,
    cancelMember: vi.fn(),
    retryMember: vi.fn(),
  };
});

import { cancelMember, RegistryError, retryMember } from "../api/registry";
import { TaskClaimPanel } from "./TaskClaimPanel";

const HOUR = 3600 * 1000;
const TASK_ID = "df0c8b03-fab2-5ddd-9743-09fb4a634cf5";
const HOLDER_BUILD = "01a0c5c3-f18e-7d22-bcaf-00000000aaaa";
const HOLDER_PLAN = "01a0c5c3-f18e-7d22-bcaf-0000000p1a11";
const VIEWED_BUILD = "01a0c5c3-f18e-7d22-bcaf-00000000bbbb";
const VIEWED_PLAN = "01a0c5c3-f18e-7d22-bcaf-0000000p2b22";
const RELEASE = `${CLAIM_ACTION_LABELS.release}…`;
const RESET = `${CLAIM_ACTION_LABELS.retry}…`;

function makeTask(overrides: Partial<Task> = {}): Task {
  return {
    task_id: TASK_ID,
    task_namespace: "demo",
    task_name: "GrindBeans",
    version: null,
    output_uri: null,
    status: "running",
    status_at: new Date(Date.now() - 4 * HOUR).toISOString(),
    started_at: new Date(Date.now() - 4 * HOUR).toISOString(),
    completed_at: null,
    error_message: null,
    claim_expires_at: new Date(Date.now() + HOUR).toISOString(),
    claim_plan_id: HOLDER_PLAN,
    claim_build_id: HOLDER_BUILD,
    execution_id: null,
    instances: [],
    ...overrides,
  };
}

const lapsed = () =>
  makeTask({ claim_expires_at: new Date(Date.now() - HOUR).toISOString() });

async function confirmIn(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(screen.getAllByRole("button", { name: label }).slice(-1)[0]);
}

beforeEach(() => {
  vi.mocked(cancelMember).mockReset();
  vi.mocked(retryMember).mockReset();
});

describe("TaskClaimPanel claim holder", () => {
  it("states the holder and how long in one line, linking to the holder", async () => {
    const onOpenBuild = vi.fn();
    const user = userEvent.setup();
    render(
      <TaskClaimPanel
        task={makeTask()}
        environmentId="env-1"
        buildId={VIEWED_BUILD}
        planId={VIEWED_PLAN}
        onChanged={() => {}}
        onOpenBuild={onOpenBuild}
      />,
    );
    expect(screen.getByText(/which holds its claim/)).toBeInTheDocument();
    expect(screen.getByText(/4h 00m/)).toBeInTheDocument();
    expect(screen.getByText(/not the build you are viewing/)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "…0000aaaa" }));
    expect(onOpenBuild).toHaveBeenCalledWith(HOLDER_BUILD);
  });

  it("keeps the explanation in the dialog, not the pane", async () => {
    const user = userEvent.setup();
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    expect(screen.queryByText(/next checkpoint/)).toBeNull();

    await user.click(screen.getByRole("button", { name: RELEASE }));
    expect(
      await screen.findByText(/so that build retries it on its next tick/),
    ).toBeInTheDocument();
    expect(screen.getByText(/it finds out at its next checkpoint/)).toBeInTheDocument();
  });

  it("addresses the release to the holder's plan, not the viewed build's", async () => {
    vi.mocked(cancelMember).mockResolvedValue({
      applied: true,
      status: "cancelled",
      execution_id: null,
      claim_expires_at: null,
    });
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(
      <TaskClaimPanel
        task={makeTask()}
        environmentId="env-1"
        buildId={VIEWED_BUILD}
        planId={VIEWED_PLAN}
        onChanged={onChanged}
      />,
    );
    await user.click(screen.getByRole("button", { name: RELEASE }));
    await confirmIn(user, CLAIM_ACTION_LABELS.release);
    await waitFor(() =>
      expect(cancelMember).toHaveBeenCalledWith(HOLDER_PLAN, TASK_ID, "env-1"),
    );
    expect(onChanged).toHaveBeenCalled();
    expect(
      await screen.findByText(/Released the claim under build/),
    ).toBeInTheDocument();
  });

  it("releases from the task page, with no build in view", async () => {
    vi.mocked(cancelMember).mockResolvedValue({
      applied: true,
      status: "cancelled",
      execution_id: null,
      claim_expires_at: null,
    });
    const user = userEvent.setup();
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    await user.click(screen.getByRole("button", { name: RELEASE }));
    await confirmIn(user, CLAIM_ACTION_LABELS.release);
    await waitFor(() =>
      expect(cancelMember).toHaveBeenCalledWith(HOLDER_PLAN, TASK_ID, "env-1"),
    );
  });

  it("offers the release to any member: there is no admin gate", () => {
    // Maintainer decision 2026-09-24, matching the CLI: the server is the
    // authority and records the act as an event.
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    expect(screen.getByRole("button", { name: RELEASE })).toBeInTheDocument();
    expect(screen.queryByText(/admin role/)).toBeNull();
  });

  it("shows the server's refusal when the claim moved meanwhile", async () => {
    vi.mocked(cancelMember).mockRejectedValue(
      new RegistryError("not the claim holder", 409, "not_claim_holder"),
    );
    const user = userEvent.setup();
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    await user.click(screen.getByRole("button", { name: RELEASE }));
    await confirmIn(user, CLAIM_ACTION_LABELS.release);
    expect(await screen.findByText("not the claim holder")).toBeInTheDocument();
  });

  it("points at the holder's build-level stop for a running task", () => {
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    expect(
      screen.getByText(/To stop what is running, use Build controls .* …0000aaaa/),
    ).toBeInTheDocument();
  });

  it("offers no release when the holder is not recorded, and says so", () => {
    render(
      <TaskClaimPanel
        task={makeTask({ claim_plan_id: null, claim_build_id: null })}
        environmentId="env-1"
        buildId={VIEWED_BUILD}
        planId={VIEWED_PLAN}
        onChanged={() => {}}
      />,
    );
    expect(
      screen.getByText(/The build holding its claim is not recorded/),
    ).toBeVisible();
    expect(screen.queryByRole("button", { name: RELEASE })).toBeNull();
    expect(screen.queryByText(/To stop what is running/)).toBeNull();
  });

  it("says nothing about claims for a task that holds none", () => {
    render(
      <TaskClaimPanel
        task={makeTask({ status: "completed", claim_expires_at: null })}
        environmentId="env-1"
        buildId={VIEWED_BUILD}
        planId={VIEWED_PLAN}
        onChanged={() => {}}
      />,
    );
    expect(screen.queryByText(/claim/i)).toBeNull();
    expect(screen.queryByRole("button", { name: RELEASE })).toBeNull();
  });

  it("reports the holder without cross-build wording when no build is in view", () => {
    render(
      <TaskClaimPanel task={makeTask()} environmentId="env-1" onChanged={() => {}} />,
    );
    expect(screen.getByText(/which holds its claim/)).toBeInTheDocument();
    expect(screen.queryByText(/not the build you are viewing/)).toBeNull();
  });

  it("resets a lapsed claim through the holder's plan when no build is in view", async () => {
    vi.mocked(retryMember).mockResolvedValue({
      applied: true,
      status: "pending",
      execution_id: null,
      claim_expires_at: null,
    });
    const user = userEvent.setup();
    render(
      <TaskClaimPanel task={lapsed()} environmentId="env-1" onChanged={() => {}} />,
    );
    await user.click(screen.getByRole("button", { name: RESET }));
    await confirmIn(user, CLAIM_ACTION_LABELS.retry);
    await waitFor(() =>
      expect(retryMember).toHaveBeenCalledWith(HOLDER_PLAN, TASK_ID, "env-1"),
    );
  });

  it("resets through the viewed build's plan when one is in view", async () => {
    vi.mocked(retryMember).mockResolvedValue({
      applied: true,
      status: "pending",
      execution_id: null,
      claim_expires_at: null,
    });
    const user = userEvent.setup();
    render(
      <TaskClaimPanel
        task={lapsed()}
        environmentId="env-1"
        buildId={VIEWED_BUILD}
        planId={VIEWED_PLAN}
        onChanged={() => {}}
      />,
    );
    await user.click(screen.getByRole("button", { name: RESET }));
    await confirmIn(user, CLAIM_ACTION_LABELS.retry);
    await waitFor(() =>
      expect(retryMember).toHaveBeenCalledWith(VIEWED_PLAN, TASK_ID, "env-1"),
    );
  });
});
